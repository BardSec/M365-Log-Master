"""Unit tests for GraphClient – all HTTP calls are mocked."""
from __future__ import annotations

import time
from datetime import datetime, timezone
from unittest.mock import MagicMock, patch

import pytest

from app.graph_client import GraphClient, GraphAuthError, _fmt_dt


# ─── Helpers ─────────────────────────────────────────────────────────────────

def _make_client():
    return GraphClient(
        tenant_id="tenant123",
        client_id="client123",
        client_secret="secret123",
        scope="https://graph.microsoft.com/.default",
    )


def _mock_token(client):
    """Pre-set client._app so _get_msal_app() returns our mock directly."""
    mock_app = MagicMock()
    mock_app.acquire_token_silent.return_value = None
    mock_app.acquire_token_for_client.return_value = {"access_token": "tok_abc"}
    client._app = mock_app  # _get_msal_app checks self._app; bypasses MSAL init
    return mock_app


# ─── _fmt_dt ─────────────────────────────────────────────────────────────────

def test_fmt_dt_naive():
    dt = datetime(2024, 6, 15, 10, 30, 0)
    assert _fmt_dt(dt) == "2024-06-15T10:30:00Z"


def test_fmt_dt_aware():
    dt = datetime(2024, 6, 15, 10, 30, 0, tzinfo=timezone.utc)
    assert _fmt_dt(dt) == "2024-06-15T10:30:00Z"


# ─── Token acquisition ───────────────────────────────────────────────────────

def test_get_token_success():
    client = _make_client()
    mock_app = _mock_token(client)
    token = client.get_token()
    assert token == "tok_abc"


def test_get_token_failure_raises():
    client = _make_client()
    mock_app = MagicMock()
    mock_app.acquire_token_silent.return_value = None
    mock_app.acquire_token_for_client.return_value = {
        "error": "invalid_client",
        "error_description": "client secret wrong",
    }
    client._app = mock_app

    with pytest.raises(GraphAuthError, match="client secret wrong"):
        client.get_token()


# ─── _get / retry logic ──────────────────────────────────────────────────────

def test_get_returns_on_200(requests_mock):
    client = _make_client()
    _mock_token(client)

    requests_mock.get(
        "https://graph.microsoft.com/beta/auditLogs/signIns",
        json={"value": [{"id": "evt1"}]},
        status_code=200,
    )
    data = client._get("https://graph.microsoft.com/beta/auditLogs/signIns")
    assert data["value"][0]["id"] == "evt1"


def test_get_retries_on_429(requests_mock):
    client = _make_client()
    _mock_token(client)

    # First call returns 429, second returns 200
    responses = [
        {"status_code": 429, "headers": {"Retry-After": "0"}, "json": {}},
        {"status_code": 200, "json": {"value": [{"id": "evt2"}]}},
    ]
    requests_mock.get(
        "https://graph.microsoft.com/beta/auditLogs/signIns",
        [
            {"status_code": 429, "headers": {"Retry-After": "0"}, "json": {}},
            {"status_code": 200, "json": {"value": [{"id": "evt2"}]}},
        ],
    )
    data = client._get("https://graph.microsoft.com/beta/auditLogs/signIns")
    assert data["value"][0]["id"] == "evt2"


# ─── fetch_sign_in_logs pagination ───────────────────────────────────────────

def test_fetch_follows_next_link(requests_mock):
    client = _make_client()
    _mock_token(client)

    url = "https://graph.microsoft.com/beta/auditLogs/signIns"
    next_url = url + "?$skiptoken=abc"

    requests_mock.get(
        url,
        json={
            "value": [{"id": "p1e1"}, {"id": "p1e2"}],
            "@odata.nextLink": next_url,
        },
    )
    requests_mock.get(
        next_url,
        json={"value": [{"id": "p2e1"}]},
    )

    from_dt = datetime(2024, 1, 1, 0, 0, 0)
    to_dt = datetime(2024, 1, 2, 0, 0, 0)
    events = list(client.fetch_sign_in_logs(from_dt, to_dt))

    assert len(events) == 3
    assert events[0]["id"] == "p1e1"
    assert events[2]["id"] == "p2e1"


def test_fetch_empty_result(requests_mock):
    client = _make_client()
    _mock_token(client)

    requests_mock.get(
        "https://graph.microsoft.com/beta/auditLogs/signIns",
        json={"value": []},
    )

    from_dt = datetime(2024, 1, 1)
    to_dt = datetime(2024, 1, 2)
    events = list(client.fetch_sign_in_logs(from_dt, to_dt))
    assert events == []
