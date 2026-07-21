"""Shared pytest fixtures and helpers for the grocery-butler test suite.

Centralizes the single source of truth for minting request-bound bearer
tokens in tests (issue #74): every test module that needs an
``Authorization`` header for a specific HTTP request should call
:func:`bearer_header` rather than hand-rolling ``mint_token`` calls, so
the whole suite stays in sync with the ``RequestBinding`` contract in
``grocery_butler.auth_middleware``.

Also hosts the Issue #78 autouse fixture: ``grocery_butler.db`` has a
run-once ``init_db`` guard (a module-level ``_initialized_paths``
registry) that persists for the lifetime of the Python process --
including across tests within the same pytest session. Without a reset
between tests, one test's ``init_db(path)`` call could make a later,
unrelated test's ``init_db(path)`` call silently short-circuit (or vice
versa for ``:memory:``), producing order-dependent test pollution. The
``_reset_db_init_state`` fixture resets that state via
``grocery_butler.db._reset_init_state()`` before and after every test.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

import grocery_butler.db as db_module
from grocery_butler.auth_middleware import RequestBinding, mint_token

if TYPE_CHECKING:
    from collections.abc import Iterator


@pytest.fixture(autouse=True)
def _reset_db_init_state() -> Iterator[None]:
    """Reset grocery_butler.db's run-once init state around each test.

    Looked up via ``getattr`` so a rename or removal of the test-only
    ``_reset_init_state`` hook degrades this fixture to a no-op instead
    of failing test collection (Issue #78).

    Yields:
        None. Control returns to the test between the pre- and
        post-test resets.
    """
    reset = getattr(db_module, "_reset_init_state", None)
    if reset is not None:
        reset()
    yield
    if reset is not None:
        reset()


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
