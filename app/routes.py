"""Server-rendered Flask routes (HTML pages)."""
from __future__ import annotations

import json
import logging
from datetime import datetime

from flask import (
    Blueprint,
    current_app,
    redirect,
    render_template,
    request,
    session,
    url_for,
)

logger = logging.getLogger(__name__)

from .archive_service import (
    get_archive_status,
    get_archived_day_record,
    list_archived_days,
    load_archived_day,
)
from .oauth import complete_auth_flow, login_required, start_auth_flow
from .queries import get_anomalies, get_dashboard_metrics, get_event_by_id, search_events
from .sync_service import get_last_sync_info, get_sync_history

bp = Blueprint("pages", __name__)


# ── Auth routes ───────────────────────────────────────────────────────────────

@bp.route("/auth/login")
def auth_login():
    cfg = current_app.config["APP_CONFIG"]
    if not cfg.OAUTH_ENABLED:
        return redirect(url_for("pages.dashboard"))

    # Preserve the intended destination through the OAuth round-trip
    session["next_url"] = request.args.get("next", "")

    try:
        flow = start_auth_flow(cfg)
    except Exception as exc:
        logger.exception("Failed to start OAuth flow: %s", exc)
        return render_template(
            "auth_error.html",
            error="oauth_init_failed",
            description=f"MSAL error: {exc}",
        ), 500

    session["auth_flow"] = flow
    return redirect(flow["auth_uri"])


@bp.route("/auth/callback")
def auth_callback():
    cfg = current_app.config["APP_CONFIG"]
    if not cfg.OAUTH_ENABLED:
        return redirect(url_for("pages.dashboard"))

    result = complete_auth_flow(
        cfg,
        session.pop("auth_flow", {}),
        request.args,
    )

    if "error" in result:
        return render_template(
            "auth_error.html",
            error=result.get("error"),
            description=result.get("error_description", ""),
        ), 400

    session["user"] = result.get("id_token_claims", {})
    # Read next URL from session (it's not in request.args after OAuth redirect)
    next_url = session.pop("next_url", "") or url_for("pages.dashboard")
    return redirect(next_url)


@bp.route("/auth/logout")
def auth_logout():
    cfg = current_app.config["APP_CONFIG"]
    session.clear()
    if cfg.OAUTH_ENABLED:
        post_logout = url_for("pages.index", _external=True)
        ms_logout = (
            f"https://login.microsoftonline.com/{cfg.TENANT_ID}/oauth2/v2.0/logout"
            f"?post_logout_redirect_uri={post_logout}"
        )
        return redirect(ms_logout)
    return redirect(url_for("pages.index"))


# ── Application routes ────────────────────────────────────────────────────────

@bp.route("/")
@login_required
def index():
    return redirect(url_for("pages.dashboard"))


@bp.route("/search")
@login_required
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
@login_required
def event_detail(event_id: str):
    event = get_event_by_id(event_id)
    if event is None:
        return render_template("404.html"), 404

    raw_json = json.dumps(event.get("raw_event") or {}, indent=2, default=str)
    return render_template("event_detail.html", event=event, raw_json=raw_json)


SIGNIN_TYPE_FILTERS = {
    "interactive": ["interactiveUser"],
    "noninteractive": ["nonInteractiveUser"],
    "serviceprincipal": ["servicePrincipal"],
    "managedidentity": ["managedIdentity"],
    "users": ["interactiveUser", "nonInteractiveUser"],
    "all": [
        "interactiveUser",
        "nonInteractiveUser",
        "servicePrincipal",
        "managedIdentity",
    ],
}


@bp.route("/dashboard")
@login_required
def dashboard():
    window = request.args.get("window", "24h")
    hours = _hours_from_window(window)

    type_key = request.args.get("types", "interactive")
    if type_key not in SIGNIN_TYPE_FILTERS:
        type_key = "interactive"
    signin_types = SIGNIN_TYPE_FILTERS[type_key]

    metrics = get_dashboard_metrics(hours=hours, signin_types=signin_types)
    anomalies = get_anomalies(hours=hours)
    return render_template(
        "dashboard.html",
        metrics=metrics,
        anomalies=anomalies,
        window=window,
        hours=hours,
        type_key=type_key,
    )


