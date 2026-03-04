"""Dummy data for demo mode – no database or Microsoft credentials required."""
from __future__ import annotations

import math
import random
from datetime import datetime, timedelta
from typing import Any

# ── Fixtures ──────────────────────────────────────────────────────────────────

_USERS = [
    ("alice.chen@contoso.com",     "Alice Chen",     "US", "40.112.72.205"),
    ("bob.martinez@contoso.com",   "Bob Martinez",   "US", "13.107.6.152"),
    ("charlie.nguyen@contoso.com", "Charlie Nguyen", "GB", "51.140.148.0"),
    ("diana.kowalski@contoso.com", "Diana Kowalski", "DE", "40.74.28.0"),
    ("frank.okonkwo@contoso.com",  "Frank Okonkwo",  "NG", "197.211.58.0"),
    ("grace.tanaka@contoso.com",   "Grace Tanaka",   "JP", "122.1.115.0"),
    ("henry.silva@contoso.com",    "Henry Silva",    "BR", "177.71.207.0"),
    ("ivan.petrov@external.ru",    "Ivan Petrov",    "RU", "195.22.26.248"),
]

_APPS = [
    "Microsoft Teams",
    "SharePoint Online",
    "Exchange Online",
    "Azure Portal",
    "Microsoft 365 Admin",
    "OneDrive for Business",
    "Power BI",
]

_CLIENT_APPS = [
    "Browser",
    "Mobile Apps and Desktop clients",
    "Exchange ActiveSync",
    "Other clients",
]

# (error_code, failure_reason, additional_details)
_SUCCESS = (0, "None", "")
_ERRORS = [
    (50076, "UserStrongAuthClientAuthNRequired", "MFA required by policy"),
    (53003, "BlockedByConditionalAccess",        "Blocked by conditional access"),
    (70011, "InvalidScopeError",                 "Invalid scope requested"),
    (50057, "UserDisabled",                      "User account is disabled"),
    (50126, "InvalidUserNameOrPassword",         "Invalid username or password"),
]


def _now() -> datetime:
    return datetime.utcnow()


def _make_event(idx: int, minutes_ago: int) -> dict[str, Any]:
    rng = random.Random(idx)
    u = _USERS[idx % len(_USERS)]
    a = _APPS[idx % len(_APPS)]
    c = _CLIENT_APPS[idx % len(_CLIENT_APPS)]
    err = _ERRORS[idx % len(_ERRORS)] if rng.random() < 0.12 else _SUCCESS
    ts = _now() - timedelta(minutes=minutes_ago)
    ca_status = "failure" if err[0] != 0 else "success"
    risk = "high" if u[0].endswith(".ru") else "none"

    return {
        "id": f"demo-{idx:06d}",
        "created_at": ts,
        "user_principal_name": u[0],
        "user_display_name": u[1],
        "app_display_name": a,
        "ip_address": u[3],
        "client_app_used": c,
        "error_code": err[0],
        "country": u[2],
        "conditional_access_status": ca_status,
        "risk_level_aggregated": risk,
        "status": {"errorCode": err[0], "failureReason": err[1], "additionalDetails": err[2]},
        "location": {
            "city": "Demo City",
            "state": "Demo State",
            "countryOrRegion": u[2],
            "geoCoordinates": {},
        },
        "device_detail": {
            "deviceId": f"device-{idx % 20:03d}",
            "displayName": f"LAPTOP-{idx % 20:03d}",
            "operatingSystem": "Windows 10",
            "browser": "Chrome 122.0",
            "isCompliant": True,
            "isManaged": True,
            "trustType": "Azure AD joined",
        },
        "raw_event": {
            "id": f"demo-{idx:06d}",
            "createdDateTime": ts.strftime("%Y-%m-%dT%H:%M:%SZ"),
            "userPrincipalName": u[0],
            "userDisplayName": u[1],
            "appDisplayName": a,
            "ipAddress": u[3],
            "clientAppUsed": c,
            "status": {"errorCode": err[0], "failureReason": err[1]},
            "location": {"countryOrRegion": u[2]},
            "conditionalAccessStatus": ca_status,
            "riskLevelAggregated": risk,
        },
    }


