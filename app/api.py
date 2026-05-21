"""JSON API endpoints."""
from __future__ import annotations

import logging
import threading
from datetime import datetime
from typing import Any

from flask import Blueprint, current_app, jsonify, request

from .oauth import login_required
from .queries import (
    get_anomalies,
    get_dashboard_metrics,
    get_new_ips_per_user,
    search_events,
)
from .archive_index import rebuild_from_r2 as rebuild_archive_index
from .archive_service import get_archive_status, run_archive_sweep
from .sync_service import get_last_sync_info, get_sync_history, run_sync

logger = logging.getLogger(__name__)

api = Blueprint("api", __name__, url_prefix="/api")


def _int(v, default):
    try:
        return int(v)
    except (TypeError, ValueError):
        return default


def _parse_dt(s: str | None) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None


def _hours_from_window(window: str) -> int:
    return {"24h": 24, "7d": 168, "30d": 720}.get(window, 24)


@api.get("/search")
@login_required
def api_search():
    keyword = request.args.get("q", "").strip() or None
    upn = request.args.get("upn", "").strip() or None
    ip = request.args.get("ip", "").strip() or None
    app_name = request.args.get("app", "").strip() or None
    country = request.args.get("country", "").strip() or None
    from_dt = _parse_dt(request.args.get("from"))
    to_dt = _parse_dt(request.args.get("to"))
    ec_str = request.args.get("error_code", "").strip()
    error_code = int(ec_str) if ec_str.isdigit() else None
    page = _int(request.args.get("page"), 1)
    per_page = _int(request.args.get("per_page"), 50)

    try:
        data = search_events(
            keyword=keyword,
            user_principal_name=upn,
            ip_address=ip,
            app_display_name=app_name,
            error_code=error_code,
            country=country,
            from_dt=from_dt,
            to_dt=to_dt,
            page=page,
            per_page=per_page,
        )
        # Serialise datetime objects
        for row in data["results"]:
            for k, v in row.items():
                if isinstance(v, datetime):
                    row[k] = v.isoformat()
        return jsonify(data)
    except Exception as exc:
        logger.exception("Search error")
        return jsonify({"error": str(exc)}), 500


@api.get("/dashboard")
@login_required
def api_dashboard():
    window = request.args.get("window", "24h")
    hours = _hours_from_window(window)
    lookback_days = _int(request.args.get("lookback_days"), 30)
    try:
        metrics = get_dashboard_metrics(hours=hours)
        anomalies = get_anomalies(hours=hours, lookback_days=lookback_days)
        new_ips = get_new_ips_per_user(hours=hours, lookback_days=lookback_days)
        return jsonify(
            {
                "window": window,
                "hours": hours,
                "metrics": metrics,
                "anomalies": anomalies[:100],
                "new_ips_per_user": new_ips,
            }
        )
    except Exception as exc:
        logger.exception("Dashboard error")
        return jsonify({"error": str(exc)}), 500


@api.get("/dashboard/anomalies")
@login_required
def api_dashboard_anomalies():
    """Async endpoint the dashboard page calls after initial paint."""
    from .queries import get_anomalies_cache_age

    window = request.args.get("window", "24h")
    hours = _hours_from_window(window)
    lookback_days = _int(request.args.get("lookback_days"), 30)
    try:
        cache_age = get_anomalies_cache_age(hours, lookback_days)
        anomalies = get_anomalies(hours=hours, lookback_days=lookback_days)
        return jsonify({
            "anomalies": anomalies,
            "cache_age_seconds": cache_age,
        })
    except Exception as exc:
        logger.exception("Dashboard anomalies error")
        return jsonify({"error": str(exc)}), 500


_sync_lock = threading.Lock()


@api.post("/sync-now")
@login_required
def api_sync_now():
    cfg = current_app.config["APP_CONFIG"]
    if not cfg.graph_configured:
        return (
            jsonify(
                {
                    "status": "error",
                    "error": "Graph credentials not configured. Set TENANT_ID, CLIENT_ID, CLIENT_SECRET.",
                }
            ),
            400,
        )

    if not _sync_lock.acquire(blocking=False):
        return jsonify({"status": "running", "message": "Sync already in progress."}), 409

    app = current_app._get_current_object()

    def _run():
        try:
            with app.app_context():
                from .graph_client import GraphClient

                client = GraphClient(
                    tenant_id=cfg.TENANT_ID,
                    client_id=cfg.CLIENT_ID,
                    client_secret=cfg.CLIENT_SECRET,
                    scope=cfg.GRAPH_SCOPE,
                )
                run_sync(
                    client,
                    lookback_minutes=cfg.SYNC_LOOKBACK_MINUTES,
                    page_size=cfg.GRAPH_PAGE_SIZE,
                )
        except Exception:
            logger.exception("Manual sync error")
        finally:
            _sync_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started", "message": "Sync started in background. Poll /api/sync-status for results."}), 202


@api.get("/sync-status")
@login_required
def api_sync_status():
    info = get_last_sync_info()
    history = get_sync_history(limit=10)
    return jsonify({"cursor": info, "history": history})


_archive_lock = threading.Lock()


@api.post("/archive-now")
@login_required
def api_archive_now():
    cfg = current_app.config["APP_CONFIG"]
    if not cfg.archive_configured:
        return (
            jsonify({
                "status": "error",
                "error": "Archive not configured. Set R2_* env vars and ARCHIVE_ENABLED=true.",
            }),
            400,
        )

    max_days = _int(request.args.get("max_days"), 0) or None

    if not _archive_lock.acquire(blocking=False):
        return jsonify({"status": "running", "message": "Archive sweep already in progress."}), 409

    app = current_app._get_current_object()

    def _run():
        try:
            with app.app_context():
                run_archive_sweep(cfg, max_days=max_days)
        except Exception:
            logger.exception("Manual archive error")
        finally:
            _archive_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return (
        jsonify({"status": "started", "message": "Archive sweep started in background. Poll /api/archive-status for results."}),
        202,
    )


@api.get("/archive-status")
@login_required
def api_archive_status():
    cfg = current_app.config["APP_CONFIG"]
    return jsonify(get_archive_status(cfg))


_rebuild_lock = threading.Lock()


@api.post("/archive-rebuild-index")
@login_required
def api_archive_rebuild_index():
    cfg = current_app.config["APP_CONFIG"]
    if not cfg.archive_configured:
        return (
            jsonify({"status": "error", "error": "Archive not configured."}),
            400,
        )
    if not _rebuild_lock.acquire(blocking=False):
        return jsonify({"status": "running", "message": "Rebuild already in progress."}), 409

    app = current_app._get_current_object()

    def _run():
        try:
            with app.app_context():
                rebuild_archive_index(cfg)
        except Exception:
            logger.exception("Index rebuild error")
        finally:
            _rebuild_lock.release()

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"}), 202
