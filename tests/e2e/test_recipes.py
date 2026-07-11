"""E2E: recipe save/list/detail/delete and meals/parse recipe reuse.

``no_claude`` keeps the suite fully offline. ``/recipes/save`` never
touches Claude at all, and scenario 17 saves the recipe before parsing
it, so ``MealParser.find_recipe`` resolves it on the direct
``RecipeStore`` lookup -- before any Claude call would even be
attempted.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

    from flask.testing import FlaskClient

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
    client: FlaskClient,
    signed_headers: Callable[..., dict[str, str]],
    no_claude: None,
) -> None:
    """Saving a recipe makes it listable and fetchable; duplicates 409."""
    headers = signed_headers()
    body = _save_body()

    created = client.post("/api/v1/recipes/save", json=body, headers=headers)
    assert created.status_code == 201
    recipe_id = created.get_json()["id"]

    listed = client.get("/api/v1/recipes", headers=headers)
    names = [r["display_name"] for r in listed.get_json()["recipes"]]
    assert "e2e taco night" in names

    detail = client.get(f"/api/v1/recipes/{recipe_id}", headers=headers)
    assert detail.status_code == 200
    assert detail.get_json()["name"] == "e2e taco night"

    duplicate = client.post("/api/v1/recipes/save", json=body, headers=headers)
    assert duplicate.status_code == 409


def test_meals_parse_reuses_saved_recipe_without_claude(
    client: FlaskClient,
    signed_headers: Callable[..., dict[str, str]],
    no_claude: None,
) -> None:
    """Parsing a saved recipe's name returns its stored ingredients."""
    headers = signed_headers()
    client.post("/api/v1/recipes/save", json=_save_body(), headers=headers)

    response = client.post(
        "/api/v1/meals/parse",
        json={"text": "e2e taco night"},
        headers=headers,
    )
    assert response.status_code == 200
    meals = response.get_json()["meals"]
    assert len(meals) == 1
    meal = meals[0]
    assert meal["known_recipe"] is True
    ingredients = [i["ingredient"] for i in meal["purchase_items"]]
    assert "corn tortillas" in ingredients


def test_delete_recipe_then_detail_404(
    client: FlaskClient,
    signed_headers: Callable[..., dict[str, str]],
    no_claude: None,
) -> None:
    """Deleting a recipe removes it; its detail page then 404s."""
    headers = signed_headers()
    created = client.post(
        "/api/v1/recipes/save",
        json=_save_body("e2e one-off"),
        headers=headers,
    )
    recipe_id = created.get_json()["id"]

    deleted = client.delete(f"/api/v1/recipes/{recipe_id}", headers=headers)
    assert deleted.status_code == 204

    detail = client.get(f"/api/v1/recipes/{recipe_id}", headers=headers)
    assert detail.status_code == 404
