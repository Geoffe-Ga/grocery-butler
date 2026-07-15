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
HMAC = HMAC-SHA256(
    SHARED_SECRET,
    "\\x00".join((caller_id, str(timestamp), method.upper(), path, body_sha256)),
).hexdigest()
The method/path/body-hash triple is a :class:`RequestBinding`: the
signature is bound to the exact request it authorizes, so a token minted
for one endpoint cannot be replayed against another. TTL: tokens older
than ``MAX_TOKEN_AGE_SECONDS`` are rejected.

Security posture (issue #74)
-----------------------------
* **Cross-endpoint replay: CLOSED.** Method, path, and a SHA-256 hash of
  the body are bound into the HMAC, and tokens are minted per-request.
  A token stolen from one request cannot authorize a different
  method/path/body triple.
* **Same-request replay within the 5-minute TTL: ACCEPTED/DEFERRED.**
  Binding a request does not stop the *same* token from being replayed
  against the *same* endpoint again before it expires. This is
  mitigated in depth by the pending-action state machine: destructive
  operations stage first and only execute via ``/actions/confirm``,
  which claims the row exactly once (a second confirm returns 409), and
  the staged ``action_id`` doubles as the Safeway idempotency key. A
  replayed confirm therefore cannot double-submit. A nonce cache would
  close this window fully but is deliberately out of scope for #74.
* **Distinct confirm credential / second factor: DEFERRED.** A second
  secret held by the same calling principal (RubotPaul) would add
  little defense-in-depth. The real second factor in this system is
  product-level: stage -> human-reviewed chat message -> confirm. A
  human-held second factor is tracked as follow-up work, not a blocker
  for #74.
"""

from __future__ import annotations

import dataclasses
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


@dataclasses.dataclass(frozen=True)
class RequestBinding:
    """The (method, path, body) triple a bearer token's signature is bound to.

    Issue #74: binding tokens to the exact request they authorize closes
    the replay window where a token minted for one endpoint (e.g. a
    harmless read) could be reused against any other endpoint (e.g. a
    destructive write) within its TTL.

    Attributes:
        method: The HTTP method, stored verbatim (uncanonicalized) as
            given to :meth:`of`.
        path: The request path, stored verbatim as given to :meth:`of`.
        body_sha256: Hex SHA-256 digest of the exact request body bytes.
    """

    method: str
    path: str
    body_sha256: str

    @classmethod
    def of(cls, method: str, path: str, body: bytes) -> RequestBinding:
        """Build a RequestBinding from a request's method, path, and body.

        Args:
            method: The HTTP method of the request (case preserved
                as-given; canonicalization happens later, only inside
                the signing payload).
            path: The exact request path.
            body: The exact raw request body bytes.

        Returns:
            A RequestBinding with ``body`` hashed via SHA-256.
        """
        return cls(
            method=method, path=path, body_sha256=hashlib.sha256(body).hexdigest()
        )


def _signing_payload(caller_id: str, ts: int, binding: RequestBinding) -> bytes:
    """Build the exact bytes signed/verified for a request-bound token.

    Fields are joined with a NUL delimiter (rather than the legacy dotted
    concatenation) so no combination of field values can be ambiguously
    reinterpreted as a different field split -- the wire token format
    (``<caller_id>.<ts>.<hmac_hex>``) is unaffected; only the bytes fed
    into the HMAC change. The method is canonicalized to uppercase here
    (and only here) so minting against a lowercase method and verifying
    against the uppercase form Flask/aiohttp always report still match.

    Args:
        caller_id: The caller identity embedded in the token.
        ts: The token's Unix timestamp.
        binding: The request the token is bound to.

    Returns:
        The UTF-8 encoded payload to sign or verify.
    """
    return "\x00".join(
        (
            caller_id,
            str(ts),
            binding.method.upper(),
            binding.path,
            binding.body_sha256,
        )
    ).encode()


def _verify_token(
    token: str, binding: RequestBinding, *, now: float | None = None
) -> str:
    """Return caller_id if token is valid for the given binding, else raise.

    Args:
        token: The bearer token string (``<caller_id>.<ts>.<hmac_hex>``).
        binding: The request the token must be bound to.
        now: Override for the current time (epoch seconds); defaults to
            the wall clock.

    Returns:
        The caller_id embedded in the token.

    Raises:
        AuthError: If the token is malformed, expired, from the future,
            or its signature does not match ``binding``.
    """
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
        _signing_payload(caller_id, ts, binding),
        hashlib.sha256,
    ).hexdigest()
    if not hmac.compare_digest(expected, sig):
        raise AuthError("bad signature")

    return caller_id


def mint_token(
    caller_id: str, binding: RequestBinding, *, now: float | None = None
) -> str:
    """Generate a token bound to one exact request. Used by RubotPaul.

    Args:
        caller_id: The caller identity to embed in the token.
        binding: The request (method, path, body) this token authorizes.
        now: Override for the mint time (epoch seconds); defaults to the
            wall clock.

    Returns:
        The token string ``<caller_id>.<ts>.<hmac_hex>``.
    """
    ts = int(now if now is not None else time.time())
    sig = hmac.new(
        _shared_secret(),
        _signing_payload(caller_id, ts, binding),
        hashlib.sha256,
    ).hexdigest()
    return f"{caller_id}.{ts}.{sig}"


# ---- Flask integration ----------------------------------------------------


def require_bearer() -> str:
    """Flask: validate Authorization header against the live request.

    Builds the :class:`RequestBinding` from the current Flask request's
    method, path, and raw body bytes (via ``request.get_data()``, which
    Werkzeug caches so the view can still call ``request.get_json()``
    afterward).

    Returns:
        The caller_id embedded in the validated token.

    Raises:
        werkzeug.exceptions.HTTPException: Aborts with 401 (or the
            AuthError's status) when the header is missing or the token
            is invalid for this request.
    """
    # Local import keeps this file framework-agnostic.
    from flask import abort, request

    header = request.headers.get("Authorization", "")
    if not header.startswith("Bearer "):
        LOG.warning("auth_missing path=%s", request.path)
        abort(401, description="missing bearer token")
    token = header[len("Bearer ") :]
    binding = RequestBinding.of(request.method, request.path, request.get_data())
    try:
        caller_id = _verify_token(token, binding)
    except AuthError as exc:
        LOG.warning("auth_failed reason=%s path=%s", exc.reason, request.path)
        abort(exc.status, description=exc.reason)
    LOG.info("auth_ok caller=%s path=%s", caller_id, request.path)
    return caller_id


# ---- aiohttp integration --------------------------------------------------


async def aiohttp_auth_middleware(
    app: Any, handler: Callable[[Any], Awaitable[Any]]
) -> Callable[[Any], Awaitable[Any]]:
    """aiohttp middleware factory. Use with web.Application(middlewares=[...]).

    Args:
        app: The aiohttp application (unused; required by the middleware
            factory signature).
        handler: The next handler in the middleware chain.

    Returns:
        A middleware callable that validates the bearer token against a
        :class:`RequestBinding` built from the request's method, path,
        and body before delegating to ``handler``.
    """
    from aiohttp import web

    async def middleware(request: Any) -> Any:
        """Validate the request's bearer token, then delegate to handler."""
        header = request.headers.get("Authorization", "")
        if not header.startswith("Bearer "):
            return web.json_response({"error": "missing bearer token"}, status=401)
        token = header[len("Bearer ") :]
        body = await request.read()
        binding = RequestBinding.of(request.method, request.path, body)
        try:
            caller_id = _verify_token(token, binding)
        except AuthError as exc:
            return web.json_response({"error": exc.reason}, status=exc.status)
        request["caller_id"] = caller_id
        return await handler(request)

    return middleware
