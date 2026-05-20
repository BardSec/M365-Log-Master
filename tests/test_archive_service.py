"""Unit tests for archive_service – DB and S3 client are mocked."""
from __future__ import annotations

import datetime as dt
import gzip
import io
import json
from unittest.mock import MagicMock, patch

import pytest

from app.archive_service import (
    _build_gzip_payload,
    _r2_key,
    archive_day,
    days_to_archive,
    load_archived_day,
    run_archive_sweep,
)
import app.archive_service as archive_service


# ─── _r2_key ─────────────────────────────────────────────────────────────────

def test_r2_key_format():
    assert _r2_key(dt.date(2026, 5, 20)) == "signins/2026/05/2026-05-20.jsonl.gz"
    assert _r2_key(dt.date(2026, 1, 3)) == "signins/2026/01/2026-01-03.jsonl.gz"


# ─── _build_gzip_payload ─────────────────────────────────────────────────────

def test_build_gzip_payload_basic():
    rows = [
        {"id": "a", "created_at": dt.datetime(2026, 5, 20, 12, 0), "pk": 1},
        {"id": "b", "created_at": dt.datetime(2026, 5, 20, 12, 1), "pk": 2},
    ]
    payload, n = _build_gzip_payload(rows)
    assert n == 2
    assert payload[:2] == b"\x1f\x8b"  # gzip magic

    decoded = gzip.decompress(payload).decode("utf-8").splitlines()
    assert len(decoded) == 2
    parsed = json.loads(decoded[0])
    # pk should be stripped from archive output
    assert "pk" not in parsed
    assert parsed["id"] == "a"
    assert parsed["created_at"] == "2026-05-20T12:00:00"


def test_build_gzip_payload_empty():
    payload, n = _build_gzip_payload([])
    assert n == 0
    assert gzip.decompress(payload) == b""


# ─── archive_day – DB + S3 mocked ────────────────────────────────────────────

def _make_cfg(enabled=True, bucket="test-bucket"):
    cfg = MagicMock()
    cfg.ARCHIVE_ENABLED = enabled
    cfg.ARCHIVE_HOT_DAYS = 35
    cfg.R2_BUCKET = bucket
    cfg.R2_ENDPOINT = "https://example.r2.cloudflarestorage.com"
    cfg.R2_ACCESS_KEY_ID = "ak"
    cfg.R2_SECRET_ACCESS_KEY = "sk"
    cfg.archive_configured = bool(enabled and bucket)
    return cfg


def test_archive_day_rejects_when_unconfigured():
    cfg = _make_cfg(enabled=False)
    with pytest.raises(RuntimeError, match="not configured"):
        archive_day(cfg, dt.date(2026, 5, 1))


def test_archive_day_uploads_then_deletes():
    """Happy path: rows fetched, uploaded to R2, then deleted from PG."""
    cfg = _make_cfg()
    day = dt.date(2026, 5, 1)

    # Build a fake session that returns two pages of rows, then empty.
    fake_rows_p1 = [
        {
            "id": f"row-{i}", "created_at": dt.datetime(2026, 5, 1, 10, i),
            "user_id": "u", "user_display_name": "U", "user_principal_name": "u@x",
            "app_id": "a", "app_display_name": "A", "ip_address": "1.1.1.1",
            "client_app_used": "B", "status": {"errorCode": 0}, "location": {},
            "device_detail": {}, "conditional_access_status": "n/a",
            "risk_detail": "none", "risk_level_aggregated": "none",
            "error_code": 0, "country": "US", "signin_event_type": "interactiveUser",
            "raw_event": {"id": f"row-{i}"}, "pk": i,
        }
        for i in range(1, 4)
    ]

    session = MagicMock()

    # Order of session.execute calls (3 rows < CHUNK_SIZE so generator
    # returns after one SELECT; DELETE batch returns rowcount<CHUNK so
    # delete loop runs once):
    #   1) upsert archive_log -> in_progress
    #   2) one SELECT chunk
    #   3) one DELETE batch (rowcount=3)
    #   4) final UPDATE archive_log -> success
    select_chunk = MagicMock(
        mappings=lambda r=fake_rows_p1: MagicMock(all=lambda: r)
    )
    upsert_in_progress = MagicMock()
    delete_first = MagicMock(rowcount=3)
    update_success = MagicMock()

    session.execute.side_effect = [
        upsert_in_progress,
        select_chunk,
        delete_first,
        update_success,
    ]

    fake_s3 = MagicMock()
    with patch("app.archive_service.get_session", return_value=session), \
         patch("app.archive_service.get_r2_client", return_value=fake_s3):
        result = archive_day(cfg, day)

    assert result["status"] == "success"
    assert result["rows"] == 3
    assert result["bytes"] > 0
    assert result["r2_key"] == "signins/2026/05/2026-05-01.jsonl.gz"

    # Verify put_object was called against our bucket+key
    fake_s3.put_object.assert_called_once()
    call_kwargs = fake_s3.put_object.call_args.kwargs
    assert call_kwargs["Bucket"] == "test-bucket"
    assert call_kwargs["Key"] == "signins/2026/05/2026-05-01.jsonl.gz"
    assert call_kwargs["ContentEncoding"] == "gzip"
    # The body should be valid gzipped JSONL
    decoded = gzip.decompress(call_kwargs["Body"]).decode("utf-8").splitlines()
    assert len(decoded) == 3
    assert json.loads(decoded[0])["id"] == "row-1"


