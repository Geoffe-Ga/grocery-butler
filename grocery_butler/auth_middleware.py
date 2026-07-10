"""Shared HMAC bearer auth for RubotPaul-callable services.

Vendored from the RubotPaul migration kit (`shared/auth_middleware.py`).
It's deliberately small and dependency-free (stdlib only) so copy-paste is
the right move; resist the urge to package it.

Usage (Flask):

    from grocery_butler.auth_middleware import require_bearer

    @app.post("/api/v1/order/submit")
    def submit_order():
        require_bearer()  # raises 401 if invalid
        ...

Usage (aiohttp):

    from grocery_butler.auth_middleware import aiohttp_auth_middleware

    app = web.Application(middlewares=[aiohttp_auth_middleware])

Token format: "<caller_id>.<timestamp>.<hmac_hex>"
HMAC = HMAC-SHA256(SHARED_SECRET, f"{caller_id}.{timestamp}").hexdigest()
TTL: tokens older than MAX_TOKEN_AGE_SECONDS are rejected.
"""

from __future__ import annotations

import hashlib
import hmac
import logging
import os
import time
from typing import TYPE_CHECKING, Any, Final

if TYPE_CHECKING:
    from collections.abc import Awaitable, Callable

LOG = logging.getLogger("auth")

MAX_TOKEN_AGE_SECONDS: Final[int] = 300  # 5 minutes — backward window
MAX_TOKEN_FUTURE_SKEW_SECONDS: Final[int] = 30  # forward clock skew tolerance
SECRET_ENV_VAR: Final[str] = "RUBOTPAUL_SHARED_SECRET"


class AuthError(Exception):
    """Raised when bearer token is missing or invalid."""

    def __init__(self, reason: str, status: int = 401):
        """Store the failure reason and the HTTP status to respond with."""
        super().__init__(reason)
        self.reason = reason
        self.status = status


def _shared_secret() -> bytes:
    """Return the shared secret from the environment, failing loud if unset."""
    secret = os.environ.get(SECRET_ENV_VAR)
    if not secret:
        # Fail loud at startup, not at first request
        raise RuntimeError(
            f"{SECRET_ENV_VAR} not set; refusing to start auth-protected service"
        )
    return secret.encode()


def _verify_token(token: str, *, now: float | None = None) -> str:
    """Return caller_id if token valid, else raise AuthError."""
    now = now if now is not None else time.time()
    parts = token.split(".")
    if len(parts) != 3:
        raise AuthError("malformed token")
    caller_id, ts_str, sig = parts
    try:
        ts = int(ts_str)
    except ValueError as exc:
        raise AuthError("malformed timestamp") from exc

    # Asymmetric: reject expired tokens, tolerate small forward clock skew only.
    # Using abs() here would let an attacker with a fast clock mint long-lived
    # tokens.
    if now - ts > MAX_TOKEN_AGE_SECONDS:
        raise AuthError("token expired")
    if ts - now > MAX_TOKEN_FUTURE_SKEW_SECONDS:
        raise AuthError("token from future")

    expected = hmac.new(
        _shared_secret(),
        f"{caller_id}.{ts}".encode(),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise AuthError("bad signature")

    return caller_id


def mint_token(caller_id: str, *, now: float | None = None) -> str:
    """Generate a token. Used by RubotPaul-side client code."""
    ts = int(now if now is not None else time.time())
    sig = hmac.new(
        _shared_secret(),
        f"{caller_id}.{ts}".encode(),
        hashlib.sha256,
    ).hexdigest()
    return f"{caller_id}.{ts}.{sig}"


# ---- Flask integration ----------------------------------------------------


def require_bearer() -> str:
    """Flask: validate Authorization header. Returns caller_id; aborts on failure."""
    # Local import keeps this file framework-agnostic.
    from flask import abort, request

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        LOG.warning("auth_missing path=%s", request.path)
        abort(401, description="missing bearer token")
    token = header[len("Bearer ") :]
    try:
        caller_id = _verify_token(token)
    except AuthError as exc:
        LOG.warning("auth_failed reason=%s path=%s", exc.reason, request.path)
        abort(exc.status, description=exc.reason)
    LOG.info("auth_ok caller=%s path=%s", caller_id, request.path)
    return caller_id


# ---- aiohttp integration --------------------------------------------------


async def aiohttp_auth_middleware(
    app: Any, handler: Callable[[Any], Awaitable[Any]]
) -> Callable[[Any], Awaitable[Any]]:
    """aiohttp middleware factory. Use with web.Application(middlewares=[...])."""
    from aiohttp import web

    async def middleware(request: Any) -> Any:
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return web.json_response({"error": "missing bearer token"}, status=401)
        token = header[len("Bearer ") :]
        try:
            caller_id = _verify_token(token)
        except AuthError as exc:
            return web.json_response({"error": exc.reason}, status=exc.status)
        request["caller_id"] = caller_id
        return await handler(request)

    return middleware