def _build_events(hours: int) -> list[dict[str, Any]]:
    """Generate events spanning the requested window, newest first."""
    minutes_total = hours * 60
    count = min(hours * 40, 1200)
    events = [
        _make_event(i, int(i / count * minutes_total))
        for i in range(count)
    ]
    return events  # i=0 → minutes_ago=0 (newest), i=count-1 → oldest


# ── Public API (mirrors queries.py + sync_service.py) ────────────────────────

def search_events(
    keyword: str | None = None,
    user_principal_name: str | None = None,
    ip_address: str | None = None,
    app_display_name: str | None = None,
    error_code: int | None = None,
    country: str | None = None,
    from_dt: datetime | None = None,
    to_dt: datetime | None = None,
    page: int = 1,
    per_page: int = 50,
    sort: str = "created_at",
    order: str = "desc",
) -> dict:
    events = _build_events(24)

    # Apply filters
    if keyword:
        kw = keyword.lower()
        events = [
            e for e in events
            if kw in e["user_principal_name"].lower()
            or kw in e["ip_address"].lower()
            or kw in e["app_display_name"].lower()
            or kw in e["user_display_name"].lower()
        ]
    if user_principal_name:
        upn = user_principal_name.lower()
        events = [e for e in events if upn in e["user_principal_name"].lower()]
    if ip_address:
        events = [e for e in events if e["ip_address"] == ip_address]
    if app_display_name:
        app = app_display_name.lower()
        events = [e for e in events if app in e["app_display_name"].lower()]
    if error_code is not None:
        events = [e for e in events if e["error_code"] == error_code]
    if country:
        c = country.lower()
        events = [e for e in events if c in e["country"].lower()]
    if from_dt:
        events = [e for e in events if e["created_at"] >= from_dt]
    if to_dt:
        events = [e for e in events if e["created_at"] <= to_dt]

    total = len(events)
    offset = (page - 1) * per_page
    page_events = events[offset: offset + per_page]

    return {
        "total": total,
        "page": page,
        "per_page": per_page,
        "pages": math.ceil(total / per_page) if total else 0,
        "results": page_events,
    }


def get_event_by_id(event_id: str) -> dict | None:
    # Match by demo ID or return a sensible default event
    try:
        idx = int(event_id.replace("demo-", ""))
    except (ValueError, AttributeError):
        idx = 0
    return _make_event(idx, minutes_ago=42)


def get_dashboard_metrics(hours: int = 24) -> dict:
    rng = random.Random(hours)

    total = hours * 52 + rng.randint(0, 50)
    failures = int(total * 0.072)
    failure_rate = round(failures / total * 100, 1) if total else 0.0

    top_users = [
        {"user_principal_name": u[0], "cnt": rng.randint(3, 25)}
        for u in _USERS
        if rng.random() > 0.3
    ]
    top_users = sorted(top_users, key=lambda x: x["cnt"], reverse=True)[:10]

    top_ips = [
        {"ip_address": u[3], "cnt": rng.randint(2, 18)}
        for u in _USERS
        if rng.random() > 0.3
    ]
    top_ips = sorted(top_ips, key=lambda x: x["cnt"], reverse=True)[:10]

    timeline = _gen_timeline(hours)

    return {
        "total": total,
        "failures": failures,
        "failure_rate": failure_rate,
        "top_users_by_failures": top_users,
        "top_ips_by_failures": top_ips,
        "timeline": timeline,
    }


