"""
Archive sign_in_events older than ARCHIVE_HOT_DAYS to Cloudflare R2 as
gzipped JSON Lines, then DELETE the archived rows from Postgres.

Design:
- One R2 object per day: signins/YYYY/MM/YYYY-MM-DD.jsonl.gz
- Rows streamed in chunks (default 5,000) so RAM stays flat regardless of
  daily volume — important since the host has only ~2 GB free RAM.
- Idempotent per day: re-archiving overwrites the object and updates
  archive_log. We never DELETE before a successful upload.
- Each row written as one JSON line: full to_dict() output plus raw_event
  so the archive is self-describing for forensic re-import later.
"""
from __future__ import annotations

import datetime as dt
import gzip
import io
import json
import logging
from typing import Any, Iterable

import boto3
from botocore.config import Config as BotoConfig
from sqlalchemy import text

from .extensions import get_session

logger = logging.getLogger(__name__)

CHUNK_SIZE = 5000

# Columns we pull from sign_in_events for the archive. Mirror to_dict() plus
# raw_event so the archive contains the full Graph payload.
_ARCHIVE_COLUMNS = [
    "id", "created_at", "user_id", "user_display_name", "user_principal_name",
    "app_id", "app_display_name", "ip_address", "client_app_used",
    "status", "location", "device_detail",
    "conditional_access_status", "risk_detail", "risk_level_aggregated",
    "error_code", "country", "signin_event_type", "raw_event",
]


def get_r2_client(cfg) -> Any:
    """Build an S3 client pointed at Cloudflare R2."""
    return boto3.client(
        "s3",
        endpoint_url=cfg.R2_ENDPOINT,
        aws_access_key_id=cfg.R2_ACCESS_KEY_ID,
        aws_secret_access_key=cfg.R2_SECRET_ACCESS_KEY,
        region_name="auto",
        config=BotoConfig(
            retries={"max_attempts": 5, "mode": "standard"},
            s3={"addressing_style": "path"},
        ),
    )


def _r2_key(day: dt.date) -> str:
    return f"signins/{day.year:04d}/{day.month:02d}/{day.isoformat()}.jsonl.gz"


def _stream_rows_for_day(session, day: dt.date) -> Iterable[dict]:
    """Yield rows for the given UTC day in id-ordered chunks (cursor-stable)."""
    last_pk = -1
    cols = ", ".join(_ARCHIVE_COLUMNS) + ", pk"
    while True:
        rows = session.execute(
            text(
                f"SELECT {cols} FROM sign_in_events "
                "WHERE created_at >= :day AND created_at < :next_day "
                "  AND pk > :last_pk "
                "ORDER BY pk ASC LIMIT :lim"
            ),
            {
                "day": day,
                "next_day": day + dt.timedelta(days=1),
                "last_pk": last_pk,
                "lim": CHUNK_SIZE,
            },
        ).mappings().all()
        if not rows:
            return
        for r in rows:
            last_pk = r["pk"]
            yield dict(r)
        if len(rows) < CHUNK_SIZE:
            return


def _json_default(o: Any) -> Any:
    if isinstance(o, (dt.datetime, dt.date)):
        return o.isoformat()
    raise TypeError(f"not JSON-serializable: {type(o).__name__}")


def _build_gzip_payload(rows: Iterable[dict]) -> tuple[bytes, int]:
    """Encode an iterable of dicts as gzipped JSON Lines, return (bytes, row_count).

    Builds in memory because R2 PutObject needs Content-Length. For our daily
    volumes (37k rows interactive only, even 15x = 555k rows × ~1 KB
    serialized = ~500 MB raw / ~50-80 MB gzipped per day), this is fine.
    If a single day ever exceeds ~1 GB compressed we'd switch to multipart.
    """
    buf = io.BytesIO()
    n = 0
    with gzip.GzipFile(fileobj=buf, mode="wb", compresslevel=6) as gz:
        for row in rows:
            # Drop the pk we added for cursoring; not useful in the archive.
            row.pop("pk", None)
            gz.write(json.dumps(row, default=_json_default).encode("utf-8"))
            gz.write(b"\n")
            n += 1
    return buf.getvalue(), n


