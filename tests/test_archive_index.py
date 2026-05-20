"""Unit tests for archive_index — uses a real DuckDB file in a tmp dir."""
from __future__ import annotations

import datetime as dt
import os
from unittest.mock import MagicMock

import pytest

# DuckDB ships unsigned .so binaries that macOS Gatekeeper rejects on
# Python 3.14. Skip locally; these run on Linux in Docker / CI.
pytest.importorskip("duckdb")

from app import archive_index


@pytest.fixture
def cfg(tmp_path):
    """Throwaway DuckDB file per test."""
    archive_index.close()
    fake = MagicMock()
    fake.ARCHIVE_INDEX_PATH = str(tmp_path / "test_index.duckdb")
    yield fake
    archive_index.close()


def _row(id_: str, day_iso: str, hour: int, **overrides) -> dict:
    base = {
        "id": id_,
        "created_at": f"{day_iso}T{hour:02d}:00:00",
        "user_principal_name": "alice@example.com",
        "user_display_name": "Alice",
        "app_display_name": "Microsoft Teams",
        "ip_address": "10.0.0.1",
        "country": "US",
        "error_code": 0,
        "signin_event_type": "interactiveUser",
    }
    base.update(overrides)
    return base


def test_index_day_inserts_rows(cfg):
    day = dt.date(2026, 4, 1)
    rows = [_row(f"r{i}", day.isoformat(), i) for i in range(3)]
    n = archive_index.index_day(cfg, day, rows)
    assert n == 3

    stats = archive_index.get_index_stats(cfg)
    assert stats["rows"] == 3
    assert stats["days"] == 1


def test_index_day_is_idempotent(cfg):
    day = dt.date(2026, 4, 2)
    rows = [_row(f"r{i}", day.isoformat(), i) for i in range(5)]
    archive_index.index_day(cfg, day, rows)
    archive_index.index_day(cfg, day, rows)  # rerun same day

    stats = archive_index.get_index_stats(cfg)
    # Should still be 5, not 10 (delete-then-insert under the hood)
    assert stats["rows"] == 5


def test_search_filters_upn(cfg):
    day = dt.date(2026, 4, 3)
    rows = [
        _row("a", day.isoformat(), 1, user_principal_name="alice@example.com"),
        _row("b", day.isoformat(), 2, user_principal_name="bob@example.com"),
        _row("c", day.isoformat(), 3, user_principal_name="carol@school.edu"),
    ]
    archive_index.index_day(cfg, day, rows)

    res = archive_index.search(cfg, user_principal_name="example.com")
    assert res["total"] == 2
    assert {r["id"] for r in res["results"]} == {"a", "b"}


def test_search_filters_date_range(cfg):
    archive_index.index_day(cfg, dt.date(2026, 1, 1), [_row("a", "2026-01-01", 12)])
    archive_index.index_day(cfg, dt.date(2026, 2, 1), [_row("b", "2026-02-01", 12)])
    archive_index.index_day(cfg, dt.date(2026, 3, 1), [_row("c", "2026-03-01", 12)])

    res = archive_index.search(
        cfg, date_from=dt.date(2026, 2, 1), date_to=dt.date(2026, 2, 28)
    )
    assert res["total"] == 1
    assert res["results"][0]["id"] == "b"


def test_search_filters_error_code_and_type(cfg):
    day = dt.date(2026, 4, 4)
    rows = [
        _row("ok", day.isoformat(), 1, error_code=0, signin_event_type="interactiveUser"),
        _row("fail", day.isoformat(), 2, error_code=50126, signin_event_type="interactiveUser"),
        _row("svc", day.isoformat(), 3, error_code=0, signin_event_type="servicePrincipal"),
    ]
    archive_index.index_day(cfg, day, rows)

    only_fails = archive_index.search(cfg, error_code=50126)
    assert only_fails["total"] == 1
    assert only_fails["results"][0]["id"] == "fail"

    only_sp = archive_index.search(cfg, signin_event_type="servicePrincipal")
    assert only_sp["total"] == 1
    assert only_sp["results"][0]["id"] == "svc"


def test_search_keyword_hits_multiple_columns(cfg):
    day = dt.date(2026, 4, 5)
    rows = [
        _row("a", day.isoformat(), 1, user_principal_name="alice@x.com"),
        _row("b", day.isoformat(), 2, app_display_name="alice-portal"),
        _row("c", day.isoformat(), 3, ip_address="10.0.0.99"),
    ]
    archive_index.index_day(cfg, day, rows)

    res = archive_index.search(cfg, keyword="alice")
    # 'alice' matches UPN of 'a' and app name of 'b'
    assert res["total"] == 2
    assert {r["id"] for r in res["results"]} == {"a", "b"}


def test_search_paginates(cfg):
    day = dt.date(2026, 4, 6)
    rows = [_row(f"r{i:03d}", day.isoformat(), i % 24) for i in range(75)]
    archive_index.index_day(cfg, day, rows)

    p1 = archive_index.search(cfg, per_page=50, page=1)
    p2 = archive_index.search(cfg, per_page=50, page=2)
    assert p1["total"] == 75
    assert len(p1["results"]) == 50
    assert len(p2["results"]) == 25
    assert p1["pages"] == 2


def test_index_day_skips_rows_without_created_at(cfg):
    day = dt.date(2026, 4, 7)
    rows = [
        _row("good", day.isoformat(), 1),
        {**_row("bad", day.isoformat(), 2), "created_at": None},
    ]
    n = archive_index.index_day(cfg, day, rows)
    assert n == 1


def test_index_file_created_on_disk(cfg):
    archive_index.index_day(cfg, dt.date(2026, 4, 8), [_row("a", "2026-04-08", 1)])
    assert os.path.exists(cfg.ARCHIVE_INDEX_PATH)
    assert os.path.getsize(cfg.ARCHIVE_INDEX_PATH) > 0