def test_archive_day_empty_day_no_upload():
    """If a day has no rows we mark success without calling R2."""
    cfg = _make_cfg()
    session = MagicMock()
    upsert = MagicMock()
    empty_select = MagicMock(mappings=lambda: MagicMock(all=lambda: []))
    update = MagicMock()
    session.execute.side_effect = [upsert, empty_select, update]

    fake_s3 = MagicMock()
    with patch("app.archive_service.get_session", return_value=session), \
         patch("app.archive_service.get_r2_client", return_value=fake_s3):
        result = archive_day(cfg, dt.date(2026, 5, 1))

    assert result["status"] == "empty"
    assert result["rows"] == 0
    fake_s3.put_object.assert_not_called()


def test_archive_day_upload_failure_no_delete():
    """If R2 upload fails, the DELETE must NOT run."""
    cfg = _make_cfg()
    session = MagicMock()

    fake_row = {
        "id": "r", "created_at": dt.datetime(2026, 5, 1, 10),
        "user_id": None, "user_display_name": None, "user_principal_name": None,
        "app_id": None, "app_display_name": None, "ip_address": None,
        "client_app_used": None, "status": {}, "location": {}, "device_detail": {},
        "conditional_access_status": None, "risk_detail": None,
        "risk_level_aggregated": None, "error_code": 0, "country": None,
        "signin_event_type": "interactiveUser", "raw_event": {"id": "r"}, "pk": 1,
    }

    select1 = MagicMock(mappings=lambda: MagicMock(all=lambda: [fake_row]))
    select2 = MagicMock(mappings=lambda: MagicMock(all=lambda: []))

    session.execute.side_effect = [
        MagicMock(),  # upsert in_progress
        select1,
        select2,
        MagicMock(),  # error UPDATE inside the except handler
    ]

    fake_s3 = MagicMock()
    fake_s3.put_object.side_effect = RuntimeError("R2 is down")

    with patch("app.archive_service.get_session", return_value=session), \
         patch("app.archive_service.get_r2_client", return_value=fake_s3):
        with pytest.raises(RuntimeError, match="R2 is down"):
            archive_day(cfg, dt.date(2026, 5, 1))

    # Verify no DELETE was emitted (only the upsert, two selects, and the
    # error-handler UPDATE). DELETE SQL starts with the word DELETE.
    sql_text = " ".join(
        str(call.args[0]) for call in session.execute.call_args_list
        if call.args
    )
    assert "DELETE" not in sql_text.upper()


# ─── days_to_archive ─────────────────────────────────────────────────────────

def test_days_to_archive_uses_cutoff(monkeypatch):
    cfg = _make_cfg()
    session = MagicMock()
    fake_days = [
        {"d": dt.date(2026, 1, 1)},
        {"d": dt.date(2026, 1, 2)},
    ]
    session.execute.return_value.mappings.return_value.all.return_value = fake_days

    with patch("app.archive_service.get_session", return_value=session):
        days = days_to_archive(cfg)

    assert days == [dt.date(2026, 1, 1), dt.date(2026, 1, 2)]
    # The cutoff parameter should reflect hot-window subtraction
    call = session.execute.call_args
    assert "cutoff" in call.args[1]


# ─── run_archive_sweep ──────────────────────────────────────────────────────

