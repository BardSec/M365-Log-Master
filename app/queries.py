"""
SQL query helpers for search and dashboard/anomaly metrics.

All heavy lifting is done in PostgreSQL; Python only shapes the results.
"""
from __future__ import annotations

import math
from datetime import datetime, timedelta
from typing import Any

from sqlalchemy import text

from .extensions import get_session

# ─── Search ───────────────────────────────────────────────────────────────────


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
    """
    Flexible search over sign_in_events. Returns paginated results.
    Uses trigram GIN index for keyword search (pg_trgm).
    """
    allowed_sorts = {
        "created_at", "user_principal_name", "ip_address",
        "app_display_name", "error_code", "country",
    }
    if sort not in allowed_sorts:
        sort = "created_at"
    if order not in ("asc", "desc"):
        order = "desc"

    conditions: list[str] = []
    params: dict[str, Any] = {}

    if keyword:
        conditions.append(
            "(user_principal_name ILIKE :kw OR ip_address ILIKE :kw "
            "OR app_display_name ILIKE :kw OR user_display_name ILIKE :kw)"
        )
        params["kw"] = f"%{keyword}%"

    if user_principal_name:
        conditions.append("user_principal_name ILIKE :upn")
        params["upn"] = f"%{user_principal_name}%"

    if ip_address:
        conditions.append("ip_address = :ip")
        params["ip"] = ip_address

    if app_display_name:
        conditions.append("app_display_name ILIKE :app")
        params["app"] = f"%{app_display_name}%"

    if error_code is not None:
        conditions.append("error_code = :ec")
        params["ec"] = error_code

    if country:
        conditions.append("country ILIKE :country")
        params["country"] = f"%{country}%"

    if from_dt:
        conditions.append("created_at >= :from_dt")
        params["from_dt"] = from_dt

    if to_dt:
        conditions.append("created_at <= :to_dt")
        params["to_dt"] = to_dt

    where = ("WHERE " + " AND ".join(conditions)) if conditions else ""

    count_sql = f"SELECT COUNT(*) FROM sign_in_events {where}"
    data_sql = f"""
        SELECT id, created_at, user_principal_name, user_display_name,
               app_display_name, ip_address, client_app_used,
               error_code, country, conditional_access_status,
               risk_level_aggregated, status, location, device_detail
        FROM sign_in_events
        {where}
        ORDER BY {sort} {order}
        LIMIT :limit OFFSET :offset
    """
    params["limit"] = per_page
    params["offset"] = (page - 1) * per_page

    session = get_session()
    try:
        total = session.execute(text(count_sql), params).scalar()
        rows = session.execute(text(data_sql), params).mappings().all()
        return {
            "total": total,
            "page": page,
            "per_page": per_page,
            "pages": math.ceil(total / per_page) if total else 0,
            "results": [dict(r) for r in rows],
        }
    finally:
        session.close()


def get_event_by_id(event_id: str) -> dict | None:
    session = get_session()
    try:
        row = session.execute(
            text("SELECT * FROM sign_in_events WHERE id = :id"),
            {"id": event_id},
        ).mappings().first()
        return dict(row) if row else None
    finally:
        session.close()


# ─── Dashboard ────────────────────────────────────────────────────────────────


def _window_clause(hours: int) -> tuple[str, dict]:
    """Return WHERE clause and params for a time window of N hours."""
    return "created_at >= NOW() - INTERVAL ':h hours'", {"h": hours}


