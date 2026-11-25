"""E2E: the happy-path chain from meal text to a retailer-neutral list.

Exercises every layer with real wiring -- ``MealParser`` + a seeded
recipe, ``Consolidator``, and the Flask ``/api/v1`` blueprint -- and the
pending-actions staging/confirmation flow. The chain ends at the
shopping list: since ADR 0001 there is no retailer cart or checkout, so
the retired ``/order/*`` endpoints must not exist.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

    from grocery_butler.models import ParsedMeal

pytestmark = pytest.mark.e2e


def test_full_chain_meal_to_shopping_list(
    signed_request: Callable[..., Any],
    seed_recipe: ParsedMeal,
    no_claude: None,
) -> None:
    """meals/parse -> shopping-list/preview yields the consolidated list."""
    parsed = signed_request("POST", "/api/v1/meals/parse", {"text": seed_recipe.name})
    assert parsed.status_code == 200
    meals = parsed.get_json()["meals"]
    assert len(meals) == 1
    assert meals[0]["known_recipe"] is True

    preview = signed_request(
        "POST",
        "/api/v1/shopping-list/preview",
        {"meals": meals, "include_restock": False},
    )
    assert preview.status_code == 200
    shopping_list = preview.get_json()["items"]
    assert {item["ingredient"] for item in shopping_list} == {
        "ground beef",
        "spaghetti",
        "tomato sauce",
    }


@pytest.mark.parametrize("path", ["/api/v1/order/preview", "/api/v1/order/submit"])
def test_retired_order_endpoints_are_gone(
    signed_request: Callable[..., Any],
    path: str,
) -> None:
    """The private Safeway order endpoints were removed (ADR 0001)."""
    response = signed_request("POST", path, {"shopping_list": []})
    assert response.status_code == 404


def test_confirming_same_action_twice_returns_409(
    signed_request: Callable[..., Any],
) -> None:
    """A second confirm call for an already-resolved action is rejected."""
    staged = signed_request(
        "POST",
        "/api/v1/preferences/set",
        {"preferences": {"default_servings": "2"}},
    )
    assert staged.status_code == 200
    action_id = staged.get_json()["action_id"]

    first = signed_request("POST", "/api/v1/actions/confirm", {"action_id": action_id})
    assert first.status_code == 200

    second = signed_request("POST", "/api/v1/actions/confirm", {"action_id": action_id})
    assert second.status_code == 409
