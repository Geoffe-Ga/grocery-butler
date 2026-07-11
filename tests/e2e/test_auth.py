"""E2E: bearer-token authentication rejects invalid requests, accepts valid ones.

Exercises the real ``grocery_butler.auth_middleware`` module end to end
through a live Flask route -- only the request's ``Authorization``
header changes between scenarios; nothing in the auth module is mocked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

    from flask.testing import FlaskClient

pytestmark = pytest.mark.e2e

INVENTORY_PATH = "/api/v1/inventory"


def test_missing_authorization_returns_401(client: FlaskClient) -> None:
    """A request with no Authorization header is rejected."""
    response = client.get(INVENTORY_PATH)
    assert response.status_code == 401


def test_malformed_token_returns_401(client: FlaskClient) -> None:
    """A syntactically invalid bearer token is rejected."""
    response = client.get(
        INVENTORY_PATH,
        headers={"Authorization": "Bearer not-a-real-token"},
    )
    assert response.status_code == 401


def test_foreign_secret_signature_returns_401(
    client: FlaskClient,
    signed_headers: Callable[..., dict[str, str]],
) -> None:
    """A token signed with a different shared secret is rejected."""
    headers = signed_headers(secret="a-different-shared-secret")
    response = client.get(INVENTORY_PATH, headers=headers)
    assert response.status_code == 401


def test_expired_token_returns_401(
    client: FlaskClient,
    signed_headers: Callable[..., dict[str, str]],
) -> None:
    """A token older than the backward TTL window is rejected."""
    headers = signed_headers(now_offset=-400.0)
    response = client.get(INVENTORY_PATH, headers=headers)
    assert response.status_code == 401


def test_future_dated_token_returns_401(
    client: FlaskClient,
    signed_headers: Callable[..., dict[str, str]],
) -> None:
    """A token minted too far in the future is rejected (clock-skew guard)."""
    headers = signed_headers(now_offset=120.0)
    response = client.get(INVENTORY_PATH, headers=headers)
    assert response.status_code == 401


def test_valid_token_on_protected_route_returns_200(
    client: FlaskClient,
    signed_headers: Callable[..., dict[str, str]],
) -> None:
    """A validly signed, fresh token is accepted."""
    response = client.get(INVENTORY_PATH, headers=signed_headers())
    assert response.status_code == 200
    assert response.get_json() == {"items": []}
