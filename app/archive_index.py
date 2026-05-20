"""
DuckDB-backed search index over R2-archived sign-in events.

Why this exists: the R2 archive is the source of truth for cold data, but
scanning gzipped JSONL across many days for every search is too slow. This
module maintains a single columnar DuckDB file containing the searchable
columns only (no raw_event, no nested JSON blobs) so cross-archive search
is sub-second even at year-scale.

If the file is ever lost, rebuild_from_r2() walks every archived day and
re-populates it.
"""
from __future__ import annotations

import datetime as dt
import logging
import os
import threading
from typing import Any

import duckdb

logger = logging.getLogger(__name__)

# Module-level connection — DuckDB connections are not thread-safe so we
# guard with a lock. The Flask app runs sync workers (Gunicorn sync), so
# contention is low in practice.
_conn: duckdb.DuckDBPyConnection | None = None
_conn_lock = threading.Lock()
_current_path: str | None = None


SCHEMA_SQL = """
CREATE TABLE IF NOT EXISTS signins (
    id                   VARCHAR NOT NULL,
    day                  DATE NOT NULL,
    created_at           TIMESTAMP NOT NULL,
    user_principal_name  VARCHAR,
    user_display_name    VARCHAR,
    app_display_name     VARCHAR,
    ip_address           VARCHAR,
    country              VARCHAR,
    error_code           INTEGER,
    signin_event_type    VARCHAR
);
CREATE INDEX IF NOT EXISTS ix_signins_day ON signins(day);
CREATE INDEX IF NOT EXISTS ix_signins_id  ON signins(id);
CREATE INDEX IF NOT EXISTS ix_signins_upn ON signins(user_principal_name);
CREATE INDEX IF NOT EXISTS ix_signins_ip  ON signins(ip_address);
"""
# Note: no PRIMARY KEY on id. DuckDB doesn't refresh a PK index between a
# DELETE and an INSERT in the same transaction, which broke our
# delete-then-insert idempotency pattern. Uniqueness is enforced by the
# caller (index_day deletes the whole day before inserting fresh rows).


def _get_conn(cfg) -> duckdb.DuckDBPyConnection:
    """Open or return the cached DuckDB connection."""
    global _conn, _current_path
    if _conn is not None and _current_path == cfg.ARCHIVE_INDEX_PATH:
        return _conn

    os.makedirs(os.path.dirname(cfg.ARCHIVE_INDEX_PATH), exist_ok=True)
    _conn = duckdb.connect(cfg.ARCHIVE_INDEX_PATH)
    _current_path = cfg.ARCHIVE_INDEX_PATH
    _conn.execute(SCHEMA_SQL)
    return _conn


def close() -> None:
    """Close the connection (used in tests)."""
    global _conn, _current_path
    if _conn is not None:
        _conn.close()
        _conn = None
        _current_path = None


def _parse_dt(value: Any) -> dt.datetime | None:
    """Parse the ISO string we wrote into archives back into a datetime."""
    if value is None:
        return None
    if isinstance(value, dt.datetime):
        return value
    if isinstance(value, str):
        # Tolerate fractional seconds + trailing 'Z'
        s = value.rstrip("Z")
        for fmt in ("%Y-%m-%dT%H:%M:%S.%f", "%Y-%m-%dT%H:%M:%S"):
            try:
                return dt.datetime.strptime(s, fmt)
            except ValueError:
                continue
    return None


def index_day(cfg, day: dt.date, rows: list[dict]) -> int:
    """
    Idempotently upsert one day's rows into the index.

    `rows` is the list of dicts produced by archive_service when the day
    was archived (matches the dicts that are written to JSONL).

    Returns the row count written.
    """
    if not rows:
        return 0

    conn = _get_conn(cfg)
    tuples = []
    for r in rows:
        created = _parse_dt(r.get("created_at"))
        if created is None:
            continue
        tuples.append((
            r.get("id"),
            day,
            created,
            r.get("user_principal_name"),
            r.get("user_display_name"),
            r.get("app_display_name"),
            r.get("ip_address"),
            r.get("country"),
            r.get("error_code"),
            r.get("signin_event_type"),
        ))

    with _conn_lock:
        # Delete-then-insert is the safest idempotent pattern (DuckDB
        # doesn't support ON CONFLICT for tables with PRIMARY KEY in
        # all versions). Wrap in a transaction so the index never has
        # half a day.
        conn.execute("BEGIN")
        try:
            conn.execute("DELETE FROM signins WHERE day = ?", [day])
            conn.executemany(
                "INSERT INTO signins VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                tuples,
            )
            conn.execute("COMMIT")
        except Exception:
            conn.execute("ROLLBACK")
            raise

    return len(tuples)


