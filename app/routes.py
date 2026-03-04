"""Server-rendered Flask routes (HTML pages)."""
from __future__ import annotations

import json
from datetime import datetime

from flask import Blueprint, current_app, redirect, render_template, request, url_for

from .queries import get_anomalies, get_dashboard_metrics, get_event_by_id, search_events
from .sync_service import get_last_sync_info, get_sync_history

bp = Blueprint("pages", __name__)


def _int_param(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except (ValueError, TypeError):
        return default


def _hours_from_window(window: str) -> int:
    return {"24h": 24, "7d": 168, "30d": 720}.get(window, 24)


@bp.route("/")
def index():
    return redirect(url_for("pages.dashboard"))


@bp.route("/search")
def search():
    keyword = request.args.get("q", "").strip()
    upn = request.args.get("upn", "").strip()
    ip = request.args.get("ip", "").strip()
    app_name = request.args.get("app", "").strip()
    country = request.args.get("country", "").strip()
    from_str = request.args.get("from", "")
    to_str = request.args.get("to", "")
    page = _int_param("page", 1)
    per_page = _int_param("per_page", 50)

    from_dt = _parse_dt(from_str)
    to_dt = _parse_dt(to_str)

    error_code_str = request.args.get("error_code", "").strip()
    error_code = int(error_code_str) if error_code_str.isdigit() else None

    results = None
    if any([keyword, upn, ip, app_name, country, from_dt, to_dt, error_code is not None]):
        results = search_events(
            keyword=keyword or None,
            user_principal_name=upn or None,
            ip_address=ip or None,
            app_display_name=app_name or None,
            error_code=error_code,
            country=country or None,
            from_dt=from_dt,
            to_dt=to_dt,
            page=page,
            per_page=per_page,
        )

    return render_template(
        "search.html",
        results=results,
        q=keyword,
        upn=upn,
        ip=ip,
        app=app_name,
        country=country,
        from_str=from_str,
        to_str=to_str,
        error_code=error_code_str,
        page=page,
        per_page=per_page,
    )


@bp.route("/event/<event_id>")
def event_detail(event_id: str):
    event = get_event_by_id(event_id)
    if event is None:
        return render_template("404.html"), 404

    raw_json = json.dumps(event.get("raw_event") or {}, indent=2, default=str)
    return render_template("event_detail.html", event=event, raw_json=raw_json)


@bp.route("/dashboard")
def dashboard():
    window = request.args.get("window", "24h")
    hours = _hours_from_window(window)
    metrics = get_dashboard_metrics(hours=hours)
    anomalies = get_anomalies(hours=hours)
    return render_template(
        "dashboard.html",
        metrics=metrics,
        anomalies=anomalies,
        window=window,
        hours=hours,
    )


@bp.route("/admin/sync-status")
def sync_status():
    info = get_last_sync_info()
    history = get_sync_history(limit=20)
    return render_template("sync_status.html", info=info, history=history)


def _parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None
