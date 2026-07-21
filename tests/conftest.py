"""Shared pytest fixtures and helpers for the grocery-butler test suite.

Centralizes the single source of truth for minting request-bound bearer
tokens in tests (issue #74): every test module that needs an
``Authorization`` header for a specific HTTP request should call
:func:`bearer_header` rather than hand-rolling ``mint_token`` calls, so
the whole suite stays in sync with the ``RequestBinding`` contract in
``grocery_butler.auth_middleware``.
"""

from __future__ import annotations

from grocery_butler.auth_middleware import RequestBinding, mint_token


def bearer_header(
    caller_id: str, method: str, path: str, body: bytes = b""
) -> dict[str, str]:
    """Return an ``Authorization`` header bound to one exact HTTP request.

    Mints a token whose signature is bound to ``method``, ``path``, and a
    SHA-256 digest of ``body`` via
    :class:`~grocery_butler.auth_middleware.RequestBinding`, matching what
    the server-side verifier reconstructs from the live request. A header
    minted for one request is not valid for a different method, path, or
    body -- that is the whole point of the request-bound token contract
    (issue #74).

    Args:
        caller_id: The caller identity to embed in the token.
        method: The HTTP method the token will be sent with (e.g.
            ``"GET"``, ``"POST"``). Case does not matter -- the signing
            payload canonicalizes it to uppercase.
        path: The exact request path the token will be sent to (e.g.
            ``"/api/v1/inventory"``).
        body: The exact raw request body bytes that will be sent, if
            any. Defaults to an empty body.

    Returns:
        A single-entry dict suitable for passing as test-client
        ``headers``, e.g. ``{"Authorization": "Bearer <token>"}``.
    """
    binding = RequestBinding.of(method, path, body)
    token = mint_token(caller_id, binding)
    return {"Authorization": f"Bearer {token}"}
