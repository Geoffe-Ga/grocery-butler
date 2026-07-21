"""Tests for the /api/v1 low-stakes write endpoints (grocery_butler.api).

Issue #74: every bearer token minted in this module is now bound to the
exact (method, path, body) of the request it authorizes, via the shared
``tests.conftest.bearer_header`` helper. A single static header cannot
cover the many different requests exercised here, so the
``auth_headers`` fixture is a per-request minting factory rather than a
static header dict.
"""

from __future__ import annotations

import json
import os
import sqlite3
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient

    AuthHeaderFactory = Callable[[str, str, bytes], dict[str, str]]

from grocery_butler.app import create_app
from grocery_butler.auth_middleware import SECRET_ENV_VAR
from grocery_butler.models import (
    Ingredient,
    IngredientCategory,
    InventoryItem,
    InventoryStatus,
    ParsedMeal,
)
from grocery_butler.pantry_manager import PantryManager
from grocery_butler.recipe_store import RecipeStore
from tests.conftest import bearer_header

TEST_SECRET = "test-shared-secret"
SECRET_ENV = {SECRET_ENV_VAR: TEST_SECRET}

WRITE_ENDPOINTS = [
    ("POST", "/api/v1/stock/update"),
    ("POST", "/api/v1/stock/add"),
    ("POST", "/api/v1/restock/clear"),
    ("POST", "/api/v1/recipes/save"),
    ("DELETE", "/api/v1/recipes/1"),
]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a temporary database path for test isolation."""
    return str(tmp_path / "test_api_writes.db")


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
def auth_headers() -> AuthHeaderFactory:
    """Return a factory that mints a bearer header for one exact request.

    A single static header cannot authorize every request exercised in
    this module -- each write endpoint call has its own method, path,
    and body, and the request-bound token contract (issue #74) signs
    all three together. Callers invoke the returned factory once per
    request, e.g. ``auth_headers("POST", "/api/v1/stock/add", body)``.

    Returns:
        A callable ``(method, path, body=b"") -> {"Authorization": ...}``
        that mints tokens under the test shared secret.
    """

    def _make(method: str, path: str, body: bytes = b"") -> dict[str, str]:
        """Mint a bearer header bound to the given method/path/body."""
        with patch.dict(os.environ, SECRET_ENV):
            return bearer_header("rubotpaul", method, path, body)

    return _make


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
                ingredient="penne",
                quantity=1.0,
                unit="lb",
                category=IngredientCategory.PANTRY_DRY,
            )
        ],
        pantry_items=[],
    )


def _post(
    client: FlaskClient,
    path: str,
    auth_headers: AuthHeaderFactory,
    body: dict[str, Any],
) -> Any:
    """POST a JSON-serializable body with a token bound to the exact bytes sent.

    Serializes ``body`` exactly once so the identical bytes are both
    sent as the request payload and hashed into the bearer token's
    binding -- the two must match byte-for-byte for the server's HMAC
    verification to succeed (issue #74).

    Args:
        client: Flask test client.
        path: Request path to POST to.
        auth_headers: Factory fixture minting a bearer header for one
            exact (method, path, body) triple.
        body: JSON-serializable request payload.

    Returns:
        The Flask test client's response.
    """
    serialized = json.dumps(body).encode()
    headers = auth_headers("POST", path, serialized)
    return client.post(
        path, data=serialized, content_type="application/json", headers=headers
    )


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(("method", "path"), WRITE_ENDPOINTS)
def test_write_endpoints_require_bearer(
    client: FlaskClient, method: str, path: str
) -> None:
    """Every write endpoint returns 401 without a bearer token."""
    with patch.dict(os.environ, SECRET_ENV):
        response = client.open(path, method=method, json={})
    assert response.status_code == 401
    assert response.get_json() is not None


# ---------------------------------------------------------------------------
# POST /stock/update
# ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("token", "expected"),
    [
        ("in", InventoryStatus.ON_HAND),
        ("good", InventoryStatus.ON_HAND),
        ("low", InventoryStatus.LOW),
        ("out", InventoryStatus.OUT),
    ],
)
def test_stock_update_writes_status(
    client: FlaskClient,
    pantry_mgr: PantryManager,
    auth_headers: AuthHeaderFactory,
    sample_item: InventoryItem,
    token: str,
    expected: InventoryStatus,
) -> None:
    """Each accepted status token maps onto the stored InventoryStatus."""
    pantry_mgr.add_item(sample_item)
    with patch.dict(os.environ, SECRET_ENV):
        response = _post(
            client,
            "/api/v1/stock/update",
            auth_headers,
            {"item": "Milk", "status": token},
        )
    assert response.status_code == 200
    stored = pantry_mgr.get_item("milk")
    assert stored is not None
    assert stored.status is expected
    assert response.get_json()["status"] == expected.value


def test_stock_update_rejects_bad_status(
    client: FlaskClient,
    pantry_mgr: PantryManager,
    auth_headers: AuthHeaderFactory,
    sample_item: InventoryItem,
) -> None:
    """A status outside in/out/low/good is a 400 and writes nothing."""
    pantry_mgr.add_item(sample_item)
    with patch.dict(os.environ, SECRET_ENV):
        response = _post(
            client,
            "/api/v1/stock/update",
            auth_headers,
            {"item": "milk", "status": "plentiful"},
        )
    assert response.status_code == 400
    stored = pantry_mgr.get_item("milk")
    assert stored is not None
    assert stored.status is InventoryStatus.ON_HAND


def test_stock_update_rejects_non_string_status(
    client: FlaskClient, auth_headers: AuthHeaderFactory
) -> None:
    """A non-string status is a 400, not a server error."""
    with patch.dict(os.environ, SECRET_ENV):
        response = _post(
            client,
            "/api/v1/stock/update",
            auth_headers,
            {"item": "milk", "status": ["out"]},
        )
    assert response.status_code == 400


def test_stock_update_requires_item(
    client: FlaskClient, auth_headers: AuthHeaderFactory
) -> None:
    """A missing item is a 400."""
    with patch.dict(os.environ, SECRET_ENV):
        response = _post(
            client, "/api/v1/stock/update", auth_headers, {"status": "out"}
        )
    assert response.status_code == 400


def test_stock_update_unknown_item_404(
    client: FlaskClient, auth_headers: AuthHeaderFactory
) -> None:
    """Updating an untracked item is a JSON 404."""
    with patch.dict(os.environ, SECRET_ENV):
        response = _post(
            client,
            "/api/v1/stock/update",
            auth_headers,
            {"item": "caviar", "status": "out"},
        )
    assert response.status_code == 404
    assert "error" in response.get_json()


def test_stock_update_rejects_non_object_body(
    client: FlaskClient, auth_headers: AuthHeaderFactory
) -> None:
    """A non-object JSON body is a 400."""
    with patch.dict(os.environ, SECRET_ENV):
        serialized = json.dumps(["milk"]).encode()
        response = client.post(
            "/api/v1/stock/update",
            data=serialized,
            content_type="application/json",
            headers=auth_headers("POST", "/api/v1/stock/update", serialized),
        )
    assert response.status_code == 400


# ---------------------------------------------------------------------------
# POST /stock/add
# ---------------------------------------------------------------------------


def test_stock_add_creates_item(
    client: FlaskClient, pantry_mgr: PantryManager, auth_headers: AuthHeaderFactory
) -> None:
    """A valid add returns 201 and persists the item."""
    with patch.dict(os.environ, SECRET_ENV):
        response = _post(
            client,
            "/api/v1/stock/add",
            auth_headers,
            {"item": "Oat Milk", "category": "dairy"},
        )
    assert response.status_code == 201
    stored = pantry_mgr.get_item("oat milk")
    assert stored is not None
    assert stored.category is IngredientCategory.DAIRY
    payload = response.get_json()
    assert payload["ingredient"] == "oat milk"
    assert payload["display_name"] == "Oat Milk"


@pytest.mark.parametrize(
    "body",
    [
        {"category": "dairy"},
        {"item": "oat milk"},
        {"item": "", "category": "dairy"},
        {"item": "oat milk", "category": ""},
    ],
)
def test_stock_add_requires_item_and_category(
    client: FlaskClient, auth_headers: AuthHeaderFactory, body: dict[str, Any]
) -> None:
    """Missing item or category is a 400."""
    with patch.dict(os.environ, SECRET_ENV):
        response = _post(client, "/api/v1/stock/add", auth_headers, body)
    assert response.status_code == 400


def test_stock_add_rejects_unknown_category(
    client: FlaskClient, auth_headers: AuthHeaderFactory
) -> None:
    """A category outside IngredientCategory is a 400."""
    with patch.dict(os.environ, SECRET_ENV):
        response = _post(
            client,
            "/api/v1/stock/add",
            auth_headers,
            {"item": "oat milk", "category": "cryptids"},
        )
    assert response.status_code == 400


def test_stock_add_duplicate_conflict(
    client: FlaskClient,
    pantry_mgr: PantryManager,
    auth_headers: AuthHeaderFactory,
    sample_item: InventoryItem,
) -> None:
    """Adding an already-tracked ingredient is a 409."""
    pantry_mgr.add_item(sample_item)
    with patch.dict(os.environ, SECRET_ENV):
        response = _post(
            client,
            "/api/v1/stock/add",
            auth_headers,
            {"item": "Milk", "category": "dairy"},
        )
    assert response.status_code == 409


# ---------------------------------------------------------------------------
# POST /restock/clear
# ---------------------------------------------------------------------------


def test_restock_clear_empties_queue(
    client: FlaskClient,
    pantry_mgr: PantryManager,
    auth_headers: AuthHeaderFactory,
    sample_item: InventoryItem,
) -> None:
    """Clearing the restock queue moves low/out items back to on_hand."""
    pantry_mgr.add_item(sample_item)
    pantry_mgr.update_status("milk", InventoryStatus.OUT)
    assert pantry_mgr.get_restock_queue()
    with patch.dict(os.environ, SECRET_ENV):
        response = _post(client, "/api/v1/restock/clear", auth_headers, {})
    assert response.status_code == 200
    assert response.get_json()["ok"] is True
    assert pantry_mgr.get_restock_queue() == []


# ---------------------------------------------------------------------------
# POST /recipes/save
# ---------------------------------------------------------------------------


def _recipe_body(**overrides: Any) -> dict[str, Any]:
    """Return a valid recipes/save body, with optional overrides."""
    body: dict[str, Any] = {
        "name": "Veggie Chili",
        "ingredients": [
            {
                "ingredient": "kidney beans",
                "quantity": 2.0,
                "unit": "can",
                "category": "pantry_dry",
            },
            {
                "ingredient": "salt",
                "quantity": 1.0,
                "unit": "tsp",
                "category": "pantry_dry",
                "is_pantry_item": True,
            },
        ],
    }
    body.update(overrides)
    return body


def test_recipes_save_persists_recipe(
    client: FlaskClient, recipe_store: RecipeStore, auth_headers: AuthHeaderFactory
) -> None:
    """A valid save returns 201 and persists purchase + pantry splits."""
    with patch.dict(os.environ, SECRET_ENV):
        response = _post(client, "/api/v1/recipes/save", auth_headers, _recipe_body())
    assert response.status_code == 201
    payload = response.get_json()
    stored = recipe_store.get_recipe_by_id(payload["id"])
    assert stored is not None
    assert stored.name == "Veggie Chili"
    assert [i.ingredient for i in stored.purchase_items] == ["kidney beans"]
    assert [i.ingredient for i in stored.pantry_items] == ["salt"]


def test_recipes_save_duplicate_conflict(
    client: FlaskClient,
    recipe_store: RecipeStore,
    auth_headers: AuthHeaderFactory,
    sample_meal: ParsedMeal,
) -> None:
    """Saving a recipe whose name already exists is a 409."""
    recipe_store.save_recipe(sample_meal)
    with patch.dict(os.environ, SECRET_ENV):
        response = _post(
            client,
            "/api/v1/recipes/save",
            auth_headers,
            _recipe_body(name="Test Pasta"),
        )
    assert response.status_code == 409


def test_recipes_save_raced_duplicate_returns_409(
    client: FlaskClient,
    app: Flask,
    auth_headers: dict[str, str],
) -> None:
    """A concurrent insert that races the pre-check is a 409, not a 500.

    Regression test for issue #79: `post_recipes_save` checks
    `store.get_recipe(name)` for None, then calls `store.save_recipe(meal)`.
    If another request inserts the same name in between (or the pre-check
    is otherwise stale), `save_recipe` raises
    `grocery_butler.db.adapter.IntegrityError` because `recipes.name` is
    UNIQUE, and today that propagates as an unhandled 500 instead of being
    translated to a 409 the way `post_stock_add` handles its own
    `IntegrityError` case.

    PROPAGATE_EXCEPTIONS is explicitly disabled (see
    TestInternalServerErrorContract in test_api_errors.py) so the test
    client exercises the request pipeline the way it behaves in
    production instead of TESTING=True re-raising the exception.
    """
    app.config["PROPAGATE_EXCEPTIONS"] = False
    with (
        patch.object(RecipeStore, "get_recipe", return_value=None),
        patch.object(
            RecipeStore,
            "save_recipe",
            side_effect=sqlite3.IntegrityError(
                "UNIQUE constraint failed: recipes.name"
            ),
        ),
        patch.dict(os.environ, SECRET_ENV),
    ):
        response = _post(client, "/api/v1/recipes/save", auth_headers, _recipe_body())
    assert response.status_code == 409


@pytest.mark.parametrize(
    "body",
    [
        {
            "ingredients": [
                {"ingredient": "x", "quantity": 1, "unit": "lb", "category": "other"}
            ]
        },
        {"name": "Veggie Chili"},
        {"name": "", "ingredients": []},
        {"name": "Veggie Chili", "ingredients": []},
    ],
)
def test_recipes_save_requires_name_and_ingredients(
    client: FlaskClient, auth_headers: AuthHeaderFactory, body: dict[str, Any]
) -> None:
    """Missing name or ingredients is a 400."""
    with patch.dict(os.environ, SECRET_ENV):
        response = _post(client, "/api/v1/recipes/save", auth_headers, body)
    assert response.status_code == 400


@pytest.mark.parametrize(
    "ingredients",
    ["penne", [{"ingredient": "penne"}], [42]],
)
def test_recipes_save_rejects_invalid_ingredients(
    client: FlaskClient, auth_headers: AuthHeaderFactory, ingredients: Any
) -> None:
    """Non-list or non-Ingredient payloads are a 400."""
    with patch.dict(os.environ, SECRET_ENV):
        response = _post(
            client,
            "/api/v1/recipes/save",
            auth_headers,
            _recipe_body(ingredients=ingredients),
        )
    assert response.status_code == 400


@pytest.mark.parametrize("servings", ["four", 0, -2, True])
def test_recipes_save_rejects_bad_servings(
    client: FlaskClient, auth_headers: AuthHeaderFactory, servings: Any
) -> None:
    """A non-positive-int servings override is a 400."""
    with patch.dict(os.environ, SECRET_ENV):
        response = _post(
            client,
            "/api/v1/recipes/save",
            auth_headers,
            _recipe_body(servings=servings),
        )
    assert response.status_code == 400


def test_recipes_save_honors_servings(
    client: FlaskClient, recipe_store: RecipeStore, auth_headers: AuthHeaderFactory
) -> None:
    """An explicit servings override is persisted."""
    with patch.dict(os.environ, SECRET_ENV):
        response = _post(
            client, "/api/v1/recipes/save", auth_headers, _recipe_body(servings=6)
        )
    assert response.status_code == 201
    stored = recipe_store.get_recipe_by_id(response.get_json()["id"])
    assert stored is not None
    assert stored.servings == 6


# ---------------------------------------------------------------------------
# DELETE /recipes/<id>
# ---------------------------------------------------------------------------


def test_recipes_delete_removes_recipe(
    client: FlaskClient,
    recipe_store: RecipeStore,
    auth_headers: AuthHeaderFactory,
    sample_meal: ParsedMeal,
) -> None:
    """Deleting an existing recipe returns 204 and removes it."""
    recipe_id = recipe_store.save_recipe(sample_meal)
    recipe_path = f"/api/v1/recipes/{recipe_id}"
    with patch.dict(os.environ, SECRET_ENV):
        response = client.delete(
            recipe_path, headers=auth_headers("DELETE", recipe_path)
        )
    assert response.status_code == 204
    assert response.data == b""
    assert recipe_store.get_recipe_by_id(recipe_id) is None


def test_recipes_delete_unknown_404(
    client: FlaskClient, auth_headers: AuthHeaderFactory
) -> None:
    """Deleting an unknown recipe id is a JSON 404."""
    with patch.dict(os.environ, SECRET_ENV):
        response = client.delete(
            "/api/v1/recipes/999",
            headers=auth_headers("DELETE", "/api/v1/recipes/999"),
        )
    assert response.status_code == 404
    assert "error" in response.get_json()
