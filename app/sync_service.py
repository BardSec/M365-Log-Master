"""Incremental sync service: Graph → local PostgreSQL."""
from __future__ import annotations

import logging
import time
from datetime import datetime, timedelta, timezone
from typing import Any

from sqlalchemy import text
from sqlalchemy.dialects.postgresql import insert as pg_insert

from .extensions import get_session
from .graph_client import SIGNIN_EVENT_TYPES, GraphClient
from .models import SignInEvent, SyncCursor, SyncLog

logger = logging.getLogger(__name__)


def _utcnow() -> datetime:
    return datetime.now(timezone.utc).replace(tzinfo=None)


def _parse_dt(value: str | None) -> datetime | None:
    if not value:
        return None
    for fmt in ("%Y-%m-%dT%H:%M:%SZ", "%Y-%m-%dT%H:%M:%S.%fZ"):
        try:
            return datetime.strptime(value, fmt)
        except ValueError:
            continue
    return None


def _primary_event_type(raw: dict) -> str | None:
    """
    Pick the primary classifier from Graph's signInEventTypes array.

    The array can contain auxiliary tags like 'refreshToken' alongside the
    primary category. We return the first value that matches one of the four
    known top-level categories. Falls back to isInteractive if the array is
    missing (older payloads / v1.0 compatibility).
    """
    types = raw.get("signInEventTypes") or []
    if isinstance(types, list):
        for t in types:
            if t in SIGNIN_EVENT_TYPES:
                return t
    is_interactive = raw.get("isInteractive")
    if is_interactive is True:
        return "interactiveUser"
    if is_interactive is False:
        return "nonInteractiveUser"
    return None


def _coerce_event(raw: dict) -> dict[str, Any]:
    """Map a Graph sign-in event dict to column kwargs for SignInEvent."""
    status = raw.get("status") or {}
    location = raw.get("location") or {}

    error_code: int | None = None
    raw_ec = status.get("errorCode")
    if raw_ec is not None:
        try:
            error_code = int(raw_ec)
        except (ValueError, TypeError):
            pass

    country = location.get("countryOrRegion")

    return dict(
        id=raw["id"],
        created_at=_parse_dt(raw.get("createdDateTime")),
        user_id=raw.get("userId"),
        user_display_name=raw.get("userDisplayName"),
        user_principal_name=raw.get("userPrincipalName"),
        app_id=raw.get("appId"),
        app_display_name=raw.get("appDisplayName"),
        ip_address=raw.get("ipAddress"),
        client_app_used=raw.get("clientAppUsed"),
        status=status,
        location=location,
        device_detail=raw.get("deviceDetail"),
        conditional_access_status=raw.get("conditionalAccessStatus"),
        risk_detail=raw.get("riskDetail"),
        risk_level_aggregated=raw.get("riskLevelAggregated"),
        error_code=error_code,
        country=country,
        signin_event_type=_primary_event_type(raw),
        raw_event=raw,
    )


def get_or_create_cursor(session) -> SyncCursor:
    cursor = session.get(SyncCursor, 1)
    if cursor is None:
        cursor = SyncCursor(id=1, last_sync_time=None, last_event_id=None)
        session.add(cursor)
        session.flush()
    return cursor


