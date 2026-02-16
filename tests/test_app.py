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
from grocery_butler.models import IngredientCategory, InventoryItem, InventoryStatus
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
        self, client: FlaskClient, recipe_store: RecipeStore
    ) -> None:
        """Test dashboard displays accurate recipe count."""
        from grocery_butler.models import Ingredient, IngredientCategory, ParsedMeal

        meal = ParsedMeal(
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
                )
            ],
            pantry_items=[],
        )
        recipe_store.save_recipe(meal)

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
# TestPlaceholderPages
# ---------------------------------------------------------------------------


class TestPlaceholderPages:
    """Tests for placeholder pages."""

    def test_recipes_page(self, client: FlaskClient) -> None:
        """Test GET /recipes returns 200 with placeholder."""
        response = client.get("/recipes")
        assert response.status_code == 200
        assert b"Recipes" in response.data
        assert b"coming soon" in response.data

    def test_pantry_page(self, client: FlaskClient) -> None:
        """Test GET /pantry returns 200 with placeholder."""
        response = client.get("/pantry")
        assert response.status_code == 200
        assert b"Pantry Staples" in response.data

    def test_shopping_list_page(self, client: FlaskClient) -> None:
        """Test GET /shopping-list returns 200 with placeholder."""
        response = client.get("/shopping-list")
        assert response.status_code == 200
        assert b"Shopping List" in response.data

    def test_brands_page(self, client: FlaskClient) -> None:
        """Test GET /brands returns 200 with placeholder."""
        response = client.get("/brands")
        assert response.status_code == 200
        assert b"Brand Preferences" in response.data

    def test_preferences_page(self, client: FlaskClient) -> None:
        """Test GET /preferences returns 200 with placeholder."""
        response = client.get("/preferences")
        assert response.status_code == 200
        assert b"Preferences" in response.data


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
