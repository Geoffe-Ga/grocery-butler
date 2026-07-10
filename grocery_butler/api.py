"""Internal JSON API for RubotPaul at ``/api/v1``.

Separate Flask blueprint from the user-facing HTML app: same database and
stores, but bearer-token auth (vendored HMAC middleware), JSON responses,
and no HTML. Registered on the existing web app by ``create_app`` so it
ships in the same Railway web process.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from flask import Blueprint, current_app, jsonify

from grocery_butler.auth_middleware import require_bearer
from grocery_butler.pantry_manager import PantryManager
from grocery_butler.recipe_store import RecipeStore

if TYPE_CHECKING:
    from flask import Response

    from grocery_butler.models import BrandPreference, InventoryItem

api_v1 = Blueprint("api_v1", __name__, url_prefix="/api/v1")


def _pantry_manager() -> PantryManager:
    """Return a PantryManager bound to the app's database."""
    return PantryManager(current_app.config["DATABASE_PATH"])


def _recipe_store() -> RecipeStore:
    """Return a RecipeStore bound to the app's database."""
    return RecipeStore(current_app.config["DATABASE_PATH"])


# ---------------------------------------------------------------------------
# Serializers
# ---------------------------------------------------------------------------


def _item(item: InventoryItem) -> dict[str, Any]:
    """Serialize an InventoryItem to a JSON-safe dict."""
    return item.model_dump(mode="json")


def _brand(pref: BrandPreference) -> dict[str, Any]:
    """Serialize a BrandPreference to a JSON-safe dict."""
    return pref.model_dump(mode="json")


# ---------------------------------------------------------------------------
# Read endpoints
# ---------------------------------------------------------------------------


@api_v1.get("/inventory")
def get_inventory() -> Response:
    """Return all tracked inventory items."""
    require_bearer()
    items = _pantry_manager().get_inventory()
    return jsonify(items=[_item(i) for i in items])


@api_v1.get("/pantry")
def get_pantry() -> Response:
    """Return all pantry staples."""
    require_bearer()
    return jsonify(staples=_recipe_store().get_pantry_staples())


@api_v1.get("/recipes")
def list_recipes() -> Response:
    """Return summary rows for all saved recipes."""
    require_bearer()
    return jsonify(recipes=_recipe_store().list_recipes())


@api_v1.get("/recipes/<int:recipe_id>")
def get_recipe(recipe_id: int) -> Response | tuple[Response, int]:
    """Return one full recipe by id, or a JSON 404 if unknown."""
    require_bearer()
    meal = _recipe_store().get_recipe_by_id(recipe_id)
    if meal is None:
        return jsonify(error="recipe not found"), 404
    return jsonify(id=recipe_id, **meal.model_dump(mode="json"))


@api_v1.get("/brands")
def list_brands() -> Response:
    """Return all brand preference rules."""
    require_bearer()
    prefs = _recipe_store().get_brand_preferences()
    return jsonify(brands=[_brand(p) for p in prefs])


@api_v1.get("/preferences")
def get_preferences() -> Response:
    """Return all app-level preferences as a flat object."""
    require_bearer()
    return jsonify(_recipe_store().get_all_preferences())


@api_v1.get("/restock")
def get_restock() -> Response:
    """Return the restock queue (items low or out)."""
    require_bearer()
    items = _pantry_manager().get_restock_queue()
    return jsonify(items=[_item(i) for i in items])