def archive_day(cfg, day: dt.date) -> dict:
    """
    Archive a single UTC day to R2, then DELETE those rows from Postgres.

    Idempotent: re-running for the same day overwrites the R2 object and
    refreshes archive_log. We always upload before deleting.
    """
    if not cfg.archive_configured:
        raise RuntimeError("Archive is not configured (set R2_* and ARCHIVE_ENABLED=true).")

    started_at = dt.datetime.utcnow()
    key = _r2_key(day)
    session = get_session()
    try:
        # Mark in_progress (insert or update). Avoid SQLAlchemy ORM for
        # simplicity; raw upsert keeps this isolated from the model layer.
        session.execute(
            text(
                "INSERT INTO archive_log (day, status, started_at) "
                "VALUES (:day, 'in_progress', :now) "
                "ON CONFLICT (day) DO UPDATE SET "
                "  status='in_progress', started_at=:now, "
                "  finished_at=NULL, error_message=NULL"
            ),
            {"day": day, "now": started_at},
        )
        session.commit()

        # Materialize the day's rows so we can both gzip-upload them AND
        # feed them to the search index. Memory cost is bounded by daily
        # volume (~500 MB worst-case at 15x interactive volume).
        day_rows = list(_stream_rows_for_day(session, day))
        payload, row_count = _build_gzip_payload([dict(r) for r in day_rows])

        if row_count == 0:
            logger.info("archive: no rows for %s; skipping upload", day)
            finished = dt.datetime.utcnow()
            session.execute(
                text(
                    "UPDATE archive_log SET status='success', rows_archived=0, "
                    "  bytes_uploaded=0, r2_key=NULL, finished_at=:now "
                    "WHERE day=:day"
                ),
                {"day": day, "now": finished},
            )
            session.commit()
            return {"day": day.isoformat(), "rows": 0, "bytes": 0, "status": "empty"}

        # Upload to R2.
        s3 = get_r2_client(cfg)
        s3.put_object(
            Bucket=cfg.R2_BUCKET,
            Key=key,
            Body=payload,
            ContentType="application/x-ndjson",
            ContentEncoding="gzip",
        )
        bytes_uploaded = len(payload)
        logger.info(
            "archive: uploaded %s (%d rows, %d bytes gzipped) to %s",
            day, row_count, bytes_uploaded, key,
        )

        # Update the search index. Failure here must not block archival —
        # the index can always be rebuilt from R2 later.
        try:
            from . import archive_index

            # The gzip writer strips pk; we still have it on day_rows.
            # Re-serialize created_at as ISO so index_day's parser handles
            # it the same way as data loaded back from R2.
            indexable = []
            for r in day_rows:
                rr = dict(r)
                rr.pop("pk", None)
                ts = rr.get("created_at")
                if hasattr(ts, "isoformat"):
                    rr["created_at"] = ts.isoformat()
                indexable.append(rr)
            archive_index.index_day(cfg, day, indexable)
        except Exception:
            logger.exception("archive: failed to update DuckDB index for %s", day)

        # Delete archived rows from Postgres. Done in chunks to keep the
        # transaction short and let autovacuum keep up.
        deleted_total = 0
        while True:
            res = session.execute(
                text(
                    "WITH del AS ("
                    "  SELECT pk FROM sign_in_events "
                    "  WHERE created_at >= :day AND created_at < :next_day "
                    "  ORDER BY pk LIMIT :lim"
                    ") "
                    "DELETE FROM sign_in_events s USING del "
                    "WHERE s.pk = del.pk"
                ),
                {
                    "day": day,
                    "next_day": day + dt.timedelta(days=1),
                    "lim": CHUNK_SIZE,
                },
            )
            session.commit()
            deleted_total += res.rowcount
            if res.rowcount < CHUNK_SIZE:
                break

        finished = dt.datetime.utcnow()
        session.execute(
            text(
                "UPDATE archive_log SET status='success', rows_archived=:n, "
                "  bytes_uploaded=:b, r2_key=:k, finished_at=:now "
                "WHERE day=:day"
            ),
            {"day": day, "n": row_count, "b": bytes_uploaded, "k": key, "now": finished},
        )
        session.commit()

        return {
            "day": day.isoformat(),
            "rows": row_count,
            "bytes": bytes_uploaded,
            "deleted": deleted_total,
            "r2_key": key,
            "status": "success",
        }

    except Exception as exc:
        session.rollback()
        finished = dt.datetime.utcnow()
        try:
            session.execute(
                text(
                    "UPDATE archive_log SET status='error', error_message=:e, "
                    "  finished_at=:now WHERE day=:day"
                ),
                {"day": day, "e": str(exc)[:2000], "now": finished},
            )
            session.commit()
        except Exception:
            session.rollback()
        logger.exception("archive: failed for %s", day)
        raise
    finally:
        session.close()


def days_to_archive(cfg) -> list[dt.date]:
    """
    Return the list of UTC days that:
      - are older than ARCHIVE_HOT_DAYS ago,
      - have at least one row in sign_in_events,
      - do not yet have a success entry in archive_log.
    """
    cutoff = dt.date.today() - dt.timedelta(days=cfg.ARCHIVE_HOT_DAYS)
    session = get_session()
    try:
        rows = session.execute(
            text(
                "SELECT DISTINCT date_trunc('day', created_at)::date AS d "
                "FROM sign_in_events "
                "WHERE created_at < :cutoff "
                "  AND NOT EXISTS ("
                "    SELECT 1 FROM archive_log a "
                "    WHERE a.day = date_trunc('day', sign_in_events.created_at)::date "
                "      AND a.status = 'success'"
                "  ) "
                "ORDER BY d"
            ),
            {"cutoff": cutoff},
        ).mappings().all()
        return [r["d"] for r in rows]
    finally:
        session.close()