def search(
    cfg,
    *,
    keyword: str | None = None,
    user_principal_name: str | None = None,
    ip_address: str | None = None,
    app_display_name: str | None = None,
    country: str | None = None,
    error_code: int | None = None,
    signin_event_type: str | None = None,
    date_from: dt.date | None = None,
    date_to: dt.date | None = None,
    page: int = 1,
    per_page: int = 50,
) -> dict:
    """Run a search against the archive index. Returns paginated rows + total."""
    conn = _get_conn(cfg)

    where: list[str] = []
    params: list[Any] = []

    if date_from is not None:
        where.append("day >= ?")
        params.append(date_from)
    if date_to is not None:
        where.append("day <= ?")
        params.append(date_to)
    if user_principal_name:
        where.append("user_principal_name ILIKE ?")
        params.append(f"%{user_principal_name}%")
    if ip_address:
        where.append("ip_address ILIKE ?")
        params.append(f"%{ip_address}%")
    if app_display_name:
        where.append("app_display_name ILIKE ?")
        params.append(f"%{app_display_name}%")
    if country:
        where.append("country ILIKE ?")
        params.append(f"%{country}%")
    if error_code is not None:
        where.append("error_code = ?")
        params.append(error_code)
    if signin_event_type:
        where.append("signin_event_type = ?")
        params.append(signin_event_type)
    if keyword:
        where.append(
            "(user_principal_name ILIKE ? OR user_display_name ILIKE ? "
            "OR app_display_name ILIKE ? OR ip_address ILIKE ?)"
        )
        kw = f"%{keyword}%"
        params.extend([kw, kw, kw, kw])

    where_sql = (" WHERE " + " AND ".join(where)) if where else ""

    with _conn_lock:
        total = conn.execute(
            f"SELECT COUNT(*) FROM signins {where_sql}", params
        ).fetchone()[0]

        offset = (page - 1) * per_page
        rows = conn.execute(
            f"""
            SELECT id, day, created_at, user_principal_name, user_display_name,
                   app_display_name, ip_address, country, error_code,
                   signin_event_type
            FROM signins
            {where_sql}
            ORDER BY created_at DESC
            LIMIT {int(per_page)} OFFSET {int(offset)}
            """,
            params,
        ).fetchall()

    cols = [
        "id", "day", "created_at", "user_principal_name", "user_display_name",
        "app_display_name", "ip_address", "country", "error_code",
        "signin_event_type",
    ]
    results = [dict(zip(cols, r)) for r in rows]

    return {
        "total": int(total),
        "page": page,
        "per_page": per_page,
        "pages": (int(total) + per_page - 1) // per_page if total else 0,
        "results": results,
    }


def get_index_stats(cfg) -> dict:
    """Stats for the admin page: row count, day count, file size."""
    try:
        conn = _get_conn(cfg)
        with _conn_lock:
            row = conn.execute(
                "SELECT COUNT(*) AS rows, COUNT(DISTINCT day) AS days, "
                "MIN(day) AS earliest, MAX(day) AS latest FROM signins"
            ).fetchone()
        stats = {
            "rows": int(row[0]),
            "days": int(row[1]),
            "earliest": row[2].isoformat() if row[2] else None,
            "latest": row[3].isoformat() if row[3] else None,
        }
    except Exception as exc:
        logger.exception("Failed to read index stats")
        return {"rows": 0, "days": 0, "earliest": None, "latest": None, "error": str(exc)}

    try:
        stats["file_bytes"] = os.path.getsize(cfg.ARCHIVE_INDEX_PATH)
    except OSError:
        stats["file_bytes"] = 0
    return stats


def rebuild_from_r2(cfg) -> dict:
    """
    Drop the index table and re-populate from every archived day in R2.

    Used after data loss, or one-time when this feature is first deployed
    against a project that already has archives.
    """
    # Local imports to avoid a hard dep cycle (archive_service imports this
    # module via the hook; this function pulls archive_service in reverse).
    from .archive_service import (
        get_archived_day_record,
        list_archived_days,
        load_archived_day,
    )

    conn = _get_conn(cfg)
    with _conn_lock:
        conn.execute("DROP TABLE IF EXISTS signins")
        conn.execute(SCHEMA_SQL)

    days = list_archived_days()
    summary = {"days_indexed": 0, "rows_indexed": 0, "errors": []}
    for d in days:
        if d.get("status") != "success" or not d.get("rows_archived"):
            continue
        day = d["day"]
        if isinstance(day, str):
            day = dt.date.fromisoformat(day)
        try:
            rows = load_archived_day(cfg, day)
            n = index_day(cfg, day, rows)
            summary["days_indexed"] += 1
            summary["rows_indexed"] += n
        except Exception as exc:
            logger.exception("Index rebuild failed for %s", day)
            summary["errors"].append({"day": str(day), "error": str(exc)})

    logger.info("rebuild_from_r2 summary: %s", summary)
    return summary
