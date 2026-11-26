"""Shared pytest fixtures and helpers for the grocery-butler test suite.

Centralizes the single source of truth for minting request-bound bearer
tokens in tests (issue #74): every test module that needs an
``Authorization`` header for a specific HTTP request should call
:func:`bearer_header` rather than hand-rolling ``mint_token`` calls, so
the whole suite stays in sync with the ``RequestBinding`` contract in
``grocery_butler.auth_middleware``.
"""

from __future__ import annotations

import pytest

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


class RealProviderContactedError(AssertionError):
    """Raised when a test would have opened a connection to Instacart."""

    def __init__(self, host: str) -> None:
        """Name the host the test tried to reach.

        Args:
            host: The host that would have been contacted.
        """
        super().__init__(
            f"A test tried to contact {host}. Tests use a fake transport or a"
            " fake sender and never call Instacart."
        )


@pytest.fixture(autouse=True)
def _no_real_provider_calls(monkeypatch: pytest.MonkeyPatch) -> None:
    """Fail any test that reaches the real HTTPS sender.

    ``HttpTransport`` looks its default sender up when it is called, so
    replacing it here covers every transport a test builds without a fake.
    """
    from grocery_butler.fulfillment.instacart import http_transport

    def refuse(host: str, *_args: object) -> http_transport.ProviderResponse:
        raise RealProviderContactedError(host)

    monkeypatch.setattr(http_transport, "_send_https", refuse)