def run_archive_sweep(cfg, max_days: int | None = None) -> dict:
    """Archive every eligible day (or up to max_days for the first run)."""
    if not cfg.archive_configured:
        logger.info("archive: disabled / not configured")
        return {"status": "disabled", "days_processed": 0}

    days = days_to_archive(cfg)
    if max_days is not None:
        days = days[:max_days]

    summary = {
        "status": "success",
        "days_processed": 0,
        "rows_archived": 0,
        "bytes_uploaded": 0,
        "errors": [],
    }
    for d in days:
        try:
            r = archive_day(cfg, d)
            summary["days_processed"] += 1
            summary["rows_archived"] += r.get("rows", 0)
            summary["bytes_uploaded"] += r.get("bytes", 0)
        except Exception as exc:
            summary["errors"].append({"day": d.isoformat(), "error": str(exc)})
            # Keep going — one bad day shouldn't block the rest.
    if summary["errors"]:
        summary["status"] = "partial" if summary["days_processed"] else "error"
    logger.info("archive sweep: %s", summary)
    return summary


def list_archived_days() -> list[dict]:
    """Return all archive_log entries, newest day first."""
    session = get_session()
    try:
        rows = session.execute(
            text(
                "SELECT day, status, rows_archived, bytes_uploaded, r2_key, "
                "       error_message, started_at, finished_at "
                "FROM archive_log ORDER BY day DESC"
            )
        ).mappings().all()
        return [dict(r) for r in rows]
    finally:
        session.close()


def get_archived_day_record(day: dt.date) -> dict | None:
    """Look up a single archive_log entry by day."""
    session = get_session()
    try:
        row = session.execute(
            text(
                "SELECT day, status, rows_archived, bytes_uploaded, r2_key, "
                "       error_message, started_at, finished_at "
                "FROM archive_log WHERE day = :day"
            ),
            {"day": day},
        ).mappings().first()
        return dict(row) if row else None
    finally:
        session.close()


# Process-local cache so repeated pagination clicks on the same archived day
# don't re-download from R2. Bounded so a couple of huge days don't blow up
# RAM on the box (only ~2 GB free at steady state).
_DAY_CACHE: "dict[str, list[dict]]" = {}
_DAY_CACHE_ORDER: list[str] = []
_DAY_CACHE_MAX = 2


def _cache_day(key: str, rows: list[dict]) -> None:
    """Insert into the LRU-ish day cache, evicting oldest if needed."""
    if key in _DAY_CACHE:
        _DAY_CACHE_ORDER.remove(key)
    _DAY_CACHE[key] = rows
    _DAY_CACHE_ORDER.append(key)
    while len(_DAY_CACHE_ORDER) > _DAY_CACHE_MAX:
        evict = _DAY_CACHE_ORDER.pop(0)
        _DAY_CACHE.pop(evict, None)


def load_archived_day(cfg, day: dt.date) -> list[dict]:
    """
    Fetch one day's archive from R2 and return its parsed rows.

    The whole day is loaded into memory (caller iterates / paginates).
    Cached process-locally so repeat calls within a session are free.
    """
    if not cfg.archive_configured:
        raise RuntimeError("Archive is not configured.")

    cache_key = day.isoformat()
    cached = _DAY_CACHE.get(cache_key)
    if cached is not None:
        # Promote to most-recent in eviction order
        _DAY_CACHE_ORDER.remove(cache_key)
        _DAY_CACHE_ORDER.append(cache_key)
        return cached

    key = _r2_key(day)
    s3 = get_r2_client(cfg)
    obj = s3.get_object(Bucket=cfg.R2_BUCKET, Key=key)
    raw = obj["Body"].read()
    decompressed = gzip.decompress(raw)

    rows: list[dict] = []
    for line in decompressed.splitlines():
        if not line:
            continue
        rows.append(json.loads(line))

    _cache_day(cache_key, rows)
    return rows


def get_archive_status(cfg) -> dict:
    """For the admin dashboard: earliest hot date + last archive run + R2 totals."""
    session = get_session()
    try:
        hot = session.execute(
            text("SELECT MIN(created_at) AS earliest, COUNT(*) AS rows FROM sign_in_events")
        ).mappings().first()
        last = session.execute(
            text(
                "SELECT day, status, rows_archived, bytes_uploaded, finished_at "
                "FROM archive_log ORDER BY started_at DESC LIMIT 1"
            )
        ).mappings().first()
        totals = session.execute(
            text(
                "SELECT COALESCE(SUM(rows_archived),0) AS rows, "
                "       COALESCE(SUM(bytes_uploaded),0) AS bytes, "
                "       COUNT(*) FILTER (WHERE status='success') AS days "
                "FROM archive_log"
            )
        ).mappings().first()
        return {
            "configured": cfg.archive_configured,
            "hot_days": cfg.ARCHIVE_HOT_DAYS,
            "hot_rows": int(hot["rows"] or 0),
            "hot_earliest": hot["earliest"].isoformat() if hot["earliest"] else None,
            "last_run": dict(last) if last else None,
            "totals": dict(totals) if totals else None,
        }
    finally:
        session.close()
