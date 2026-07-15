"""E2E: Safeway order preview/submit/confirm over a real pipeline + MockTransport.

Every layer (``SafewayPipeline``, ``CartBuilder``, ``ProductSearchService``,
``ProductSelector``, ``SubstitutionService``, ``OrderService``) runs for
real; only the httpx transport inside ``SafewayClient`` is mocked.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from grocery_butler.safeway_client import OKTA_CLIENT_ID

if TYPE_CHECKING:
    from collections.abc import Callable

    from flask.testing import FlaskClient

    from tests.e2e.conftest import SafewayMockState

pytestmark = pytest.mark.e2e


def _shopping_list_body(ingredients: list[str]) -> dict[str, object]:
    """Return an ``/order/preview`` request body for the given ingredients.

    Args:
        ingredients: Ingredient names to include, each treated as its
            own search term with a simple default quantity/unit/category.

    Returns:
        A JSON-serializable request body dict.
    """
    return {
        "shopping_list": [
            {
                "ingredient": name,
                "quantity": 1.0,
                "unit": "each",
                "category": "other",
                "search_term": name,
                "from_meals": ["manual"],
            }
            for name in ingredients
        ]
    }


def test_order_preview_happy_path_traverses_full_pipeline(
    client: FlaskClient,
    signed_headers: Callable[..., dict[str, str]],
    no_claude: None,
    safeway_mock: SafewayMockState,
) -> None:
    """A clean shopping list builds a cart and hits every pipeline stage."""
    headers = signed_headers()
    response = client.post(
        "/api/v1/order/preview",
        json=_shopping_list_body(["milk", "bread"]),
        headers=headers,
    )
    assert response.status_code == 200
    body = response.get_json()
    cart = body["cart"]
    assert len(cart["items"]) == 2
    assert cart["failed_items"] == []
    assert cart["substituted_items"] == []
    assert cart["subtotal"] == pytest.approx(2 * 3.99)
    assert body["total"] == "7.98"

    assert "/api/v1/authn" in safeway_mock.requested_paths
    assert f"/oauth2/{OKTA_CLIENT_ID}/v1/authorize" in safeway_mock.requested_paths
    assert "/api/v2/grocerystore/search" in safeway_mock.requested_paths
    assert any(p.endswith("/fulfillment") for p in safeway_mock.requested_paths)


def test_order_preview_substitution_reflected_in_response(
    client: FlaskClient,
    signed_headers: Callable[..., dict[str, str]],
    no_claude: None,
    safeway_mock: SafewayMockState,
) -> None:
    """An out-of-stock primary product surfaces a priced substitute.

    Regression guard for issue #70: previously the selected substitute
    was tracked only in ``substituted_items`` and never converted into
    a priced ``CartItem``, so it was silently dropped from the
    submitted order. Per the chief-architect's ruling, the selected
    substitute must now also appear in ``cart["items"]``, correctly
    priced and always flagged ``needs_review`` with
    ``review_reason="substitution"`` (a human must confirm every
    substitution), while ``substituted_items`` continues to carry the
    same provenance record as before.
    """
    headers = signed_headers()
    safeway_mock.oos_once.add("bell pepper")
    safeway_mock.available_products["bell pepper"] = [
        {
            "upc": "upc-bell-pepper-alt",
            "name": "Generic Bell Pepper (Alt)",
            "price": 1.49,
            "size": "1 each",
            "inStock": True,
        },
    ]

    response = client.post(
        "/api/v1/order/preview",
        json=_shopping_list_body(["bell pepper"]),
        headers=headers,
    )
    assert response.status_code == 200
    cart = response.get_json()["cart"]

    assert len(cart["items"]) == 1
    substitute_item = cart["items"][0]
    assert substitute_item["safeway_product"]["product_id"] == "upc-bell-pepper-alt"
    assert substitute_item["quantity_to_order"] == 1
    assert substitute_item["estimated_cost"] == 1.49
    assert substitute_item["needs_review"] is True
    assert substitute_item["review_reason"] == "substitution"
    assert cart["subtotal"] == 1.49

    assert len(cart["substituted_items"]) == 1
    substitution = cart["substituted_items"][0]
    assert substitution["status"] == "alternatives_found"
    assert substitution["original_item"]["ingredient"] == "bell pepper"
    assert substitution["selected"] is not None
    assert substitution["selected"]["product"]["name"] == "Generic Bell Pepper (Alt)"


def test_order_preview_empty_search_yields_failed_item(
    client: FlaskClient,
    signed_headers: Callable[..., dict[str, str]],
    no_claude: None,
    safeway_mock: SafewayMockState,
) -> None:
    """An ingredient with no search results ends up in failed_items."""
    headers = signed_headers()
    safeway_mock.force_search_empty = True

    response = client.post(
        "/api/v1/order/preview",
        json=_shopping_list_body(["unobtainium"]),
        headers=headers,
    )
    assert response.status_code == 200
    cart = response.get_json()["cart"]
    assert cart["items"] == []
    assert [item["ingredient"] for item in cart["failed_items"]] == ["unobtainium"]


def test_order_preview_auth_failure_returns_503(
    client: FlaskClient,
    signed_headers: Callable[..., dict[str, str]],
    no_claude: None,
    safeway_mock: SafewayMockState,
) -> None:
    """A failing Okta authn step surfaces as a 503, not a 500.

    Issue #77: the body is the terse, stable "cart build failed"
    message rather than one echoing the underlying Okta/auth exception
    text -- that detail is logged server-side instead of relayed to the
    client.
    """
    headers = signed_headers()
    safeway_mock.force_auth_fail = True

    response = client.post(
        "/api/v1/order/preview",
        json=_shopping_list_body(["milk"]),
        headers=headers,
    )
    assert response.status_code == 503
    assert response.get_json()["error"] == "cart build failed"


def test_order_submit_confirm_hits_orders_endpoint_with_confirmation(
    client: FlaskClient,
    signed_headers: Callable[..., dict[str, str]],
    no_claude: None,
    safeway_mock: SafewayMockState,
) -> None:
    """Confirming a staged, non-substituted cart really submits the order."""
    headers = signed_headers()
    preview = client.post(
        "/api/v1/order/preview",
        json=_shopping_list_body(["milk", "eggs"]),
        headers=headers,
    )
    body = preview.get_json()
    assert body["cart"]["substituted_items"] == []

    submitted = client.post(
        "/api/v1/order/submit",
        json={"cart": body["cart"], "total": body["total"]},
        headers=headers,
    )
    submit_body = submitted.get_json()
    action_id = submit_body["action_id"]

    # "milk" and "eggs" are searched with unit="each" (see
    # _shopping_list_body) against the mock's uniform "1 lb" product
    # size, so the real quantity calculator flags both
    # incomparable_units (issue #59). Per the chief-architect's ruling,
    # the staged message must name them and their reason before the
    # human is asked to confirm.
    staged_message = submit_body["message"]
    assert "milk" in staged_message
    assert "eggs" in staged_message
    assert "incomparable_units" in staged_message

    confirmed = client.post(
        "/api/v1/actions/confirm",
        json={"action_id": action_id},
        headers=headers,
    )
    assert confirmed.status_code == 200
    result = confirmed.get_json()["result"]
    assert result["order_id"] == "e2e-order-1"
    assert result["status"] == "confirmed"
    assert isinstance(result["items_restocked"], int)
    assert safeway_mock.requested_paths.count("/abs/pub/web/orders") == 1