def _gen_timeline(hours: int) -> list[dict]:
    rng = random.Random(hours + 1)
    now = _now().replace(minute=0, second=0, microsecond=0)

    if hours <= 24:
        bucket_td = timedelta(hours=1)
        n = hours
    elif hours <= 168:
        bucket_td = timedelta(hours=6)
        n = hours // 6
    else:
        bucket_td = timedelta(days=1)
        n = hours // 24

    result = []
    for i in range(n):
        bucket_time = now - bucket_td * (n - 1 - i)
        total = rng.randint(25, 90)
        fail = rng.randint(1, max(1, total // 10))
        result.append({
            "bucket": bucket_time.isoformat(),
            "total": total,
            "failures": fail,
        })
    return result


def get_anomalies(hours: int = 24, lookback_days: int = 30) -> list[dict]:
    now = _now()
    anomalies = [
        {
            "id": "demo-000007",
            "created_at": (now - timedelta(hours=1, minutes=12)).isoformat(),
            "user_principal_name": "ivan.petrov@external.ru",
            "ip_address": "195.22.26.248",
            "app_display_name": "Azure Portal",
            "country": "RU",
            "reason": "unfamiliar_country",
        },
        {
            "id": "demo-000015",
            "created_at": (now - timedelta(hours=2, minutes=44)).isoformat(),
            "user_principal_name": "alice.chen@contoso.com",
            "ip_address": "103.27.125.0",
            "app_display_name": "Microsoft Teams",
            "country": "CN",
            "reason": "new_ip_for_user",
        },
        {
            "id": "demo-000031",
            "created_at": (now - timedelta(hours=3, minutes=5)).isoformat(),
            "user_principal_name": "charlie.nguyen@contoso.com",
            "ip_address": "51.140.148.0",
            "app_display_name": "SharePoint Online",
            "country": "AU",
            "reason": "impossible_travel",
        },
        {
            "id": "demo-000048",
            "created_at": (now - timedelta(hours=4, minutes=33)).isoformat(),
            "user_principal_name": "diana.kowalski@contoso.com",
            "ip_address": "40.74.28.0",
            "app_display_name": "Exchange Online",
            "country": "DE",
            "reason": "repeated_ca_failure",
        },
        {
            "id": "demo-000055",
            "created_at": (now - timedelta(hours=5, minutes=17)).isoformat(),
            "user_principal_name": "ivan.petrov@external.ru",
            "ip_address": "195.22.26.248",
            "app_display_name": "Microsoft 365 Admin",
            "country": "RU",
            "reason": "repeated_ca_failure",
        },
        {
            "id": "demo-000062",
            "created_at": (now - timedelta(hours=6, minutes=8)).isoformat(),
            "user_principal_name": "henry.silva@contoso.com",
            "ip_address": "177.71.207.0",
            "app_display_name": "Power BI",
            "country": "US",
            "reason": "new_ip_for_user",
        },
    ]
    return anomalies


def get_new_ips_per_user(hours: int = 24, lookback_days: int = 30) -> list[dict]:
    return [
        {"user_principal_name": "alice.chen@contoso.com",     "new_ip_count": 2},
        {"user_principal_name": "ivan.petrov@external.ru",    "new_ip_count": 4},
        {"user_principal_name": "henry.silva@contoso.com",    "new_ip_count": 1},
        {"user_principal_name": "charlie.nguyen@contoso.com", "new_ip_count": 1},
    ]


def get_last_sync_info() -> dict:
    now = _now()
    return {
        "last_sync_time": (now - timedelta(minutes=23)).isoformat(),
        "last_run": {
            "started_at": (now - timedelta(minutes=23, seconds=4)).isoformat(),
            "finished_at": (now - timedelta(minutes=23)).isoformat(),
            "status": "success",
            "fetched_count": 247,
            "inserted_count": 241,
            "duration_seconds": 3.8,
            "error_message": None,
        },
    }


def get_sync_history(limit: int = 20) -> list[dict]:
    now = _now()
    history = []
    for i in range(min(limit, 10)):
        offset_h = i + 1
        started = now - timedelta(hours=offset_h, minutes=3)
        finished = started + timedelta(seconds=3 + i * 0.3)
        history.append({
            "id": 10 - i,
            "started_at": started.isoformat(),
            "finished_at": finished.isoformat(),
            "status": "success" if i != 3 else "error",
            "fetched_count": 247 - i * 12,
            "inserted_count": 241 - i * 12,
            "duration_seconds": round(3.8 + i * 0.3, 1),
            "error_message": "Graph API timeout" if i == 3 else None,
        })
    return history
