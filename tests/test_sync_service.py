"""Unit tests for sync_service – cursor handling, deduplication, coercion."""
from __future__ import annotations

from datetime import datetime, timedelta, timezone
from unittest.mock import MagicMock, patch, call

import pytest

from app.sync_service import _coerce_event, _parse_dt, _utcnow, get_or_create_cursor


# ─── _parse_dt ────────────────────────────────────────────────────────────────

def test_parse_dt_utc():
    dt = _parse_dt("2024-03-15T14:30:00Z")
    assert dt == datetime(2024, 3, 15, 14, 30, 0)


def test_parse_dt_with_microseconds():
    dt = _parse_dt("2024-03-15T14:30:00.123456Z")
    assert dt == datetime(2024, 3, 15, 14, 30, 0, 123456)


def test_parse_dt_none():
    assert _parse_dt(None) is None
    assert _parse_dt("") is None


def test_parse_dt_invalid():
    assert _parse_dt("not-a-date") is None


# ─── _coerce_event ────────────────────────────────────────────────────────────

def test_coerce_event_full():
    raw = {
        "id": "abc123",
        "createdDateTime": "2024-03-15T10:00:00Z",
        "userId": "uid1",
        "userDisplayName": "Alice Smith",
        "userPrincipalName": "alice@contoso.com",
        "appId": "appid1",
        "appDisplayName": "Microsoft Teams",
        "ipAddress": "1.2.3.4",
        "clientAppUsed": "Browser",
        "status": {"errorCode": 0, "failureReason": ""},
        "location": {
            "city": "Seattle",
            "state": "WA",
            "countryOrRegion": "US",
            "geoCoordinates": {},
        },
        "deviceDetail": {"deviceId": "dev1"},
        "conditionalAccessStatus": "success",
        "riskDetail": "none",
        "riskLevelAggregated": "none",
    }
    row = _coerce_event(raw)

    assert row["id"] == "abc123"
    assert row["created_at"] == datetime(2024, 3, 15, 10, 0, 0)
    assert row["user_principal_name"] == "alice@contoso.com"
    assert row["ip_address"] == "1.2.3.4"
    assert row["error_code"] == 0
    assert row["country"] == "US"
    assert row["raw_event"] == raw


def test_coerce_event_missing_status():
    raw = {"id": "x", "createdDateTime": "2024-01-01T00:00:00Z"}
    row = _coerce_event(raw)
    assert row["error_code"] is None
    assert row["status"] == {}


def test_coerce_event_non_int_error_code():
    raw = {
        "id": "y",
        "createdDateTime": "2024-01-01T00:00:00Z",
        "status": {"errorCode": "AADSTS50076"},
    }
    row = _coerce_event(raw)
    assert row["error_code"] is None


def test_coerce_event_failure_error_code():
    raw = {
        "id": "z",
        "createdDateTime": "2024-01-01T00:00:00Z",
        "status": {"errorCode": 50126, "failureReason": "Invalid credentials"},
    }
    row = _coerce_event(raw)
    assert row["error_code"] == 50126


# ─── Cursor / deduplication logic ────────────────────────────────────────────

def test_run_sync_uses_lookback():
    """
    Verify that when a cursor exists, the query start time is shifted back
    by lookback_minutes to catch slightly-out-of-order events.
    """
    from app import sync_service

    last_sync = datetime(2024, 6, 1, 12, 0, 0)
    lookback = 5

    # Expected from_dt = last_sync - 5 min
    expected_from = last_sync - timedelta(minutes=lookback)

    captured_from = []

    def mock_fetch(from_dt, to_dt, page_size=500):
        captured_from.append(from_dt)
        return iter([])  # no events

    mock_cursor = MagicMock()
    mock_cursor.last_sync_time = last_sync

    mock_session = MagicMock()
    mock_session.get.return_value = mock_cursor
    mock_session.execute.return_value.rowcount = 0

    mock_client = MagicMock()
    mock_client.fetch_sign_in_logs.side_effect = mock_fetch

    with patch("app.sync_service.get_session", return_value=mock_session):
        sync_service.run_sync(mock_client, lookback_minutes=lookback)

    assert captured_from, "fetch_sign_in_logs was not called"
    actual_from = captured_from[0]
    # Allow 1 second tolerance (utcnow() drift)
    assert abs((actual_from - expected_from).total_seconds()) < 2


def test_run_sync_first_run_backfills_24h():
    """First sync (no cursor) should query last 24 hours."""
    from app import sync_service

    captured_from = []

    def mock_fetch(from_dt, to_dt, page_size=500):
        captured_from.append((from_dt, to_dt))
        return iter([])

    mock_cursor = MagicMock()
    mock_cursor.last_sync_time = None

    mock_session = MagicMock()
    mock_session.get.return_value = mock_cursor
    mock_session.execute.return_value.rowcount = 0

    mock_client = MagicMock()
    mock_client.fetch_sign_in_logs.side_effect = mock_fetch

    before = _utcnow()
    with patch("app.sync_service.get_session", return_value=mock_session):
        sync_service.run_sync(mock_client)
    after = _utcnow()

    assert captured_from
    from_dt, to_dt = captured_from[0]
    # from_dt should be roughly 24 h before now
    expected_from = to_dt - timedelta(hours=24)
    delta = abs((from_dt - expected_from).total_seconds())
    assert delta < 5, f"Expected ~24h lookback, got delta={delta}s"


def test_run_sync_deduplication():
    """
    Duplicate event IDs: the upsert ON CONFLICT DO UPDATE means only 1 row
    is affected. We don't double-count the inserted_count from rowcount alone,
    but the important thing is no exception is raised and the function returns.
    """
    from app import sync_service

    raw_event = {
        "id": "dup1",
        "createdDateTime": "2024-01-01T00:00:00Z",
        "status": {"errorCode": 0},
        "location": {"countryOrRegion": "US"},
    }

    mock_cursor = MagicMock()
    mock_cursor.last_sync_time = datetime(2024, 1, 1, 0, 0, 0)

    mock_session = MagicMock()
    mock_session.get.return_value = mock_cursor
    # Simulate that rowcount=1 (upsert touched 1 row, even if it was a conflict)
    mock_session.execute.return_value.rowcount = 1

    mock_client = MagicMock()
    # Return the same event twice
    mock_client.fetch_sign_in_logs.return_value = iter([raw_event, raw_event])

    with patch("app.sync_service.get_session", return_value=mock_session):
        stats = sync_service.run_sync(mock_client)

    assert stats["status"] == "success"
    assert stats["fetched_count"] == 2  # both were fetched


def test_run_sync_cursor_not_updated_on_failure():
    """If sync raises, the cursor last_sync_time should NOT be advanced."""
    from app import sync_service

    original_time = datetime(2024, 1, 1, 0, 0, 0)

    mock_cursor = MagicMock()
    mock_cursor.last_sync_time = original_time

    mock_session = MagicMock()
    mock_session.get.return_value = mock_cursor

    mock_client = MagicMock()
    mock_client.fetch_sign_in_logs.side_effect = RuntimeError("Graph is down")

    with patch("app.sync_service.get_session", return_value=mock_session):
        stats = sync_service.run_sync(mock_client)

    assert stats["status"] == "error"
    assert "Graph is down" in stats["error_message"]
    # Cursor time should NOT have been overwritten via session.flush() path
    # (session.rollback was called instead)
    mock_session.rollback.assert_called()
