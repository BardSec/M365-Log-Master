"""Unit tests for anomaly heuristic logic (pure Python, no DB)."""
from __future__ import annotations

from datetime import datetime


# ─── Anomaly heuristic helper logic (tested standalone) ─────────────────────


def _is_impossible_travel(
    events: list[dict],
    max_seconds: int = 3600,
) -> list[dict]:
    """
    Simulate the impossible-travel heuristic in pure Python for testing.

    Given a list of events (sorted by created_at) for a single user,
    return events where the country changed within `max_seconds` of the
    previous event.
    """
    flagged = []
    prev = None
    for ev in sorted(events, key=lambda e: e["created_at"]):
        if prev is not None:
            delta = (ev["created_at"] - prev["created_at"]).total_seconds()
            if (
                prev.get("country")
                and ev.get("country")
                and prev["country"] != ev["country"]
                and delta < max_seconds
            ):
                flagged.append(ev)
        prev = ev
    return flagged


def _new_ips_for_user(
    recent_events: list[dict],
    historical_ips: set[str],
) -> list[str]:
    """Return IPs in recent_events that are NOT in historical_ips."""
    recent_ips = {ev["ip_address"] for ev in recent_events if ev.get("ip_address")}
    return sorted(recent_ips - historical_ips)


def _unfamiliar_countries(
    recent_events: list[dict],
    historical_countries: set[str],
) -> list[str]:
    """Return countries in recent_events not seen in historical window."""
    recent = {ev["country"] for ev in recent_events if ev.get("country")}
    return sorted(recent - historical_countries)


# ─── Impossible travel ────────────────────────────────────────────────────────

def test_impossible_travel_detected():
    events = [
        {"created_at": datetime(2024, 6, 1, 10, 0, 0), "country": "US", "ip_address": "1.1.1.1"},
        # 20 minutes later from a different country → impossible
        {"created_at": datetime(2024, 6, 1, 10, 20, 0), "country": "RU", "ip_address": "2.2.2.2"},
    ]
    flagged = _is_impossible_travel(events)
    assert len(flagged) == 1
    assert flagged[0]["country"] == "RU"


def test_impossible_travel_same_country_not_flagged():
    events = [
        {"created_at": datetime(2024, 6, 1, 10, 0, 0), "country": "US"},
        {"created_at": datetime(2024, 6, 1, 10, 20, 0), "country": "US"},
    ]
    assert _is_impossible_travel(events) == []


def test_impossible_travel_long_gap_not_flagged():
    events = [
        {"created_at": datetime(2024, 6, 1, 10, 0, 0), "country": "US"},
        # 2 hours later – outside threshold
        {"created_at": datetime(2024, 6, 1, 12, 0, 0), "country": "GB"},
    ]
    assert _is_impossible_travel(events, max_seconds=3600) == []


def test_impossible_travel_no_country_not_flagged():
    events = [
        {"created_at": datetime(2024, 6, 1, 10, 0, 0), "country": None},
        {"created_at": datetime(2024, 6, 1, 10, 5, 0), "country": "US"},
    ]
    assert _is_impossible_travel(events) == []


def test_impossible_travel_multiple_hops():
    events = [
        {"created_at": datetime(2024, 6, 1, 10, 0, 0), "country": "US"},
        {"created_at": datetime(2024, 6, 1, 10, 10, 0), "country": "BR"},  # flagged
        {"created_at": datetime(2024, 6, 1, 10, 20, 0), "country": "CN"},  # flagged
    ]
    flagged = _is_impossible_travel(events)
    assert len(flagged) == 2


# ─── New IP heuristic ─────────────────────────────────────────────────────────

def test_new_ip_detected():
    recent = [{"ip_address": "10.0.0.1"}, {"ip_address": "192.168.1.1"}]
    historical = {"192.168.1.1"}
    new = _new_ips_for_user(recent, historical)
    assert new == ["10.0.0.1"]


def test_no_new_ip_when_all_known():
    recent = [{"ip_address": "10.0.0.1"}]
    historical = {"10.0.0.1"}
    assert _new_ips_for_user(recent, historical) == []


def test_new_ip_ignores_none():
    recent = [{"ip_address": None}, {"ip_address": "10.0.0.5"}]
    historical = set()
    new = _new_ips_for_user(recent, historical)
    assert new == ["10.0.0.5"]


# ─── Unfamiliar country heuristic ────────────────────────────────────────────

def test_unfamiliar_country_detected():
    recent = [{"country": "NG"}, {"country": "US"}]
    historical = {"US", "CA"}
    unfamiliar = _unfamiliar_countries(recent, historical)
    assert unfamiliar == ["NG"]


def test_no_unfamiliar_when_all_known():
    recent = [{"country": "US"}]
    historical = {"US"}
    assert _unfamiliar_countries(recent, historical) == []


def test_unfamiliar_country_ignores_none():
    recent = [{"country": None}, {"country": "BR"}]
    historical = set()
    assert _unfamiliar_countries(recent, historical) == ["BR"]


# ─── Cursor / sync time ───────────────────────────────────────────────────────

def test_parse_dt_edge_cases():
    from app.sync_service import _parse_dt

    assert _parse_dt("2024-12-31T23:59:59Z") == datetime(2024, 12, 31, 23, 59, 59)
    assert _parse_dt("2024-01-01T00:00:00.000000Z") == datetime(2024, 1, 1, 0, 0, 0, 0)
    assert _parse_dt("") is None
    assert _parse_dt(None) is None
