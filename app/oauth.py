"""Microsoft OAuth 2.0 (authorization-code flow) helpers and login_required decorator."""
from __future__ import annotations

import functools
import logging
from typing import Callable

import msal
from flask import current_app, jsonify, redirect, request, session, url_for

logger = logging.getLogger(__name__)

# Scopes requested during user login – covers basic profile info only.
# Graph data is fetched via the separate client-credentials token.
OAUTH_SCOPES = ["openid", "profile", "email"]


def _msal_app(cfg) -> msal.ConfidentialClientApplication:
    authority = f"https://login.microsoftonline.com/{cfg.TENANT_ID}"
    return msal.ConfidentialClientApplication(
        cfg.CLIENT_ID,
        authority=authority,
        client_credential=cfg.CLIENT_SECRET,
    )


def start_auth_flow(cfg) -> dict:
    """Begin the OIDC auth-code flow and return the flow dict (store in session)."""
    app = _msal_app(cfg)
    return app.initiate_auth_code_flow(
        OAUTH_SCOPES,
        redirect_uri=cfg.OAUTH_REDIRECT_URI,
    )


def complete_auth_flow(cfg, saved_flow: dict, callback_args: dict) -> dict:
    """
    Exchange the callback params for tokens.
    Returns the MSAL result dict; check for 'error' key on failure.
    """
    app = _msal_app(cfg)
    return app.acquire_token_by_auth_code_flow(saved_flow, callback_args)


def login_required(fn: Callable) -> Callable:
    """
    Decorator that enforces authentication when OAUTH_ENABLED=true.

    - API routes (/api/*): return JSON 401.
    - Page routes: redirect to /auth/login.
    """
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        cfg = current_app.config["APP_CONFIG"]
        if cfg.OAUTH_ENABLED and "user" not in session:
            if request.path.startswith("/api/"):
                return jsonify({"error": "Unauthorized – please log in."}), 401
            return redirect(url_for("pages.auth_login", next=request.url))
        return fn(*args, **kwargs)
    return wrapper