def get_dashboard_metrics(
    hours: int = 24,
    signin_types: list[str] | None = None,
) -> dict:
    """
    Aggregate metrics for the dashboard over the last `hours` hours.

    `signin_types` filters by signin_event_type
    (interactiveUser | nonInteractiveUser | servicePrincipal | managedIdentity).
    None / empty list = no filter (all types).
    """
    session = get_session()
    try:
        interval = f"{hours} hours"
        params: dict[str, Any] = {"interval": interval}

        # Build the optional event-type filter as a SQL fragment.
        if signin_types:
            type_clause = " AND signin_event_type = ANY(:types)"
            params["types"] = list(signin_types)
        else:
            type_clause = ""

        # ── Totals ────────────────────────────────────────────────────────
        totals = session.execute(
            text(f"""
                SELECT
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE error_code != 0) AS failures
                FROM sign_in_events
                WHERE created_at >= NOW() - CAST(:interval AS INTERVAL)
                {type_clause}
            """),
            params,
        ).mappings().first()
        total = int(totals["total"] or 0)
        failures = int(totals["failures"] or 0)
        failure_rate = round(failures / total * 100, 1) if total else 0.0

        # ── Top users by failures ──────────────────────────────────────────
        top_users_by_failures = session.execute(
            text(f"""
                SELECT user_principal_name, COUNT(*) AS cnt
                FROM sign_in_events
                WHERE created_at >= NOW() - CAST(:interval AS INTERVAL)
                  AND error_code != 0
                  {type_clause}
                GROUP BY user_principal_name
                ORDER BY cnt DESC
                LIMIT 10
            """),
            params,
        ).mappings().all()

        # ── Top IPs by failures ────────────────────────────────────────────
        top_ips_by_failures = session.execute(
            text(f"""
                SELECT ip_address, COUNT(*) AS cnt
                FROM sign_in_events
                WHERE created_at >= NOW() - CAST(:interval AS INTERVAL)
                  AND error_code != 0
                  {type_clause}
                GROUP BY ip_address
                ORDER BY cnt DESC
                LIMIT 10
            """),
            params,
        ).mappings().all()

        # ── Sign-ins over time (hourly buckets) ────────────────────────────
        if hours <= 24:
            bucket = "hour"
        elif hours <= 24 * 7:
            bucket = "6 hours"
        else:
            bucket = "day"

        timeline = session.execute(
            text(f"""
                SELECT
                    DATE_TRUNC('{bucket}', created_at) AS bucket,
                    COUNT(*) AS total,
                    COUNT(*) FILTER (WHERE error_code != 0) AS failures
                FROM sign_in_events
                WHERE created_at >= NOW() - CAST(:interval AS INTERVAL)
                {type_clause}
                GROUP BY bucket
                ORDER BY bucket
            """),
            params,
        ).mappings().all()

        return {
            "total": total,
            "failures": failures,
            "failure_rate": failure_rate,
            "top_users_by_failures": [dict(r) for r in top_users_by_failures],
            "top_ips_by_failures": [dict(r) for r in top_ips_by_failures],
            "timeline": [
                {
                    "bucket": r["bucket"].isoformat() if r["bucket"] else None,
                    "total": int(r["total"]),
                    "failures": int(r["failures"]),
                }
                for r in timeline
            ],
        }
    finally:
        session.close()


# ─── Anomaly detection ────────────────────────────────────────────────────────


