"""E2E: the full happy-path chain from meal text to a confirmed order.

Exercises every layer with real wiring -- ``MealParser`` + a seeded
recipe, ``Consolidator``, ``SafewayPipeline`` (``CartBuilder`` +
``OrderService``) over a ``MockTransport``, and the pending-actions
staging/confirmation flow. Uses only items that resolve cleanly (no
substitutions): bug #70 blocks driving a substituted cart through
submit -> confirm, so that path is covered (and excluded past preview)
separately in ``test_ordering.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

from grocery_butler.models import ParsedMeal, PendingActionStatus
from grocery_butler.pending_actions import PendingActionsStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from flask.testing import FlaskClient

    from tests.e2e.conftest import SafewayMockState

pytestmark = pytest.mark.e2e


def _stage_order(
    client: FlaskClient,
    headers: dict[str, str],
    meal_name: str,
) -> str:
    """Run meals/parse -> shopping-list/preview -> order/preview -> submit.

    Args:
        client: The Flask test client.
        headers: Bearer auth headers.
        meal_name: Name of a recipe already resolvable via the recipe store.

    Returns:
        The ``action_id`` of the staged ``safeway_order_submit`` action.
    """
    parsed = client.post(
        "/api/v1/meals/parse",
        json={"text": meal_name},
        headers=headers,
    )
    meals = parsed.get_json()["meals"]
    preview = client.post(
        "/api/v1/shopping-list/preview",
        json={"meals": meals, "include_restock": False},
        headers=headers,
    )
    shopping_list = preview.get_json()["items"]
    order_preview = client.post(
        "/api/v1/order/preview",
        json={"shopping_list": shopping_list},
        headers=headers,
    )
    order_body = order_preview.get_json()
    submitted = client.post(
        "/api/v1/order/submit",
        json={"cart": order_body["cart"], "total": order_body["total"]},
        headers=headers,
    )
    action_id: str = submitted.get_json()["action_id"]
    return action_id


def test_full_chain_meal_to_confirmed_order(
    client: FlaskClient,
    signed_headers: Callable[..., dict[str, str]],
    seed_recipe: ParsedMeal,
    no_claude: None,
    safeway_mock: SafewayMockState,
    db_path: str,
) -> None:
    """meals/parse -> shopping-list/preview -> order/preview -> submit -> confirm."""
    headers = signed_headers()

    parsed = client.post(
        "/api/v1/meals/parse",
        json={"text": seed_recipe.name},
        headers=headers,
    )
    assert parsed.status_code == 200
    meals = parsed.get_json()["meals"]
    assert len(meals) == 1
    assert meals[0]["known_recipe"] is True

    preview = client.post(
        "/api/v1/shopping-list/preview",
        json={"meals": meals, "include_restock": False},
        headers=headers,
    )
    assert preview.status_code == 200
    shopping_list = preview.get_json()["items"]
    assert {item["ingredient"] for item in shopping_list} == {
        "ground beef",
        "spaghetti",
        "tomato sauce",
    }

    order_preview = client.post(
        "/api/v1/order/preview",
        json={"shopping_list": shopping_list},
        headers=headers,
    )
    assert order_preview.status_code == 200
    order_body = order_preview.get_json()
    cart = order_body["cart"]
    assert cart["failed_items"] == []
    assert cart["substituted_items"] == []
    assert len(cart["items"]) == 3

    submitted = client.post(
        "/api/v1/order/submit",
        json={"cart": cart, "total": order_body["total"]},
        headers=headers,
    )
    assert submitted.status_code == 200
    submit_body = submitted.get_json()
    assert submit_body["status"] == "pending_confirmation"
    action_id = submit_body["action_id"]

    confirmed = client.post(
        "/api/v1/actions/confirm",
        json={"action_id": action_id},
        headers=headers,
    )
    assert confirmed.status_code == 200
    confirm_body = confirmed.get_json()
    assert confirm_body["status"] == "approved"
    assert confirm_body["result"]["order_id"] == "e2e-order-1"

    pending = PendingActionsStore(db_path).get_pending_action(action_id)
    assert pending is not None
    assert pending.status is PendingActionStatus.APPROVED

    assert "/abs/pub/web/orders" in safeway_mock.requested_paths


def test_confirming_same_action_twice_returns_409(
    client: FlaskClient,
    signed_headers: Callable[..., dict[str, str]],
    seed_recipe: ParsedMeal,
    no_claude: None,
    safeway_mock: SafewayMockState,
) -> None:
    """A second confirm call for an already-resolved action is rejected."""
    headers = signed_headers()
    action_id = _stage_order(client, headers, seed_recipe.name)

    first = client.post(
        "/api/v1/actions/confirm",
        json={"action_id": action_id},
        headers=headers,
    )
    assert first.status_code == 200

    second = client.post(
        "/api/v1/actions/confirm",
        json={"action_id": action_id},
        headers=headers,
    )
    assert second.status_code == 409
