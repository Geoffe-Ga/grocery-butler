"""E2E: recipe save/list/detail/delete and meals/parse recipe reuse.

``no_claude`` keeps the suite fully offline. ``/recipes/save`` never
touches Claude at all, and scenario 17 saves the recipe before parsing
it, so ``MealParser.find_recipe`` resolves it on the direct
``RecipeStore`` lookup -- before any Claude call would even be
attempted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.e2e


def _save_body(name: str = "e2e taco night") -> dict[str, object]:
    """Return a valid ``/recipes/save`` request body.

    Args:
        name: Recipe name to save.

    Returns:
        A JSON-serializable request body dict.
    """
    return {
        "name": name,
        "servings": 4,
        "ingredients": [
            {
                "ingredient": "corn tortillas",
                "quantity": 8,
                "unit": "each",
                "category": "bakery",
                "is_pantry_item": False,
            },
            {
                "ingredient": "salt",
                "quantity": 1,
                "unit": "tsp",
                "category": "pantry_dry",
                "is_pantry_item": True,
            },
        ],
    }


def test_save_list_detail_then_duplicate_conflicts(
    signed_request: Callable[..., Any],
    no_claude: None,
) -> None:
    """Saving a recipe makes it listable and fetchable; duplicates 409."""
    body = _save_body()

    created = signed_request("POST", "/api/v1/recipes/save", body)
    assert created.status_code == 201
    recipe_id = created.get_json()["id"]

    listed = signed_request("GET", "/api/v1/recipes")
    names = [r["display_name"] for r in listed.get_json()["recipes"]]
    assert "e2e taco night" in names

    detail = signed_request("GET", f"/api/v1/recipes/{recipe_id}")
    assert detail.status_code == 200
    assert detail.get_json()["name"] == "e2e taco night"

    duplicate = signed_request("POST", "/api/v1/recipes/save", body)
    assert duplicate.status_code == 409


def test_meals_parse_reuses_saved_recipe_without_claude(
    signed_request: Callable[..., Any],
    no_claude: None,
) -> None:
    """Parsing a saved recipe's name returns its stored ingredients."""
    signed_request("POST", "/api/v1/recipes/save", _save_body())

    response = signed_request("POST", "/api/v1/meals/parse", {"text": "e2e taco night"})
    assert response.status_code == 200
    meals = response.get_json()["meals"]
    assert len(meals) == 1
    meal = meals[0]
    assert meal["known_recipe"] is True
    ingredients = [i["ingredient"] for i in meal["purchase_items"]]
    assert "corn tortillas" in ingredients


def test_delete_recipe_then_detail_404(
    signed_request: Callable[..., Any],
    no_claude: None,
) -> None:
    """Deleting a recipe removes it; its detail page then 404s."""
    created = signed_request("POST", "/api/v1/recipes/save", _save_body("e2e one-off"))
    recipe_id = created.get_json()["id"]

    deleted = signed_request("DELETE", f"/api/v1/recipes/{recipe_id}")
    assert deleted.status_code == 204

    detail = signed_request("GET", f"/api/v1/recipes/{recipe_id}")
    assert detail.status_code == 404
