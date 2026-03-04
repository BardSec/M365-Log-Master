"""Optional HTTP Basic Auth middleware."""
from __future__ import annotations

import base64
import functools
from typing import Callable

from flask import request, Response


def check_basic_auth(username: str, password: str) -> bool:
    auth = request.headers.get("Authorization", "")
    if not auth.startswith("Basic "):
        return False
    try:
        decoded = base64.b64decode(auth[6:]).decode("utf-8")
        u, _, p = decoded.partition(":")
        return u == username and p == password
    except Exception:
        return False


def requires_basic_auth(username: str, password: str):
    """Decorator factory. Skip auth if username/password are empty."""
    def decorator(fn: Callable) -> Callable:
        @functools.wraps(fn)
        def wrapper(*args, **kwargs):
            if username and password:
                if not check_basic_auth(username, password):
                    return Response(
                        "Unauthorized",
                        401,
                        {"WWW-Authenticate": 'Basic realm="M365 Log Master"'},
                    )
            return fn(*args, **kwargs)
        return wrapper
    return decorator