def get_anomalies(hours: int = 24, lookback_days: int = 30) -> list[dict]:
    """
    Run all anomaly heuristics and return a combined list of flagged events.
    All queries run against the local DB – no Graph calls.

    Scoped to signin_event_type='interactiveUser': service principal and
    managed identity sign-ins don't make sense for "impossible travel" /
    "new IP for user" heuristics, and non-interactive token refreshes
    create too much noise (they happen automatically from background
    clients regardless of the user's location/behavior).
    """
    session = get_session()
    try:
        anomalies: list[dict] = []
        interval = f"{hours} hours"
        lookback = f"{lookback_days} days"

        # ── 1. New IP for user ─────────────────────────────────────────────
        new_ip_rows = session.execute(
            text("""
                WITH recent AS (
                    SELECT id, created_at, user_principal_name, ip_address,
                           app_display_name, country
                    FROM sign_in_events
                    WHERE created_at >= NOW() - CAST(:interval AS INTERVAL)
                      AND signin_event_type = 'interactiveUser'
                      AND ip_address IS NOT NULL
                ),
                historical_ips AS (
                    SELECT DISTINCT user_principal_name, ip_address
                    FROM sign_in_events
                    WHERE created_at < NOW() - CAST(:interval AS INTERVAL)
                      AND created_at >= NOW() - CAST(:lookback AS INTERVAL)
                      AND signin_event_type = 'interactiveUser'
                      AND ip_address IS NOT NULL
                )
                SELECT r.id, r.created_at, r.user_principal_name,
                       r.ip_address, r.app_display_name, r.country,
                       'new_ip_for_user' AS reason
                FROM recent r
                LEFT JOIN historical_ips h
                    ON r.user_principal_name = h.user_principal_name
                   AND r.ip_address = h.ip_address
                WHERE h.ip_address IS NULL
                ORDER BY r.created_at DESC
                LIMIT 200
            """),
            {"interval": interval, "lookback": lookback},
        ).mappings().all()
        anomalies.extend([dict(r) for r in new_ip_rows])

        # ── 2. Unfamiliar country ──────────────────────────────────────────
        new_country_rows = session.execute(
            text("""
                WITH recent AS (
                    SELECT id, created_at, user_principal_name, ip_address,
                           app_display_name, country
                    FROM sign_in_events
                    WHERE created_at >= NOW() - CAST(:interval AS INTERVAL)
                      AND signin_event_type = 'interactiveUser'
                      AND country IS NOT NULL
                ),
                historical_countries AS (
                    SELECT DISTINCT user_principal_name, country
                    FROM sign_in_events
                    WHERE created_at < NOW() - CAST(:interval AS INTERVAL)
                      AND created_at >= NOW() - CAST(:lookback AS INTERVAL)
                      AND signin_event_type = 'interactiveUser'
                      AND country IS NOT NULL
                )
                SELECT r.id, r.created_at, r.user_principal_name,
                       r.ip_address, r.app_display_name, r.country,
                       'unfamiliar_country' AS reason
                FROM recent r
                LEFT JOIN historical_countries h
                    ON r.user_principal_name = h.user_principal_name
                   AND r.country = h.country
                WHERE h.country IS NULL
                ORDER BY r.created_at DESC
                LIMIT 200
            """),
            {"interval": interval, "lookback": lookback},
        ).mappings().all()
        anomalies.extend([dict(r) for r in new_country_rows])

        # ── 3. Impossible travel (same user, 2 distant logins < 1 hour) ───
        # Heuristic: consecutive sign-ins for same user from different
        # countries within 60 minutes. We approximate "distance" by requiring
        # different countryOrRegion values (simple but cheap).
        impossible_travel_rows = session.execute(
            text("""
                WITH ordered AS (
                    SELECT id, created_at, user_principal_name, ip_address,
                           app_display_name, country,
                           LAG(created_at)  OVER w AS prev_time,
                           LAG(country)     OVER w AS prev_country,
                           LAG(ip_address)  OVER w AS prev_ip
                    FROM sign_in_events
                    WHERE created_at >= NOW() - CAST(:interval AS INTERVAL)
                      AND signin_event_type = 'interactiveUser'
                      AND country IS NOT NULL
                    WINDOW w AS (
                        PARTITION BY user_principal_name
                        ORDER BY created_at
                    )
                )
                SELECT id, created_at, user_principal_name, ip_address,
                       app_display_name, country,
                       'impossible_travel' AS reason
                FROM ordered
                WHERE prev_time IS NOT NULL
                  AND prev_country IS NOT NULL
                  AND country != prev_country
                  AND EXTRACT(EPOCH FROM (created_at - prev_time)) < 3600
                ORDER BY created_at DESC
                LIMIT 200
            """),
            {"interval": interval},
        ).mappings().all()
        anomalies.extend([dict(r) for r in impossible_travel_rows])

        # ── 4. Repeated conditional access failures ────────────────────────
        ca_failures = session.execute(
            text("""
                SELECT
                    user_principal_name,
                    COUNT(*) AS failure_count,
                    MAX(id) AS id,
                    MAX(created_at) AS created_at,
                    MAX(ip_address) AS ip_address,
                    MAX(app_display_name) AS app_display_name,
                    MAX(country) AS country,
                    'repeated_ca_failure' AS reason
                FROM sign_in_events
                WHERE created_at >= NOW() - CAST(:interval AS INTERVAL)
                  AND signin_event_type = 'interactiveUser'
                  AND conditional_access_status = 'failure'
                GROUP BY user_principal_name
                HAVING COUNT(*) >= 3
                ORDER BY failure_count DESC
                LIMIT 50
            """),
            {"interval": interval},
        ).mappings().all()
        anomalies.extend([dict(r) for r in ca_failures])

        # Deduplicate by (id, reason) to keep list manageable
        seen: set[tuple] = set()
        unique: list[dict] = []
        for a in anomalies:
            key = (a.get("id"), a.get("reason"))
            if key not in seen:
                seen.add(key)
                # Normalise datetime fields
                if isinstance(a.get("created_at"), datetime):
                    a = dict(a)
                    a["created_at"] = a["created_at"].isoformat()
                unique.append(a)

        return sorted(unique, key=lambda x: x.get("created_at") or "", reverse=True)

    finally:
        session.close()


def get_new_ips_per_user(hours: int = 24, lookback_days: int = 30) -> list[dict]:
    """Summarise new-IP anomalies grouped by user."""
    session = get_session()
    try:
        interval = f"{hours} hours"
        lookback = f"{lookback_days} days"
        rows = session.execute(
            text("""
                WITH recent AS (
                    SELECT user_principal_name, ip_address
                    FROM sign_in_events
                    WHERE created_at >= NOW() - CAST(:interval AS INTERVAL)
                      AND ip_address IS NOT NULL
                    GROUP BY user_principal_name, ip_address
                ),
                historical_ips AS (
                    SELECT DISTINCT user_principal_name, ip_address
                    FROM sign_in_events
                    WHERE created_at < NOW() - CAST(:interval AS INTERVAL)
                      AND created_at >= NOW() - CAST(:lookback AS INTERVAL)
                      AND ip_address IS NOT NULL
                )
                SELECT r.user_principal_name, COUNT(*) AS new_ip_count
                FROM recent r
                LEFT JOIN historical_ips h
                    ON r.user_principal_name = h.user_principal_name
                   AND r.ip_address = h.ip_address
                WHERE h.ip_address IS NULL
                GROUP BY r.user_principal_name
                ORDER BY new_ip_count DESC
                LIMIT 20
            """),
            {"interval": interval, "lookback": lookback},
        ).mappings().all()
        return [dict(r) for r in rows]
    finally:
        session.close()
