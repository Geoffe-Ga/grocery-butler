"""E2E: brand preference staging via ``/brands/set`` -> confirm/deny.

The web ``/brands/<id>/remove`` form flow is excluded here: it is an
open bug (#63) where ``templates/brands.html`` links using
``loop.index`` instead of the preference's real database id (the
``BrandPreference`` model returned by ``get_brand_preferences`` has no
``id`` field at all, so the template can only be coincidentally
correct). That is a templating defect in a different layer than the
``/api/v1`` blueprint this suite covers, and is reported back rather
than fixed here.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

    from flask.testing import FlaskClient

pytestmark = pytest.mark.e2e


def _brand_body() -> dict[str, str]:
    """Return a valid ``/brands/set`` request body.

    Returns:
        A JSON-serializable request body dict.
    """
    return {
        "match_target": "milk",
        "match_type": "ingredient",
        "brand": "Organic Valley",
        "preference_type": "preferred",
        "notes": "e2e",
    }


def test_brands_set_then_confirm_is_applied(
    client: FlaskClient,
    signed_headers: Callable[..., dict[str, str]],
    no_claude: None,
) -> None:
    """Confirming a staged brand rule persists it to the real RecipeStore."""
    headers = signed_headers()
    staged = client.post("/api/v1/brands/set", json=_brand_body(), headers=headers)
    assert staged.status_code == 200
    body = staged.get_json()
    assert body["status"] == "pending_confirmation"
    action_id = body["action_id"]

    confirmed = client.post(
        "/api/v1/actions/confirm",
        json={"action_id": action_id},
        headers=headers,
    )
    assert confirmed.status_code == 200
    assert confirmed.get_json()["status"] == "approved"

    listed = client.get("/api/v1/brands", headers=headers)
    brands = [b["brand"] for b in listed.get_json()["brands"]]
    assert "Organic Valley" in brands


def test_brands_set_then_deny_is_not_applied(
    client: FlaskClient,
    signed_headers: Callable[..., dict[str, str]],
    no_claude: None,
) -> None:
    """Denying a staged brand rule leaves the RecipeStore untouched."""
    headers = signed_headers()
    staged = client.post("/api/v1/brands/set", json=_brand_body(), headers=headers)
    action_id = staged.get_json()["action_id"]

    denied = client.post(
        "/api/v1/actions/deny",
        json={"action_id": action_id},
        headers=headers,
    )
    assert denied.status_code == 200
    assert denied.get_json()["status"] == "denied"

    listed = client.get("/api/v1/brands", headers=headers)
    brands = [b["brand"] for b in listed.get_json()["brands"]]
    assert "Organic Valley" not in brands
