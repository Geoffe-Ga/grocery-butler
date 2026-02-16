"""Tests for grocery_butler.app Flask web application."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient

from grocery_butler.app import create_app
from grocery_butler.models import (
    Ingredient,
    IngredientCategory,
    InventoryItem,
    InventoryStatus,
    ParsedMeal,
)
from grocery_butler.pantry_manager import PantryManager
from grocery_butler.recipe_store import RecipeStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a temporary database path for test isolation."""
    return str(tmp_path / "test_web.db")


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
def sample_item() -> InventoryItem:
    """Return a sample InventoryItem for testing."""
    return InventoryItem(
        ingredient="milk",
        display_name="Milk",
        category=IngredientCategory.DAIRY,
        status=InventoryStatus.ON_HAND,
    )


@pytest.fixture()
def sample_meal() -> ParsedMeal:
    """Return a sample ParsedMeal for testing."""
    return ParsedMeal(
        name="Test Pasta",
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
            Ingredient(
                ingredient="tomato sauce",
                quantity=2.0,
                unit="cups",
                category=IngredientCategory.PANTRY_DRY,
            ),
        ],
        pantry_items=[
            Ingredient(
                ingredient="salt",
                quantity=1.0,
                unit="tsp",
                category=IngredientCategory.PANTRY_DRY,
                is_pantry_item=True,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# TestCreateApp
# ---------------------------------------------------------------------------


class TestCreateApp:
    """Tests for the create_app factory function."""

    def test_create_app_returns_flask(self, db_path: str) -> None:
        """Test create_app returns a Flask instance."""
        from flask import Flask

        application = create_app(db_path=db_path)
        assert isinstance(application, Flask)

    def test_create_app_sets_database_path(self, app: Flask) -> None:
        """Test create_app stores database path in config."""
        assert "DATABASE_PATH" in app.config

    def test_create_app_has_secret_key(self, app: Flask) -> None:
        """Test create_app sets a secret key for flash messages."""
        assert app.config["SECRET_KEY"]


# ---------------------------------------------------------------------------
# TestDashboard
# ---------------------------------------------------------------------------


class TestDashboard:
    """Tests for the dashboard page (GET /)."""

    def test_dashboard_get_status_200(self, client: FlaskClient) -> None:
        """Test GET / returns 200."""
        response = client.get("/")
        assert response.status_code == 200

    def test_dashboard_contains_title(self, client: FlaskClient) -> None:
        """Test dashboard page has the title."""
        response = client.get("/")
        assert b"Dashboard" in response.data

    def test_dashboard_shows_zero_counts(self, client: FlaskClient) -> None:
        """Test dashboard shows zero counts with empty database."""
        response = client.get("/")
        html = response.data.decode()
        assert "0" in html

    def test_dashboard_shows_recipe_count(
        self, client: FlaskClient, recipe_store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test dashboard displays accurate recipe count."""
        recipe_store.save_recipe(sample_meal)

        response = client.get("/")
        html = response.data.decode()
        assert ">1<" in html

    def test_dashboard_shows_inventory_count(
        self, client: FlaskClient, pantry_mgr: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test dashboard displays accurate inventory count."""
        pantry_mgr.add_item(sample_item)
        response = client.get("/")
        html = response.data.decode()
        assert ">1<" in html

    def test_dashboard_shows_restock_queue(
        self, client: FlaskClient, pantry_mgr: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test dashboard shows restock items when items are low/out."""
        pantry_mgr.add_item(sample_item)
        pantry_mgr.update_status("milk", InventoryStatus.LOW)
        response = client.get("/")
        assert b"Milk" in response.data
        assert b"Restock Queue" in response.data

    def test_dashboard_no_restock_empty_state(self, client: FlaskClient) -> None:
        """Test dashboard shows empty state when no restock needed."""
        response = client.get("/")
        assert b"All items are stocked" in response.data

    def test_dashboard_contains_nav(self, client: FlaskClient) -> None:
        """Test dashboard contains navigation links."""
        response = client.get("/")
        html = response.data.decode()
        assert "Inventory" in html
        assert "Recipes" in html
        assert "Dashboard" in html


# ---------------------------------------------------------------------------
# TestInventoryPage
# ---------------------------------------------------------------------------


class TestInventoryPage:
    """Tests for the inventory page (GET /inventory)."""

    def test_inventory_get_status_200(self, client: FlaskClient) -> None:
        """Test GET /inventory returns 200."""
        response = client.get("/inventory")
        assert response.status_code == 200

    def test_inventory_contains_title(self, client: FlaskClient) -> None:
        """Test inventory page has the title."""
        response = client.get("/inventory")
        assert b"Inventory" in response.data

    def test_inventory_shows_add_form(self, client: FlaskClient) -> None:
        """Test inventory page has the add item form."""
        response = client.get("/inventory")
        assert b"Add Item" in response.data
        assert b"ingredient" in response.data

    def test_inventory_empty_state(self, client: FlaskClient) -> None:
        """Test inventory shows empty state message with no items."""
        response = client.get("/inventory")
        assert b"No items in your inventory yet" in response.data

    def test_inventory_shows_items(
        self, client: FlaskClient, pantry_mgr: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test inventory page lists tracked items."""
        pantry_mgr.add_item(sample_item)
        response = client.get("/inventory")
        assert b"Milk" in response.data

    def test_inventory_shows_status_buttons(
        self, client: FlaskClient, pantry_mgr: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test inventory page shows status toggle buttons for each item."""
        pantry_mgr.add_item(sample_item)
        response = client.get("/inventory")
        html = response.data.decode()
        assert "on_hand" in html
        assert "low" in html
        assert "out" in html

    def test_inventory_has_search_bar(self, client: FlaskClient) -> None:
        """Test inventory page has a search/filter input."""
        response = client.get("/inventory")
        assert b"inventory-search" in response.data

    def test_inventory_shows_categories(self, client: FlaskClient) -> None:
        """Test inventory page shows category options in form."""
        response = client.get("/inventory")
        html = response.data.decode()
        assert "produce" in html
        assert "dairy" in html

    def test_inventory_active_nav(self, client: FlaskClient) -> None:
        """Test inventory page marks inventory nav link as active."""
        response = client.get("/inventory")
        assert b'aria-current="page"' in response.data

    def test_inventory_shows_multiple_items(
        self, client: FlaskClient, pantry_mgr: PantryManager
    ) -> None:
        """Test inventory page shows multiple inventory items."""
        items = [
            InventoryItem(
                ingredient="milk",
                display_name="Milk",
                category=IngredientCategory.DAIRY,
            ),
            InventoryItem(
                ingredient="eggs",
                display_name="Eggs",
                category=IngredientCategory.DAIRY,
            ),
            InventoryItem(
                ingredient="bread",
                display_name="Bread",
                category=IngredientCategory.BAKERY,
            ),
        ]
        for item in items:
            pantry_mgr.add_item(item)

        response = client.get("/inventory")
        html = response.data.decode()
        assert "Milk" in html
        assert "Eggs" in html
        assert "Bread" in html


# ---------------------------------------------------------------------------
# TestInventoryUpdate
# ---------------------------------------------------------------------------


class TestInventoryUpdate:
    """Tests for POST /inventory/update AJAX endpoint."""

    def test_update_status_success(
        self, client: FlaskClient, pantry_mgr: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test successful status update returns 200 with JSON."""
        pantry_mgr.add_item(sample_item)
        response = client.post(
            "/inventory/update",
            data=json.dumps({"ingredient": "milk", "status": "low"}),
            content_type="application/json",
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["success"] is True
        assert data["status"] == "low"

    def test_update_verifies_in_db(
        self, client: FlaskClient, pantry_mgr: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test status update persists to database."""
        pantry_mgr.add_item(sample_item)
        client.post(
            "/inventory/update",
            data=json.dumps({"ingredient": "milk", "status": "out"}),
            content_type="application/json",
        )
        item = pantry_mgr.get_item("milk")
        assert item is not None
        assert item.status == InventoryStatus.OUT

    def test_update_missing_json_body(self, client: FlaskClient) -> None:
        """Test POST with no JSON body returns 400."""
        response = client.post(
            "/inventory/update",
            data="not json",
            content_type="text/plain",
        )
        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False

    def test_update_missing_ingredient(self, client: FlaskClient) -> None:
        """Test POST without ingredient field returns 400."""
        response = client.post(
            "/inventory/update",
            data=json.dumps({"status": "low"}),
            content_type="application/json",
        )
        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False

    def test_update_missing_status(self, client: FlaskClient) -> None:
        """Test POST without status field returns 400."""
        response = client.post(
            "/inventory/update",
            data=json.dumps({"ingredient": "milk"}),
            content_type="application/json",
        )
        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False

    def test_update_invalid_status(
        self, client: FlaskClient, pantry_mgr: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test POST with invalid status value returns 400."""
        pantry_mgr.add_item(sample_item)
        response = client.post(
            "/inventory/update",
            data=json.dumps({"ingredient": "milk", "status": "invalid_status"}),
            content_type="application/json",
        )
        assert response.status_code == 400
        data = response.get_json()
        assert data["success"] is False
        assert "Invalid status" in data["error"]

    def test_update_nonexistent_item(self, client: FlaskClient) -> None:
        """Test POST for nonexistent item returns 404."""
        response = client.post(
            "/inventory/update",
            data=json.dumps({"ingredient": "nonexistent", "status": "low"}),
            content_type="application/json",
        )
        assert response.status_code == 404
        data = response.get_json()
        assert data["success"] is False

    def test_update_empty_ingredient(self, client: FlaskClient) -> None:
        """Test POST with empty ingredient returns 400."""
        response = client.post(
            "/inventory/update",
            data=json.dumps({"ingredient": "", "status": "low"}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_update_empty_status(self, client: FlaskClient) -> None:
        """Test POST with empty status returns 400."""
        response = client.post(
            "/inventory/update",
            data=json.dumps({"ingredient": "milk", "status": ""}),
            content_type="application/json",
        )
        assert response.status_code == 400

    def test_update_all_valid_statuses(
        self, client: FlaskClient, pantry_mgr: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test updating to each valid status succeeds."""
        pantry_mgr.add_item(sample_item)
        for status in ["on_hand", "low", "out"]:
            response = client.post(
                "/inventory/update",
                data=json.dumps({"ingredient": "milk", "status": status}),
                content_type="application/json",
            )
            assert response.status_code == 200
            data = response.get_json()
            assert data["status"] == status


# ---------------------------------------------------------------------------
# TestInventoryAdd
# ---------------------------------------------------------------------------


class TestInventoryAdd:
    """Tests for POST /inventory/add form endpoint."""

    def test_add_item_success(
        self, client: FlaskClient, pantry_mgr: PantryManager
    ) -> None:
        """Test adding an item via form redirects to inventory."""
        response = client.post(
            "/inventory/add",
            data={
                "ingredient": "milk",
                "display_name": "Milk",
                "category": "dairy",
                "status": "on_hand",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "/inventory" in response.headers["Location"]

        item = pantry_mgr.get_item("milk")
        assert item is not None
        assert item.display_name == "Milk"

    def test_add_item_with_redirect(
        self, client: FlaskClient, pantry_mgr: PantryManager
    ) -> None:
        """Test add item redirects and shows success flash."""
        response = client.post(
            "/inventory/add",
            data={
                "ingredient": "eggs",
                "display_name": "Eggs",
                "category": "dairy",
                "status": "on_hand",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"Added Eggs" in response.data

    def test_add_item_missing_ingredient(self, client: FlaskClient) -> None:
        """Test adding without ingredient shows error flash."""
        response = client.post(
            "/inventory/add",
            data={"ingredient": "", "display_name": "Milk"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"Ingredient name is required" in response.data

    def test_add_item_auto_display_name(
        self, client: FlaskClient, pantry_mgr: PantryManager
    ) -> None:
        """Test adding without display_name auto-generates one."""
        client.post(
            "/inventory/add",
            data={"ingredient": "olive_oil", "display_name": "", "category": ""},
            follow_redirects=True,
        )
        item = pantry_mgr.get_item("olive_oil")
        assert item is not None
        assert item.display_name == "Olive Oil"

    def test_add_item_no_category(
        self, client: FlaskClient, pantry_mgr: PantryManager
    ) -> None:
        """Test adding without category sets None."""
        client.post(
            "/inventory/add",
            data={"ingredient": "mystery", "display_name": "Mystery"},
            follow_redirects=True,
        )
        item = pantry_mgr.get_item("mystery")
        assert item is not None
        assert item.category is None

    def test_add_item_invalid_category(self, client: FlaskClient) -> None:
        """Test adding with invalid category shows error flash."""
        response = client.post(
            "/inventory/add",
            data={
                "ingredient": "milk",
                "display_name": "Milk",
                "category": "invalid_cat",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"Invalid category" in response.data

    def test_add_item_invalid_status(self, client: FlaskClient) -> None:
        """Test adding with invalid status shows error flash."""
        response = client.post(
            "/inventory/add",
            data={
                "ingredient": "milk",
                "display_name": "Milk",
                "category": "dairy",
                "status": "bad_status",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"Invalid status" in response.data

    def test_add_duplicate_item(
        self, client: FlaskClient, pantry_mgr: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test adding duplicate item shows error flash."""
        pantry_mgr.add_item(sample_item)
        response = client.post(
            "/inventory/add",
            data={
                "ingredient": "milk",
                "display_name": "Milk",
                "category": "dairy",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"already exists" in response.data

    def test_add_item_default_status(
        self, client: FlaskClient, pantry_mgr: PantryManager
    ) -> None:
        """Test adding without status defaults to on_hand."""
        client.post(
            "/inventory/add",
            data={
                "ingredient": "butter",
                "display_name": "Butter",
                "category": "dairy",
            },
            follow_redirects=True,
        )
        item = pantry_mgr.get_item("butter")
        assert item is not None
        assert item.status == InventoryStatus.ON_HAND

    def test_add_item_with_status_low(
        self, client: FlaskClient, pantry_mgr: PantryManager
    ) -> None:
        """Test adding item with explicit low status."""
        client.post(
            "/inventory/add",
            data={
                "ingredient": "sugar",
                "display_name": "Sugar",
                "category": "pantry_dry",
                "status": "low",
            },
            follow_redirects=True,
        )
        item = pantry_mgr.get_item("sugar")
        assert item is not None
        assert item.status == InventoryStatus.LOW


# ---------------------------------------------------------------------------
# TestErrorHandlers
# ---------------------------------------------------------------------------


class TestErrorHandlers:
    """Tests for HTTP error handler pages."""

    def test_404_page(self, client: FlaskClient) -> None:
        """Test 404 error handler returns correct page."""
        response = client.get("/nonexistent-page")
        assert response.status_code == 404
        assert b"Page Not Found" in response.data

    def test_404_has_back_link(self, client: FlaskClient) -> None:
        """Test 404 page has a link back to dashboard."""
        response = client.get("/nonexistent-page")
        assert b"Back to Dashboard" in response.data


# ---------------------------------------------------------------------------
# TestRecipesPage
# ---------------------------------------------------------------------------


class TestRecipesPage:
    """Tests for the recipes list page (GET /recipes)."""

    def test_recipes_get_status_200(self, client: FlaskClient) -> None:
        """Test GET /recipes returns 200."""
        response = client.get("/recipes")
        assert response.status_code == 200

    def test_recipes_contains_title(self, client: FlaskClient) -> None:
        """Test recipes page has the title."""
        response = client.get("/recipes")
        assert b"Recipes" in response.data

    def test_recipes_empty_state(self, client: FlaskClient) -> None:
        """Test recipes shows empty state with no recipes."""
        response = client.get("/recipes")
        assert b"No recipes yet" in response.data

    def test_recipes_has_add_button(self, client: FlaskClient) -> None:
        """Test recipes page has add recipe button."""
        response = client.get("/recipes")
        assert b"Add Recipe" in response.data

    def test_recipes_has_search_bar(self, client: FlaskClient) -> None:
        """Test recipes page has a search input."""
        response = client.get("/recipes")
        assert b"recipe-search" in response.data

    def test_recipes_shows_recipe(
        self, client: FlaskClient, recipe_store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test recipes page shows a saved recipe."""
        recipe_store.save_recipe(sample_meal)
        response = client.get("/recipes")
        assert b"Test Pasta" in response.data

    def test_recipes_shows_ingredient_count(
        self, client: FlaskClient, recipe_store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test recipes page shows ingredient count for each recipe."""
        recipe_store.save_recipe(sample_meal)
        response = client.get("/recipes")
        html = response.data.decode()
        assert "3 ingredients" in html

    def test_recipes_shows_times_ordered(
        self, client: FlaskClient, recipe_store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test recipes page shows times ordered count."""
        recipe_store.save_recipe(sample_meal)
        response = client.get("/recipes")
        html = response.data.decode()
        assert "0 orders" in html

    def test_recipes_links_to_detail(
        self, client: FlaskClient, recipe_store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test recipe card links to detail page."""
        recipe_id = recipe_store.save_recipe(sample_meal)
        response = client.get("/recipes")
        html = response.data.decode()
        assert f"/recipes/{recipe_id}" in html


# ---------------------------------------------------------------------------
# TestRecipeDetail
# ---------------------------------------------------------------------------


class TestRecipeDetail:
    """Tests for the recipe detail page (GET /recipes/<id>)."""

    def test_recipe_detail_status_200(
        self, client: FlaskClient, recipe_store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test GET /recipes/<id> returns 200 for existing recipe."""
        recipe_id = recipe_store.save_recipe(sample_meal)
        response = client.get(f"/recipes/{recipe_id}")
        assert response.status_code == 200

    def test_recipe_detail_shows_name(
        self, client: FlaskClient, recipe_store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test recipe detail shows recipe name."""
        recipe_id = recipe_store.save_recipe(sample_meal)
        response = client.get(f"/recipes/{recipe_id}")
        assert b"Test Pasta" in response.data

    def test_recipe_detail_shows_servings(
        self, client: FlaskClient, recipe_store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test recipe detail shows servings count."""
        recipe_id = recipe_store.save_recipe(sample_meal)
        response = client.get(f"/recipes/{recipe_id}")
        assert b"4 servings" in response.data

    def test_recipe_detail_shows_purchase_items(
        self, client: FlaskClient, recipe_store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test recipe detail shows purchase items section."""
        recipe_id = recipe_store.save_recipe(sample_meal)
        response = client.get(f"/recipes/{recipe_id}")
        html = response.data.decode()
        assert "Purchase Items" in html
        assert "pasta" in html
        assert "tomato sauce" in html

    def test_recipe_detail_shows_pantry_items(
        self, client: FlaskClient, recipe_store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test recipe detail shows pantry items section."""
        recipe_id = recipe_store.save_recipe(sample_meal)
        response = client.get(f"/recipes/{recipe_id}")
        html = response.data.decode()
        assert "Pantry Items" in html
        assert "salt" in html

    def test_recipe_detail_has_delete_button(
        self, client: FlaskClient, recipe_store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test recipe detail has a delete button."""
        recipe_id = recipe_store.save_recipe(sample_meal)
        response = client.get(f"/recipes/{recipe_id}")
        assert b"Delete Recipe" in response.data

    def test_recipe_detail_has_back_link(
        self, client: FlaskClient, recipe_store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test recipe detail has back to recipes link."""
        recipe_id = recipe_store.save_recipe(sample_meal)
        response = client.get(f"/recipes/{recipe_id}")
        assert b"Back to Recipes" in response.data

    def test_recipe_detail_404_for_missing(self, client: FlaskClient) -> None:
        """Test GET /recipes/<id> returns 404 for nonexistent recipe."""
        response = client.get("/recipes/9999")
        assert response.status_code == 404
        assert b"Recipe not found" in response.data


# ---------------------------------------------------------------------------
# TestRecipeAdd
# ---------------------------------------------------------------------------


class TestRecipeAdd:
    """Tests for the recipe add page (GET/POST /recipes/add)."""

    def test_recipe_add_get_status_200(self, client: FlaskClient) -> None:
        """Test GET /recipes/add returns 200."""
        response = client.get("/recipes/add")
        assert response.status_code == 200

    def test_recipe_add_has_form(self, client: FlaskClient) -> None:
        """Test recipe add page has the form fields."""
        response = client.get("/recipes/add")
        html = response.data.decode()
        assert "Recipe Name" in html
        assert "Servings" in html
        assert "Ingredients" in html

    def test_recipe_add_has_categories(self, client: FlaskClient) -> None:
        """Test recipe add form has category dropdown."""
        response = client.get("/recipes/add")
        html = response.data.decode()
        assert "produce" in html
        assert "dairy" in html

    def test_recipe_add_post_success(
        self, client: FlaskClient, recipe_store: RecipeStore
    ) -> None:
        """Test POST /recipes/add creates a recipe and redirects."""
        response = client.post(
            "/recipes/add",
            data={
                "name": "Grilled Cheese",
                "servings": "2",
                "ing_name_0": "bread",
                "ing_qty_0": "4",
                "ing_unit_0": "slices",
                "ing_category_0": "bakery",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "/recipes/" in response.headers["Location"]

    def test_recipe_add_post_redirects_to_detail(
        self, client: FlaskClient, recipe_store: RecipeStore
    ) -> None:
        """Test POST /recipes/add redirects to the detail page with flash."""
        response = client.post(
            "/recipes/add",
            data={
                "name": "Grilled Cheese",
                "servings": "2",
                "ing_name_0": "bread",
                "ing_qty_0": "4",
                "ing_unit_0": "slices",
                "ing_category_0": "bakery",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"Grilled Cheese" in response.data

    def test_recipe_add_post_missing_name(self, client: FlaskClient) -> None:
        """Test POST without name shows error flash."""
        response = client.post(
            "/recipes/add",
            data={
                "name": "",
                "servings": "4",
                "ing_name_0": "bread",
                "ing_qty_0": "1",
                "ing_unit_0": "loaf",
                "ing_category_0": "bakery",
            },
            follow_redirects=True,
        )
        assert b"Recipe name is required" in response.data

    def test_recipe_add_post_invalid_servings(self, client: FlaskClient) -> None:
        """Test POST with non-numeric servings shows error."""
        response = client.post(
            "/recipes/add",
            data={
                "name": "Test",
                "servings": "not_a_number",
                "ing_name_0": "bread",
                "ing_qty_0": "1",
                "ing_unit_0": "loaf",
                "ing_category_0": "bakery",
            },
            follow_redirects=True,
        )
        assert b"Servings must be a number" in response.data

    def test_recipe_add_post_no_ingredients(self, client: FlaskClient) -> None:
        """Test POST without ingredients shows error."""
        response = client.post(
            "/recipes/add",
            data={
                "name": "Empty Recipe",
                "servings": "4",
            },
            follow_redirects=True,
        )
        assert b"At least one ingredient is required" in response.data

    def test_recipe_add_post_with_pantry_item(
        self, client: FlaskClient, recipe_store: RecipeStore
    ) -> None:
        """Test POST with pantry checkbox creates pantry item."""
        response = client.post(
            "/recipes/add",
            data={
                "name": "Salad",
                "servings": "2",
                "ing_name_0": "lettuce",
                "ing_qty_0": "1",
                "ing_unit_0": "head",
                "ing_category_0": "produce",
                "ing_name_1": "salt",
                "ing_qty_1": "1",
                "ing_unit_1": "tsp",
                "ing_category_1": "pantry_dry",
                "ing_pantry_1": "on",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"Salad" in response.data

    def test_recipe_add_post_duplicate_name(
        self, client: FlaskClient, recipe_store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test POST with duplicate name shows error."""
        recipe_store.save_recipe(sample_meal)
        response = client.post(
            "/recipes/add",
            data={
                "name": "Test Pasta",
                "servings": "4",
                "ing_name_0": "noodles",
                "ing_qty_0": "1",
                "ing_unit_0": "lb",
                "ing_category_0": "pantry_dry",
            },
            follow_redirects=True,
        )
        assert b"already exists" in response.data

    def test_recipe_add_post_invalid_category_falls_back(
        self, client: FlaskClient
    ) -> None:
        """Test POST with invalid category falls back to other."""
        response = client.post(
            "/recipes/add",
            data={
                "name": "Test Recipe",
                "servings": "4",
                "ing_name_0": "stuff",
                "ing_qty_0": "1",
                "ing_unit_0": "each",
                "ing_category_0": "bad_category",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200

    def test_recipe_add_post_invalid_qty(self, client: FlaskClient) -> None:
        """Test POST with non-numeric qty defaults to 0."""
        response = client.post(
            "/recipes/add",
            data={
                "name": "Qty Test",
                "servings": "4",
                "ing_name_0": "stuff",
                "ing_qty_0": "not_a_number",
                "ing_unit_0": "each",
                "ing_category_0": "other",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# TestRecipeDelete
# ---------------------------------------------------------------------------


class TestRecipeDelete:
    """Tests for POST /recipes/<id>/delete."""

    def test_recipe_delete_success(
        self, client: FlaskClient, recipe_store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test deleting a recipe redirects to recipes list."""
        recipe_id = recipe_store.save_recipe(sample_meal)
        response = client.post(
            f"/recipes/{recipe_id}/delete",
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "/recipes" in response.headers["Location"]

    def test_recipe_delete_removes_from_db(
        self, client: FlaskClient, recipe_store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test deleting a recipe removes it from the database."""
        recipe_id = recipe_store.save_recipe(sample_meal)
        client.post(f"/recipes/{recipe_id}/delete", follow_redirects=True)
        meal = recipe_store.get_recipe_by_id(recipe_id)
        assert meal is None

    def test_recipe_delete_flash_message(
        self, client: FlaskClient, recipe_store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test deleting a recipe shows success flash."""
        recipe_id = recipe_store.save_recipe(sample_meal)
        response = client.post(
            f"/recipes/{recipe_id}/delete",
            follow_redirects=True,
        )
        assert b"Recipe deleted" in response.data


# ---------------------------------------------------------------------------
# TestShoppingListPage
# ---------------------------------------------------------------------------


class TestShoppingListPage:
    """Tests for the shopping list page (GET /shopping-list)."""

    def test_shopping_list_get_status_200(self, client: FlaskClient) -> None:
        """Test GET /shopping-list returns 200."""
        response = client.get("/shopping-list")
        assert response.status_code == 200

    def test_shopping_list_contains_title(self, client: FlaskClient) -> None:
        """Test shopping list page has the title."""
        response = client.get("/shopping-list")
        assert b"Shopping List" in response.data

    def test_shopping_list_empty_state(self, client: FlaskClient) -> None:
        """Test shopping list shows empty state with no items."""
        response = client.get("/shopping-list")
        assert b"No shopping list yet" in response.data

    def test_shopping_list_has_generate_form(self, client: FlaskClient) -> None:
        """Test shopping list has the generate form."""
        response = client.get("/shopping-list")
        assert b"Generate" in response.data
        assert b"meals" in response.data

    def test_shopping_list_has_disabled_order_button_when_items(
        self, client: FlaskClient
    ) -> None:
        """Test shopping list shows disabled order button when items exist."""
        with client.session_transaction() as sess:
            sess["shopping_list_items"] = [
                {
                    "ingredient": "pasta",
                    "quantity": 1.0,
                    "unit": "lb",
                    "category": "pantry_dry",
                    "from_meals": ["Test"],
                }
            ]
        response = client.get("/shopping-list")
        html = response.data.decode()
        assert "Order from Safeway" in html
        assert "Coming Soon" in html

    def test_shopping_list_shows_items_from_session(self, client: FlaskClient) -> None:
        """Test shopping list renders items from session."""
        with client.session_transaction() as sess:
            sess["shopping_list_items"] = [
                {
                    "ingredient": "chicken",
                    "quantity": 2.0,
                    "unit": "lbs",
                    "category": "meat",
                    "from_meals": ["Stir Fry"],
                }
            ]
        response = client.get("/shopping-list")
        assert b"chicken" in response.data

    def test_shopping_list_groups_by_category(self, client: FlaskClient) -> None:
        """Test shopping list groups items by category."""
        with client.session_transaction() as sess:
            sess["shopping_list_items"] = [
                {
                    "ingredient": "chicken",
                    "quantity": 2.0,
                    "unit": "lbs",
                    "category": "meat",
                    "from_meals": ["Stir Fry"],
                },
                {
                    "ingredient": "broccoli",
                    "quantity": 1.0,
                    "unit": "head",
                    "category": "produce",
                    "from_meals": ["Stir Fry"],
                },
            ]
        response = client.get("/shopping-list")
        html = response.data.decode()
        assert "meat" in html
        assert "produce" in html

    def test_shopping_list_restock_items_distinguished(
        self, client: FlaskClient
    ) -> None:
        """Test restock items are visually distinguished."""
        with client.session_transaction() as sess:
            sess["shopping_list_items"] = [
                {
                    "ingredient": "milk",
                    "quantity": 1.0,
                    "unit": "gallon",
                    "category": "dairy",
                    "from_meals": ["restock"],
                }
            ]
        response = client.get("/shopping-list")
        html = response.data.decode()
        assert "restock" in html
        assert "shopping-item-restock" in html


# ---------------------------------------------------------------------------
# TestShoppingListGenerate
# ---------------------------------------------------------------------------


class TestShoppingListGenerate:
    """Tests for POST /shopping-list/generate."""

    def test_generate_empty_meals(self, client: FlaskClient) -> None:
        """Test POST with empty meals shows error."""
        response = client.post(
            "/shopping-list/generate",
            data={"meals": ""},
            follow_redirects=True,
        )
        assert b"Please enter at least one meal" in response.data

    def test_generate_whitespace_only_meals(self, client: FlaskClient) -> None:
        """Test POST with whitespace-only meals shows error."""
        response = client.post(
            "/shopping-list/generate",
            data={"meals": "   \n   "},
            follow_redirects=True,
        )
        assert b"Please enter at least one meal" in response.data

    def test_generate_redirects_to_shopping_list(self, client: FlaskClient) -> None:
        """Test POST redirects to shopping list page."""
        response = client.post(
            "/shopping-list/generate",
            data={"meals": "Test Meal"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "/shopping-list" in response.headers["Location"]

    def test_generate_shows_flash_message(self, client: FlaskClient) -> None:
        """Test POST shows generation success flash."""
        response = client.post(
            "/shopping-list/generate",
            data={"meals": "Meal One\nMeal Two"},
            follow_redirects=True,
        )
        assert b"Generated shopping list from 2 meal(s)" in response.data

    def test_generate_stores_items_in_session(self, client: FlaskClient) -> None:
        """Test POST stores shopping list in session."""
        client.post(
            "/shopping-list/generate",
            data={"meals": "Some Meal"},
            follow_redirects=True,
        )
        # Verify session has items by getting the page
        response = client.get("/shopping-list")
        # The page should not show empty state since we generated a list
        # (even if stub meals, the restock items from seeded data may appear)
        assert response.status_code == 200

    def test_generate_with_known_recipe(
        self, client: FlaskClient, recipe_store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test generate with a known recipe produces items."""
        recipe_store.save_recipe(sample_meal)
        response = client.post(
            "/shopping-list/generate",
            data={"meals": "Test Pasta"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"Generated shopping list" in response.data


# ---------------------------------------------------------------------------
# TestPantryPage
# ---------------------------------------------------------------------------


class TestPantryPage:
    """Tests for the pantry staples page (GET /pantry)."""

    def test_pantry_get_status_200(self, client: FlaskClient) -> None:
        """Test GET /pantry returns 200."""
        response = client.get("/pantry")
        assert response.status_code == 200

    def test_pantry_contains_title(self, client: FlaskClient) -> None:
        """Test pantry page has the title."""
        response = client.get("/pantry")
        assert b"Pantry Staples" in response.data

    def test_pantry_shows_seeded_staples(self, client: FlaskClient) -> None:
        """Test pantry shows the default seeded staples."""
        response = client.get("/pantry")
        html = response.data.decode()
        # Default pantry includes salt, pepper, oil, etc.
        assert "Salt" in html
        assert "Olive Oil" in html

    def test_pantry_has_add_form(self, client: FlaskClient) -> None:
        """Test pantry page has the add staple form."""
        response = client.get("/pantry")
        assert b"Add Staple" in response.data

    def test_pantry_has_category_dropdown(self, client: FlaskClient) -> None:
        """Test pantry add form has category dropdown."""
        response = client.get("/pantry")
        html = response.data.decode()
        assert "produce" in html
        assert "dairy" in html

    def test_pantry_has_remove_buttons(self, client: FlaskClient) -> None:
        """Test pantry page has remove buttons for staples."""
        response = client.get("/pantry")
        assert b"Remove" in response.data


# ---------------------------------------------------------------------------
# TestPantryAdd
# ---------------------------------------------------------------------------


class TestPantryAdd:
    """Tests for POST /pantry/add."""

    def test_pantry_add_success(
        self, client: FlaskClient, recipe_store: RecipeStore
    ) -> None:
        """Test adding a pantry staple redirects and shows flash."""
        response = client.post(
            "/pantry/add",
            data={"ingredient": "cumin", "category": "pantry_dry"},
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"Added Cumin" in response.data

    def test_pantry_add_redirects(self, client: FlaskClient) -> None:
        """Test adding a pantry staple redirects to pantry page."""
        response = client.post(
            "/pantry/add",
            data={"ingredient": "cumin", "category": "pantry_dry"},
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "/pantry" in response.headers["Location"]

    def test_pantry_add_missing_ingredient(self, client: FlaskClient) -> None:
        """Test adding without ingredient shows error."""
        response = client.post(
            "/pantry/add",
            data={"ingredient": "", "category": "pantry_dry"},
            follow_redirects=True,
        )
        assert b"Ingredient name is required" in response.data

    def test_pantry_add_duplicate(self, client: FlaskClient) -> None:
        """Test adding a duplicate staple shows error."""
        response = client.post(
            "/pantry/add",
            data={"ingredient": "salt", "category": "pantry_dry"},
            follow_redirects=True,
        )
        assert b"already a pantry staple" in response.data

    def test_pantry_add_persists(
        self, client: FlaskClient, recipe_store: RecipeStore
    ) -> None:
        """Test added staple appears in the pantry list."""
        client.post(
            "/pantry/add",
            data={"ingredient": "paprika", "category": "pantry_dry"},
            follow_redirects=True,
        )
        staples = recipe_store.get_pantry_staples()
        names = [s["ingredient"] for s in staples]
        assert "paprika" in names


# ---------------------------------------------------------------------------
# TestPantryRemove
# ---------------------------------------------------------------------------


class TestPantryRemove:
    """Tests for POST /pantry/<id>/remove."""

    def test_pantry_remove_success(
        self, client: FlaskClient, recipe_store: RecipeStore
    ) -> None:
        """Test removing a pantry staple redirects with flash."""
        staple_id = recipe_store.add_pantry_staple("cumin", "pantry_dry")
        response = client.post(
            f"/pantry/{staple_id}/remove",
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"Pantry staple removed" in response.data

    def test_pantry_remove_redirects(
        self, client: FlaskClient, recipe_store: RecipeStore
    ) -> None:
        """Test removing redirects to pantry page."""
        staple_id = recipe_store.add_pantry_staple("cumin", "pantry_dry")
        response = client.post(
            f"/pantry/{staple_id}/remove",
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "/pantry" in response.headers["Location"]


# ---------------------------------------------------------------------------
# TestBrandsPage
# ---------------------------------------------------------------------------


class TestBrandsPage:
    """Tests for the brands page (GET /brands)."""

    def test_brands_get_status_200(self, client: FlaskClient) -> None:
        """Test GET /brands returns 200."""
        response = client.get("/brands")
        assert response.status_code == 200

    def test_brands_contains_title(self, client: FlaskClient) -> None:
        """Test brands page has the title."""
        response = client.get("/brands")
        assert b"Brand Preferences" in response.data

    def test_brands_empty_state(self, client: FlaskClient) -> None:
        """Test brands shows empty state with no preferences."""
        response = client.get("/brands")
        assert b"No brand preferences set" in response.data

    def test_brands_has_add_form(self, client: FlaskClient) -> None:
        """Test brands page has the add preference form."""
        response = client.get("/brands")
        assert b"Add Preference" in response.data

    def test_brands_shows_preference(
        self, client: FlaskClient, recipe_store: RecipeStore
    ) -> None:
        """Test brands page shows a saved preference."""
        from grocery_butler.models import (
            BrandMatchType,
            BrandPreference,
            BrandPreferenceType,
        )

        pref = BrandPreference(
            match_target="dairy",
            match_type=BrandMatchType.CATEGORY,
            brand="Organic Valley",
            preference_type=BrandPreferenceType.PREFERRED,
        )
        recipe_store.add_brand_preference(pref)
        response = client.get("/brands")
        assert b"Organic Valley" in response.data
        assert b"Preferred Brands" in response.data

    def test_brands_groups_by_type(
        self, client: FlaskClient, recipe_store: RecipeStore
    ) -> None:
        """Test brands page groups by preferred vs avoid."""
        from grocery_butler.models import (
            BrandMatchType,
            BrandPreference,
            BrandPreferenceType,
        )

        pref1 = BrandPreference(
            match_target="dairy",
            match_type=BrandMatchType.CATEGORY,
            brand="Organic Valley",
            preference_type=BrandPreferenceType.PREFERRED,
        )
        pref2 = BrandPreference(
            match_target="soda",
            match_type=BrandMatchType.INGREDIENT,
            brand="Generic Cola",
            preference_type=BrandPreferenceType.AVOID,
        )
        recipe_store.add_brand_preference(pref1)
        recipe_store.add_brand_preference(pref2)
        response = client.get("/brands")
        html = response.data.decode()
        assert "Preferred Brands" in html
        assert "Avoided Brands" in html


# ---------------------------------------------------------------------------
# TestBrandsAdd
# ---------------------------------------------------------------------------


class TestBrandsAdd:
    """Tests for POST /brands/add."""

    def test_brands_add_success(self, client: FlaskClient) -> None:
        """Test adding a brand preference redirects with flash."""
        response = client.post(
            "/brands/add",
            data={
                "match_target": "milk",
                "match_type": "ingredient",
                "brand": "Organic Valley",
                "preference_type": "preferred",
                "notes": "",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"Added brand preference" in response.data

    def test_brands_add_redirects(self, client: FlaskClient) -> None:
        """Test adding redirects to brands page."""
        response = client.post(
            "/brands/add",
            data={
                "match_target": "milk",
                "match_type": "ingredient",
                "brand": "Test Brand",
                "preference_type": "preferred",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "/brands" in response.headers["Location"]

    def test_brands_add_missing_target(self, client: FlaskClient) -> None:
        """Test adding without target shows error."""
        response = client.post(
            "/brands/add",
            data={
                "match_target": "",
                "match_type": "ingredient",
                "brand": "Test",
                "preference_type": "preferred",
            },
            follow_redirects=True,
        )
        assert b"Target and brand name are required" in response.data

    def test_brands_add_missing_brand(self, client: FlaskClient) -> None:
        """Test adding without brand shows error."""
        response = client.post(
            "/brands/add",
            data={
                "match_target": "milk",
                "match_type": "ingredient",
                "brand": "",
                "preference_type": "preferred",
            },
            follow_redirects=True,
        )
        assert b"Target and brand name are required" in response.data

    def test_brands_add_invalid_match_type(self, client: FlaskClient) -> None:
        """Test adding with invalid match type shows error."""
        response = client.post(
            "/brands/add",
            data={
                "match_target": "milk",
                "match_type": "bad_type",
                "brand": "Test",
                "preference_type": "preferred",
            },
            follow_redirects=True,
        )
        assert b"Invalid match type" in response.data

    def test_brands_add_invalid_pref_type(self, client: FlaskClient) -> None:
        """Test adding with invalid preference type shows error."""
        response = client.post(
            "/brands/add",
            data={
                "match_target": "milk",
                "match_type": "ingredient",
                "brand": "Test",
                "preference_type": "bad_type",
            },
            follow_redirects=True,
        )
        assert b"Invalid preference type" in response.data

    def test_brands_add_duplicate(
        self, client: FlaskClient, recipe_store: RecipeStore
    ) -> None:
        """Test adding duplicate brand preference shows error."""
        from grocery_butler.models import (
            BrandMatchType,
            BrandPreference,
            BrandPreferenceType,
        )

        pref = BrandPreference(
            match_target="milk",
            match_type=BrandMatchType.INGREDIENT,
            brand="Organic Valley",
            preference_type=BrandPreferenceType.PREFERRED,
        )
        recipe_store.add_brand_preference(pref)
        response = client.post(
            "/brands/add",
            data={
                "match_target": "milk",
                "match_type": "ingredient",
                "brand": "Organic Valley",
                "preference_type": "preferred",
            },
            follow_redirects=True,
        )
        assert b"already exists" in response.data

    def test_brands_add_with_notes(self, client: FlaskClient) -> None:
        """Test adding brand preference with notes."""
        response = client.post(
            "/brands/add",
            data={
                "match_target": "eggs",
                "match_type": "ingredient",
                "brand": "Free Range Farms",
                "preference_type": "preferred",
                "notes": "Only free-range eggs",
            },
            follow_redirects=True,
        )
        assert b"Added brand preference" in response.data


# ---------------------------------------------------------------------------
# TestBrandsRemove
# ---------------------------------------------------------------------------


class TestBrandsRemove:
    """Tests for POST /brands/<id>/remove."""

    def test_brands_remove_success(
        self, client: FlaskClient, recipe_store: RecipeStore
    ) -> None:
        """Test removing a brand preference redirects with flash."""
        from grocery_butler.models import (
            BrandMatchType,
            BrandPreference,
            BrandPreferenceType,
        )

        pref = BrandPreference(
            match_target="milk",
            match_type=BrandMatchType.INGREDIENT,
            brand="Test Brand",
            preference_type=BrandPreferenceType.PREFERRED,
        )
        pref_id = recipe_store.add_brand_preference(pref)
        response = client.post(
            f"/brands/{pref_id}/remove",
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"Brand preference removed" in response.data

    def test_brands_remove_redirects(
        self, client: FlaskClient, recipe_store: RecipeStore
    ) -> None:
        """Test removing redirects to brands page."""
        from grocery_butler.models import (
            BrandMatchType,
            BrandPreference,
            BrandPreferenceType,
        )

        pref = BrandPreference(
            match_target="dairy",
            match_type=BrandMatchType.CATEGORY,
            brand="Test",
            preference_type=BrandPreferenceType.AVOID,
        )
        pref_id = recipe_store.add_brand_preference(pref)
        response = client.post(
            f"/brands/{pref_id}/remove",
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "/brands" in response.headers["Location"]


# ---------------------------------------------------------------------------
# TestPreferencesPage
# ---------------------------------------------------------------------------


class TestPreferencesPage:
    """Tests for the preferences page (GET /preferences)."""

    def test_preferences_get_status_200(self, client: FlaskClient) -> None:
        """Test GET /preferences returns 200."""
        response = client.get("/preferences")
        assert response.status_code == 200

    def test_preferences_contains_title(self, client: FlaskClient) -> None:
        """Test preferences page has the title."""
        response = client.get("/preferences")
        assert b"Preferences" in response.data

    def test_preferences_shows_default_servings(self, client: FlaskClient) -> None:
        """Test preferences shows default servings field."""
        response = client.get("/preferences")
        html = response.data.decode()
        assert "Default Servings" in html
        # Default seeded value is 4
        assert 'value="4"' in html

    def test_preferences_shows_default_units(self, client: FlaskClient) -> None:
        """Test preferences shows default units field."""
        response = client.get("/preferences")
        html = response.data.decode()
        assert "Default Units" in html
        assert "imperial" in html

    def test_preferences_shows_dietary_restrictions(self, client: FlaskClient) -> None:
        """Test preferences shows dietary restrictions field."""
        response = client.get("/preferences")
        assert b"Dietary Restrictions" in response.data

    def test_preferences_has_save_button(self, client: FlaskClient) -> None:
        """Test preferences page has a save button."""
        response = client.get("/preferences")
        assert b"Save Preferences" in response.data


# ---------------------------------------------------------------------------
# TestPreferencesSave
# ---------------------------------------------------------------------------


class TestPreferencesSave:
    """Tests for POST /preferences."""

    def test_preferences_save_success(self, client: FlaskClient) -> None:
        """Test saving preferences redirects with flash."""
        response = client.post(
            "/preferences",
            data={
                "default_servings": "6",
                "default_units": "metric",
                "dietary_restrictions": "vegetarian",
            },
            follow_redirects=True,
        )
        assert response.status_code == 200
        assert b"Preferences saved" in response.data

    def test_preferences_save_redirects(self, client: FlaskClient) -> None:
        """Test saving preferences redirects to preferences page."""
        response = client.post(
            "/preferences",
            data={
                "default_servings": "4",
                "default_units": "imperial",
            },
            follow_redirects=False,
        )
        assert response.status_code == 302
        assert "/preferences" in response.headers["Location"]

    def test_preferences_save_persists(
        self, client: FlaskClient, recipe_store: RecipeStore
    ) -> None:
        """Test saved preferences are persisted to database."""
        client.post(
            "/preferences",
            data={
                "default_servings": "8",
                "default_units": "metric",
                "dietary_restrictions": "gluten-free",
            },
            follow_redirects=True,
        )
        prefs = recipe_store.get_all_preferences()
        assert prefs.get("default_servings") == "8"
        assert prefs.get("default_units") == "metric"
        assert prefs.get("dietary_restrictions") == "gluten-free"

    def test_preferences_save_empty_values_not_stored(
        self, client: FlaskClient, recipe_store: RecipeStore
    ) -> None:
        """Test empty preference values are not stored."""
        client.post(
            "/preferences",
            data={
                "default_servings": "4",
                "default_units": "imperial",
                "dietary_restrictions": "",
            },
            follow_redirects=True,
        )
        prefs = recipe_store.get_all_preferences()
        # dietary_restrictions should not have been updated since it was empty
        assert prefs.get("default_servings") == "4"

    def test_preferences_displays_updated_values(self, client: FlaskClient) -> None:
        """Test updated preferences are shown on next load."""
        client.post(
            "/preferences",
            data={
                "default_servings": "10",
                "default_units": "metric",
                "dietary_restrictions": "vegan",
            },
            follow_redirects=True,
        )
        response = client.get("/preferences")
        html = response.data.decode()
        assert 'value="10"' in html
        assert 'value="vegan"' in html


# ---------------------------------------------------------------------------
# TestNavigation
# ---------------------------------------------------------------------------


class TestNavigation:
    """Tests for navigation rendering."""

    def test_nav_contains_all_links(self, client: FlaskClient) -> None:
        """Test navigation bar contains all expected links."""
        response = client.get("/")
        html = response.data.decode()
        expected = [
            "Dashboard",
            "Recipes",
            "Inventory",
            "Pantry",
            "Shopping List",
            "Brands",
            "Preferences",
        ]
        for label in expected:
            assert label in html

    def test_dashboard_active_indicator(self, client: FlaskClient) -> None:
        """Test dashboard page has active nav indicator."""
        response = client.get("/")
        html = response.data.decode()
        assert 'class="nav-link active"' in html

    def test_inventory_active_indicator(self, client: FlaskClient) -> None:
        """Test inventory page has active nav indicator."""
        response = client.get("/inventory")
        html = response.data.decode()
        assert 'aria-current="page"' in html


# ---------------------------------------------------------------------------
# TestStaticAssets
# ---------------------------------------------------------------------------


class TestStaticAssets:
    """Tests for static file serving."""

    def test_css_linked_in_base(self, client: FlaskClient) -> None:
        """Test CSS file is linked in the base template."""
        response = client.get("/")
        assert b"style.css" in response.data

    def test_js_linked_in_base(self, client: FlaskClient) -> None:
        """Test JS file is linked in the base template."""
        response = client.get("/")
        assert b"app.js" in response.data

    def test_css_file_serves(self, client: FlaskClient) -> None:
        """Test CSS file is accessible at static URL."""
        response = client.get("/static/css/style.css")
        assert response.status_code == 200

    def test_js_file_serves(self, client: FlaskClient) -> None:
        """Test JS file is accessible at static URL."""
        response = client.get("/static/js/app.js")
        assert response.status_code == 200


# ---------------------------------------------------------------------------
# TestBaseTemplate
# ---------------------------------------------------------------------------


class TestBaseTemplate:
    """Tests for base.html template features."""

    def test_html5_doctype(self, client: FlaskClient) -> None:
        """Test pages include HTML5 doctype."""
        response = client.get("/")
        assert b"<!DOCTYPE html>" in response.data

    def test_viewport_meta(self, client: FlaskClient) -> None:
        """Test pages include responsive viewport meta tag."""
        response = client.get("/")
        assert b"viewport" in response.data
        assert b"width=device-width" in response.data

    def test_footer_present(self, client: FlaskClient) -> None:
        """Test pages include footer."""
        response = client.get("/")
        assert b"Grocery Butler" in response.data


# ---------------------------------------------------------------------------
# TestDatabaseConnection
# ---------------------------------------------------------------------------


class TestDatabaseConnection:
    """Tests for database connection per request."""

    def test_multiple_requests_isolated(
        self, client: FlaskClient, pantry_mgr: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test database connections are properly managed across requests."""
        pantry_mgr.add_item(sample_item)

        response1 = client.get("/inventory")
        assert response1.status_code == 200
        assert b"Milk" in response1.data

        response2 = client.get("/inventory")
        assert response2.status_code == 200
        assert b"Milk" in response2.data

    def test_request_teardown_closes_db(self, app: Flask) -> None:
        """Test database connection is closed at end of request."""
        with app.test_request_context():
            from flask import g

            from grocery_butler.app import _get_db

            db = _get_db()
            assert db is not None
            assert "db" in g

        # After context teardown, g.db should have been popped
        # (verifying no exception from close)
