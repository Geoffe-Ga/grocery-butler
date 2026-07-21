"""E2E: pantry staple web-form add/remove, verified through the JSON API.

The web routes (``/pantry/add``, ``/pantry/<id>/remove``) don't require
a bearer token; the read-back goes through the real bearer-protected
``/api/v1/pantry`` endpoint.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

    from flask.testing import FlaskClient

pytestmark = pytest.mark.e2e


def _staple_names(signed_request: Callable[..., Any]) -> list[str]:
    """Return the ingredient names of all tracked pantry staples.

    Args:
        signed_request: The bound-token request-sending fixture.

    Returns:
        List of ingredient name strings currently tracked as staples.
    """
    response = signed_request("GET", "/api/v1/pantry")
    return [s["ingredient"] for s in response.get_json()["staples"]]


def test_pantry_add_form_reflected_in_api_and_duplicate_is_safe(
    client: FlaskClient,
    signed_request: Callable[..., Any],
) -> None:
    """Adding a staple via the web form shows up through the API; dup is safe."""
    added = client.post(
        "/pantry/add",
        data={"ingredient": "cumin", "category": "pantry_dry"},
    )
    assert added.status_code in (302, 303)
    assert "cumin" in _staple_names(signed_request)

    duplicate = client.post(
        "/pantry/add",
        data={"ingredient": "cumin", "category": "pantry_dry"},
    )
    assert duplicate.status_code in (302, 303)
    assert _staple_names(signed_request).count("cumin") == 1


def test_pantry_remove_form_no_longer_listed(
    client: FlaskClient,
    signed_request: Callable[..., Any],
) -> None:
    """Removing a staple via its web form drops it from the API listing."""
    client.post("/pantry/add", data={"ingredient": "cumin", "category": "pantry_dry"})

    response = signed_request("GET", "/api/v1/pantry")
    staple = next(
        s for s in response.get_json()["staples"] if s["ingredient"] == "cumin"
    )

    removed = client.post(f"/pantry/{staple['id']}/remove")
    assert removed.status_code in (302, 303)
    assert "cumin" not in _staple_names(signed_request)