def test_run_archive_sweep_disabled():
    cfg = _make_cfg(enabled=False)
    result = run_archive_sweep(cfg)
    assert result["status"] == "disabled"
    assert result["days_processed"] == 0


def test_run_archive_sweep_continues_on_per_day_error():
    """One failing day shouldn't block subsequent days."""
    cfg = _make_cfg()
    days = [dt.date(2026, 1, 1), dt.date(2026, 1, 2), dt.date(2026, 1, 3)]

    def fake_archive_day(_cfg, day):
        if day == dt.date(2026, 1, 2):
            raise RuntimeError("boom")
        return {"day": day.isoformat(), "rows": 10, "bytes": 100}

    with patch("app.archive_service.days_to_archive", return_value=days), \
         patch("app.archive_service.archive_day", side_effect=fake_archive_day):
        result = run_archive_sweep(cfg)

    assert result["days_processed"] == 2
    assert result["rows_archived"] == 20
    assert result["status"] == "partial"
    assert result["errors"] == [{"day": "2026-01-02", "error": "boom"}]


def test_load_archived_day_fetches_and_parses(monkeypatch):
    cfg = _make_cfg()
    # Build a gzipped JSONL payload matching what the archive emits.
    rows_in = [{"id": "a"}, {"id": "b"}, {"id": "c"}]
    payload, _ = _build_gzip_payload([dict(r) for r in rows_in])

    body = MagicMock()
    body.read.return_value = payload
    fake_s3 = MagicMock()
    fake_s3.get_object.return_value = {"Body": body}

    # Reset module-level day cache so test is hermetic.
    archive_service._DAY_CACHE.clear()
    archive_service._DAY_CACHE_ORDER.clear()

    with patch("app.archive_service.get_r2_client", return_value=fake_s3):
        rows = load_archived_day(cfg, dt.date(2026, 4, 1))

    assert [r["id"] for r in rows] == ["a", "b", "c"]
    fake_s3.get_object.assert_called_once_with(
        Bucket="test-bucket", Key="signins/2026/04/2026-04-01.jsonl.gz"
    )


def test_load_archived_day_uses_cache_on_second_call():
    cfg = _make_cfg()
    payload, _ = _build_gzip_payload([{"id": "x"}])

    body = MagicMock()
    body.read.return_value = payload
    fake_s3 = MagicMock()
    fake_s3.get_object.return_value = {"Body": body}

    archive_service._DAY_CACHE.clear()
    archive_service._DAY_CACHE_ORDER.clear()

    with patch("app.archive_service.get_r2_client", return_value=fake_s3):
        load_archived_day(cfg, dt.date(2026, 4, 2))
        load_archived_day(cfg, dt.date(2026, 4, 2))  # cache hit

    # R2 should only be hit once
    assert fake_s3.get_object.call_count == 1


def test_load_archived_day_cache_evicts_oldest():
    cfg = _make_cfg()
    payload, _ = _build_gzip_payload([{"id": "x"}])

    def fresh_body():
        body = MagicMock()
        body.read.return_value = payload
        return body

    fake_s3 = MagicMock()
    fake_s3.get_object.side_effect = lambda **_: {"Body": fresh_body()}

    archive_service._DAY_CACHE.clear()
    archive_service._DAY_CACHE_ORDER.clear()

    with patch("app.archive_service.get_r2_client", return_value=fake_s3):
        # Cache max is 2; loading 3 different days should evict the first.
        load_archived_day(cfg, dt.date(2026, 4, 1))
        load_archived_day(cfg, dt.date(2026, 4, 2))
        load_archived_day(cfg, dt.date(2026, 4, 3))

    assert "2026-04-01" not in archive_service._DAY_CACHE
    assert "2026-04-02" in archive_service._DAY_CACHE
    assert "2026-04-03" in archive_service._DAY_CACHE


def test_run_archive_sweep_max_days_limit():
    cfg = _make_cfg()
    days = [dt.date(2026, 1, i) for i in range(1, 11)]  # 10 days

    with patch("app.archive_service.days_to_archive", return_value=days), \
         patch("app.archive_service.archive_day",
               return_value={"rows": 1, "bytes": 1}) as mock_arc:
        result = run_archive_sweep(cfg, max_days=3)

    assert result["days_processed"] == 3
    assert mock_arc.call_count == 3