@bp.route("/admin/archive")
@login_required
def archive_index():
    cfg = current_app.config["APP_CONFIG"]
    days = list_archived_days()
    return render_template(
        "archive_list.html",
        days=days,
        archive_configured=cfg.archive_configured,
    )


@bp.route("/admin/archive/<day_str>")
@login_required
def archive_day(day_str: str):
    cfg = current_app.config["APP_CONFIG"]
    try:
        day = datetime.strptime(day_str, "%Y-%m-%d").date()
    except ValueError:
        return render_template("404.html"), 404

    record = get_archived_day_record(day)
    if record is None or record.get("status") != "success":
        return render_template("404.html"), 404

    page = _int_param("page", 1)
    per_page = _int_param("per_page", 50)
    upn = request.args.get("upn", "").strip().lower()
    ip = request.args.get("ip", "").strip().lower()

    try:
        rows = load_archived_day(cfg, day)
    except Exception as exc:
        logger.exception("Failed to load archive for %s", day)
        return render_template(
            "archive_day.html",
            day=day,
            record=record,
            rows=[],
            page=1,
            per_page=per_page,
            total=0,
            pages=0,
            upn=upn,
            ip=ip,
            error=str(exc),
        )

    filtered = rows
    if upn:
        filtered = [
            r for r in filtered
            if (r.get("user_principal_name") or "").lower().find(upn) != -1
        ]
    if ip:
        filtered = [
            r for r in filtered
            if (r.get("ip_address") or "").lower().find(ip) != -1
        ]

    total = len(filtered)
    pages = (total + per_page - 1) // per_page if total else 0
    start = (page - 1) * per_page
    page_rows = filtered[start : start + per_page]

    return render_template(
        "archive_day.html",
        day=day,
        record=record,
        rows=page_rows,
        page=page,
        per_page=per_page,
        total=total,
        pages=pages,
        upn=upn,
        ip=ip,
        error=None,
    )


@bp.route("/admin/archive/<day_str>/event/<event_id>")
@login_required
def archive_event(day_str: str, event_id: str):
    cfg = current_app.config["APP_CONFIG"]
    try:
        day = datetime.strptime(day_str, "%Y-%m-%d").date()
    except ValueError:
        return render_template("404.html"), 404

    try:
        rows = load_archived_day(cfg, day)
    except Exception:
        logger.exception("Failed to load archive for %s", day)
        return render_template("404.html"), 404

    event = next((r for r in rows if r.get("id") == event_id), None)
    if event is None:
        return render_template("404.html"), 404

    raw_json = json.dumps(event.get("raw_event") or {}, indent=2, default=str)
    return render_template(
        "event_detail.html",
        event=event,
        raw_json=raw_json,
        archived=True,
        archived_day=day,
    )


@bp.route("/admin/sync-status")
@login_required
def sync_status():
    cfg = current_app.config["APP_CONFIG"]
    info = get_last_sync_info()
    history = get_sync_history(limit=20)
    archive = get_archive_status(cfg)
    return render_template(
        "sync_status.html",
        info=info,
        history=history,
        archive=archive,
    )


# ── Helpers ───────────────────────────────────────────────────────────────────

def _int_param(name: str, default: int) -> int:
    try:
        return int(request.args.get(name, default))
    except (ValueError, TypeError):
        return default


def _hours_from_window(window: str) -> int:
    return {"24h": 24, "7d": 168, "30d": 720}.get(window, 24)


def _parse_dt(s: str) -> datetime | None:
    if not s:
        return None
    for fmt in ("%Y-%m-%dT%H:%M", "%Y-%m-%d"):
        try:
            return datetime.strptime(s, fmt)
        except ValueError:
            continue
    return None
