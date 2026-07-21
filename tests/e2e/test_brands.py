"""E2E: brand preference staging via ``/brands/set`` -> confirm/deny.

The web ``/brands/<id>/remove`` form flow lives in a different layer
than the ``/api/v1`` blueprint this suite covers. It is exercised by
the template-rendering and removal tests in ``tests/test_app.py``
(added for issue #63, which fixed ``templates/brands.html`` to post
the preference's real database id instead of ``loop.index``).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

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
    signed_request: Callable[..., Any],
    no_claude: None,
) -> None:
    """Confirming a staged brand rule persists it to the real RecipeStore."""
    staged = signed_request("POST", "/api/v1/brands/set", _brand_body())
    assert staged.status_code == 200
    body = staged.get_json()
    assert body["status"] == "pending_confirmation"
    action_id = body["action_id"]

    confirmed = signed_request(
        "POST", "/api/v1/actions/confirm", {"action_id": action_id}
    )
    assert confirmed.status_code == 200
    assert confirmed.get_json()["status"] == "approved"

    listed = signed_request("GET", "/api/v1/brands")
    brands = [b["brand"] for b in listed.get_json()["brands"]]
    assert "Organic Valley" in brands


def test_brands_set_then_deny_is_not_applied(
    signed_request: Callable[..., Any],
    no_claude: None,
) -> None:
    """Denying a staged brand rule leaves the RecipeStore untouched."""
    staged = signed_request("POST", "/api/v1/brands/set", _brand_body())
    action_id = staged.get_json()["action_id"]

    denied = signed_request("POST", "/api/v1/actions/deny", {"action_id": action_id})
    assert denied.status_code == 200
    assert denied.get_json()["status"] == "denied"

    listed = signed_request("GET", "/api/v1/brands")
    brands = [b["brand"] for b in listed.get_json()["brands"]]
    assert "Organic Valley" not in brands
