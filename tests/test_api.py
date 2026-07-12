"""Tests for the /api/v1 read endpoints (grocery_butler.api blueprint)."""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient

from grocery_butler.app import create_app
from grocery_butler.auth_middleware import SECRET_ENV_VAR, mint_token
from grocery_butler.models import (
    BrandMatchType,
    BrandPreference,
    BrandPreferenceType,
    Ingredient,
    IngredientCategory,
    InventoryItem,
    InventoryStatus,
    ParsedMeal,
)
from grocery_butler.pantry_manager import PantryManager
from grocery_butler.recipe_store import RecipeStore

TEST_SECRET = "test-shared-secret"
SECRET_ENV = {SECRET_ENV_VAR: TEST_SECRET}

READ_ENDPOINTS = [
    "/api/v1/inventory",
    "/api/v1/pantry",
    "/api/v1/recipes",
    "/api/v1/recipes/1",
    "/api/v1/brands",
    "/api/v1/preferences",
    "/api/v1/restock",
]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a temporary database path for test isolation."""
    return str(tmp_path / "test_api.db")


@pytest.fixture()
def app(db_path: str) -> Flask:
    """Create a Flask test app with a temporary database."""
    application = create_app(db_path=db_path)
    application.config["TESTING"] = True
    return application


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    """Return a Flask test client."""
    return app.test_client()


@pytest.fixture()
def pantry_mgr(db_path: str) -> PantryManager:
    """Return a PantryManager bound to the test database."""
    return PantryManager(db_path)


@pytest.fixture()
def recipe_store(db_path: str) -> RecipeStore:
    """Return a RecipeStore bound to the test database."""
    return RecipeStore(db_path)


@pytest.fixture()
def auth_headers() -> dict[str, str]:
    """Return a valid Authorization header minted with the test secret."""
    with patch.dict(os.environ, SECRET_ENV):
        token = mint_token("rubotpaul")
    return {"Authorization": f"Bearer {token}"}


@pytest.fixture()
def sample_item() -> InventoryItem:
    """Return a sample InventoryItem for seeding."""
    return InventoryItem(
        ingredient="milk",
        display_name="Milk",
        category=IngredientCategory.DAIRY,
        status=InventoryStatus.ON_HAND,
    )


@pytest.fixture()
def sample_meal() -> ParsedMeal:
    """Return a sample ParsedMeal for seeding."""
    return ParsedMeal(
        name="test pasta",
        servings=4,
        known_recipe=True,
        needs_confirmation=False,
        purchase_items=[
            Ingredient(
                ingredient="pasta",
                quantity=1.0,
                unit="lb",
                category=IngredientCategory.PANTRY_DRY,
            ),
        ],
        pantry_items=[
            Ingredient(
                ingredient="salt",
                quantity=1.0,
                unit="tsp",
                category=IngredientCategory.PANTRY_DRY,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# Auth: every /api/v1 read endpoint rejects unauthenticated requests
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestAuthRequired:
    """All read endpoints must reject requests without a valid bearer."""

    @pytest.mark.parametrize("path", READ_ENDPOINTS)
    def test_missing_bearer_returns_401(self, client: FlaskClient, path: str) -> None:
        """Requests without an Authorization header get 401."""
        response = client.get(path)
        assert response.status_code == 401

    @pytest.mark.parametrize("path", READ_ENDPOINTS)
    def test_garbage_bearer_returns_401(self, client: FlaskClient, path: str) -> None:
        """Requests with an invalid token get 401."""
        response = client.get(
            path, headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# Endpoint behavior with a valid bearer
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestInventoryEndpoint:
    """GET /api/v1/inventory."""

    def test_empty_inventory(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """An empty database yields an empty items list."""
        response = client.get("/api/v1/inventory", headers=auth_headers)
        assert response.status_code == 200
        assert response.get_json() == {"items": []}

    def test_returns_seeded_items(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        pantry_mgr: PantryManager,
        sample_item: InventoryItem,
    ) -> None:
        """Seeded inventory items come back with full field shape."""
        pantry_mgr.add_item(sample_item)
        response = client.get("/api/v1/inventory", headers=auth_headers)
        assert response.status_code == 200
        items = response.get_json()["items"]
        assert len(items) == 1
        assert items[0]["ingredient"] == "milk"
        assert items[0]["display_name"] == "Milk"
        assert items[0]["category"] == "dairy"
        assert items[0]["status"] == "on_hand"


@patch.dict(os.environ, SECRET_ENV)
class TestPantryEndpoint:
    """GET /api/v1/pantry."""

    def test_returns_staples(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        recipe_store: RecipeStore,
    ) -> None:
        """Seeded pantry staples come back with id/ingredient/category."""
        recipe_store.add_pantry_staple("dragonfruit powder", "pantry_dry")
        response = client.get("/api/v1/pantry", headers=auth_headers)
        assert response.status_code == 200
        staples = response.get_json()["staples"]
        added = [s for s in staples if s["ingredient"] == "dragonfruit powder"]
        assert len(added) == 1
        assert added[0]["category"] == "pantry_dry"
        assert added[0]["display_name"] == "Dragonfruit Powder"
        assert "id" in added[0]


@patch.dict(os.environ, SECRET_ENV)
class TestRecipeEndpoints:
    """GET /api/v1/recipes and /api/v1/recipes/<id>."""

    def test_list_recipes(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        recipe_store: RecipeStore,
        sample_meal: ParsedMeal,
    ) -> None:
        """Recipe list returns summary rows with ids."""
        recipe_id = recipe_store.save_recipe(sample_meal)
        response = client.get("/api/v1/recipes", headers=auth_headers)
        assert response.status_code == 200
        recipes = response.get_json()["recipes"]
        assert len(recipes) == 1
        assert recipes[0]["id"] == recipe_id
        assert recipes[0]["name"] == "test pasta"

    def test_get_recipe_full(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        recipe_store: RecipeStore,
        sample_meal: ParsedMeal,
    ) -> None:
        """Recipe detail returns the full parsed meal with ingredients."""
        recipe_id = recipe_store.save_recipe(sample_meal)
        response = client.get(f"/api/v1/recipes/{recipe_id}", headers=auth_headers)
        assert response.status_code == 200
        body = response.get_json()
        assert body["id"] == recipe_id
        assert body["name"] == "test pasta"
        assert body["servings"] == 4
        assert body["purchase_items"][0]["ingredient"] == "pasta"
        assert body["pantry_items"][0]["ingredient"] == "salt"

    def test_get_recipe_unknown_id_returns_404(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """An unknown recipe id yields a JSON 404."""
        response = client.get("/api/v1/recipes/999", headers=auth_headers)
        assert response.status_code == 404
        assert response.get_json() == {"error": "recipe not found"}


@patch.dict(os.environ, SECRET_ENV)
class TestBrandsEndpoint:
    """GET /api/v1/brands."""

    def test_returns_preferences(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        recipe_store: RecipeStore,
    ) -> None:
        """Seeded brand preferences come back with full field shape."""
        pref_id = recipe_store.add_brand_preference(
            BrandPreference(
                match_target="milk",
                match_type=BrandMatchType.INGREDIENT,
                brand="Clover",
                preference_type=BrandPreferenceType.PREFERRED,
                notes="organic",
            )
        )
        response = client.get("/api/v1/brands", headers=auth_headers)
        assert response.status_code == 200
        brands = response.get_json()["brands"]
        assert len(brands) == 1
        assert brands[0]["id"] == pref_id
        assert brands[0]["match_target"] == "milk"
        assert brands[0]["match_type"] == "ingredient"
        assert brands[0]["brand"] == "Clover"
        assert brands[0]["preference_type"] == "preferred"
        assert brands[0]["notes"] == "organic"


@patch.dict(os.environ, SECRET_ENV)
class TestPreferencesEndpoint:
    """GET /api/v1/preferences."""

    def test_returns_all_preferences(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        recipe_store: RecipeStore,
    ) -> None:
        """Stored preference keys come back as a flat JSON object."""
        recipe_store.set_preference("default_servings", "4")
        recipe_store.set_preference("default_units", "imperial")
        response = client.get("/api/v1/preferences", headers=auth_headers)
        assert response.status_code == 200
        body = response.get_json()
        assert body["default_servings"] == "4"
        assert body["default_units"] == "imperial"


@patch.dict(os.environ, SECRET_ENV)
class TestRestockEndpoint:
    """GET /api/v1/restock."""

    def test_returns_low_and_out_items(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        pantry_mgr: PantryManager,
        sample_item: InventoryItem,
    ) -> None:
        """Items marked out show up in the restock queue."""
        pantry_mgr.add_item(sample_item)
        pantry_mgr.update_status("milk", InventoryStatus.OUT)
        response = client.get("/api/v1/restock", headers=auth_headers)
        assert response.status_code == 200
        items = response.get_json()["items"]
        assert len(items) == 1
        assert items[0]["ingredient"] == "milk"
        assert items[0]["status"] == "out"

    def test_on_hand_items_excluded(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        pantry_mgr: PantryManager,
        sample_item: InventoryItem,
    ) -> None:
        """Items still on hand do not show up in the restock queue."""
        pantry_mgr.add_item(sample_item)
        response = client.get("/api/v1/restock", headers=auth_headers)
        assert response.status_code == 200
        assert response.get_json() == {"items": []}


# ---------------------------------------------------------------------------
# /healthz
# ---------------------------------------------------------------------------


class TestHealthz:
    """GET /healthz."""

    def test_healthz_ok_without_auth(self, client: FlaskClient) -> None:
        """/healthz responds 200 {"ok": true} with no bearer required."""
        response = client.get("/healthz")
        assert response.status_code == 200
        assert response.get_json() == {"ok": True}