def run_sync(client: GraphClient, lookback_minutes: int = 5, page_size: int = 500) -> dict:
    """
    Execute one incremental sync cycle.

    Returns a stats dict with fetched_count, inserted_count, updated_count,
    duration_seconds, status, error_message.
    """
    started_at = _utcnow()
    stats: dict[str, Any] = {
        "status": "success",
        "fetched_count": 0,
        "inserted_count": 0,
        "updated_count": 0,
        "error_message": None,
        "started_at": started_at.isoformat(),
    }

    session = get_session()
    try:
        # ── Determine time window ──────────────────────────────────────────
        cursor = get_or_create_cursor(session)
        to_dt = _utcnow()

        if cursor.last_sync_time is None:
            # First run: back-fill last 24 h
            from_dt = to_dt - timedelta(hours=24)
            logger.info("First sync – back-filling last 24 h.")
        else:
            # Subtract lookback to catch slightly-out-of-order events
            from_dt = cursor.last_sync_time - timedelta(minutes=lookback_minutes)

        logger.info(
            "Sync window: %s → %s", from_dt.isoformat(), to_dt.isoformat()
        )

        # ── Fetch from Graph ───────────────────────────────────────────────
        batch: list[dict[str, Any]] = []
        BATCH_SIZE = 200

        def _flush_batch(b: list[dict]) -> tuple[int, int]:
            inserted = updated = 0
            stmt = pg_insert(SignInEvent.__table__).values(b)
            stmt = stmt.on_conflict_do_update(
                index_elements=["id"],
                set_={
                    "raw_event": stmt.excluded.raw_event,
                    "status": stmt.excluded.status,
                    "risk_detail": stmt.excluded.risk_detail,
                    "risk_level_aggregated": stmt.excluded.risk_level_aggregated,
                },
            )
            result = session.execute(stmt)
            # rowcount counts both inserted + updated rows
            inserted = result.rowcount
            return inserted, updated

        total_fetched = 0
        total_inserted = 0

        for raw_event in client.fetch_sign_in_logs(from_dt, to_dt, page_size):
            total_fetched += 1
            row = _coerce_event(raw_event)
            if row["created_at"] is None:
                logger.warning("Skipping event with no createdDateTime: %s", raw_event.get("id"))
                continue
            batch.append(row)

            if len(batch) >= BATCH_SIZE:
                ins, _ = _flush_batch(batch)
                total_inserted += ins
                batch.clear()

        if batch:
            ins, _ = _flush_batch(batch)
            total_inserted += ins

        # ── Update cursor ──────────────────────────────────────────────────
        cursor.last_sync_time = to_dt
        session.flush()

        stats["fetched_count"] = total_fetched
        stats["inserted_count"] = total_inserted

        # ── Write sync log ─────────────────────────────────────────────────
        finished_at = _utcnow()
        duration = (finished_at - started_at).total_seconds()
        stats["duration_seconds"] = duration

        log_row = SyncLog(
            started_at=started_at,
            finished_at=finished_at,
            status="success",
            fetched_count=total_fetched,
            inserted_count=total_inserted,
            updated_count=0,
            duration_seconds=duration,
        )
        session.add(log_row)
        session.commit()

        logger.info(
            "Sync complete – fetched=%d inserted=%d duration=%.1fs",
            total_fetched,
            total_inserted,
            duration,
        )

    except Exception as exc:
        session.rollback()
        finished_at = _utcnow()
        duration = (finished_at - started_at).total_seconds()
        error_msg = str(exc)
        logger.error("Sync failed: %s", error_msg, exc_info=True)

        try:
            log_row = SyncLog(
                started_at=started_at,
                finished_at=finished_at,
                status="error",
                fetched_count=stats.get("fetched_count", 0),
                inserted_count=stats.get("inserted_count", 0),
                updated_count=0,
                error_message=error_msg,
                duration_seconds=duration,
            )
            session.add(log_row)
            session.commit()
        except Exception:
            pass

        stats["status"] = "error"
        stats["error_message"] = error_msg
        stats["duration_seconds"] = duration

    finally:
        session.close()

    return stats


def get_last_sync_info() -> dict:
    """Return last sync log entry and cursor state."""
    session = get_session()
    try:
        cursor = get_or_create_cursor(session)
        last_log = (
            session.query(SyncLog)
            .order_by(SyncLog.id.desc())
            .first()
        )
        return {
            "last_sync_time": (
                cursor.last_sync_time.isoformat() if cursor.last_sync_time else None
            ),
            "last_run": {
                "started_at": last_log.started_at.isoformat() if last_log else None,
                "finished_at": last_log.finished_at.isoformat() if last_log and last_log.finished_at else None,
                "status": last_log.status if last_log else None,
                "fetched_count": last_log.fetched_count if last_log else 0,
                "inserted_count": last_log.inserted_count if last_log else 0,
                "duration_seconds": last_log.duration_seconds if last_log else None,
                "error_message": last_log.error_message if last_log else None,
            } if last_log else None,
        }
    finally:
        session.close()


def get_sync_history(limit: int = 20) -> list[dict]:
    """Return recent sync log entries."""
    session = get_session()
    try:
        rows = (
            session.query(SyncLog)
            .order_by(SyncLog.id.desc())
            .limit(limit)
            .all()
        )
        return [
            {
                "id": r.id,
                "started_at": r.started_at.isoformat(),
                "finished_at": r.finished_at.isoformat() if r.finished_at else None,
                "status": r.status,
                "fetched_count": r.fetched_count,
                "inserted_count": r.inserted_count,
                "duration_seconds": r.duration_seconds,
                "error_message": r.error_message,
            }
            for r in rows
        ]
    finally:
        session.close()
