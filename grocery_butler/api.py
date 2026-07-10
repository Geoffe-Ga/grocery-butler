"""Internal JSON API for RubotPaul at ``/api/v1``.

Separate Flask blueprint from the user-facing HTML app: same database and
stores, but bearer-token auth (vendored HMAC middleware), JSON responses,
and no HTML. Registered on the existing web app by ``create_app`` so it
ships in the same Railway web process.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import os
import uuid
from decimal import Decimal
from typing import TYPE_CHECKING, Any

from flask import Blueprint, abort, current_app, jsonify, request
from pydantic import ValidationError

from grocery_butler.auth_middleware import require_bearer
from grocery_butler.claude_utils import make_anthropic_client
from grocery_butler.config import ConfigError, load_config
from grocery_butler.consolidator import Consolidator
from grocery_butler.db.adapter import IntegrityError
from grocery_butler.meal_parser import MealParser
from grocery_butler.models import (
    BrandPreference,
    CartSummary,
    Ingredient,
    IngredientCategory,
    InventoryItem,
    InventoryStatus,
    ParsedMeal,
    PendingActionStatus,
    ShoppingListItem,
)
from grocery_butler.pantry_manager import PantryManager
from grocery_butler.pending_actions import PendingActionsStore
from grocery_butler.recipe_store import RecipeStore
from grocery_butler.safeway_pipeline import SafewayPipeline, SafewayPipelineError

if TYPE_CHECKING:
    from collections.abc import Callable

    from flask import Response
    from werkzeug.exceptions import HTTPException

    from grocery_butler.models import PendingAction

api_v1 = Blueprint("api_v1", __name__, url_prefix="/api/v1")

#: How long a staged destructive action stays confirmable.
CONFIRMATION_TTL = dt.timedelta(minutes=5)


@api_v1.errorhandler(400)
@api_v1.errorhandler(401)
@api_v1.errorhandler(404)
@api_v1.errorhandler(409)
@api_v1.errorhandler(410)
def _json_http_error(exc: HTTPException) -> tuple[Response, int]:
    """Render blueprint HTTP errors as JSON instead of Flask's HTML."""
    return jsonify(error=exc.description), exc.code or 500


def _pantry_manager() -> PantryManager:
    """Return a PantryManager bound to the app's database."""
    return PantryManager(current_app.config["DATABASE_PATH"])


def _recipe_store() -> RecipeStore:
    """Return a RecipeStore bound to the app's database."""
    return RecipeStore(current_app.config["DATABASE_PATH"])


def _anthropic_client() -> Any | None:
    """Return an Anthropic client from the environment, or None.

    Without a key the Claude-backed pipelines fall back to their
    pure-Python paths (stored recipes, simple consolidation).
    """
    api_key = os.environ.get("ANTHROPIC_API_KEY", "")
    if not api_key:
        return None
    return make_anthropic_client(api_key)


def _meal_parser() -> MealParser:
    """Return a MealParser wired to the app's database and Claude."""
    return MealParser(_recipe_store(), _anthropic_client())


def _consolidator() -> Consolidator:
    """Return a Consolidator wired to Claude when available."""
    return Consolidator(anthropic_client=_anthropic_client())


def _safeway_pipeline() -> SafewayPipeline:
    """Return a SafewayPipeline for the app's database.

    Raises:
        ConfigError: If required environment configuration is missing.
        SafewayPipelineError: If Safeway credentials are missing.
    """
    config = load_config()
    return SafewayPipeline(
        config, current_app.config["DATABASE_PATH"], _anthropic_client()
    )


def _json_body() -> dict[str, Any]:
    """Return the request's JSON object body, or abort 400."""
    body = request.get_json(silent=True)
    if not isinstance(body, dict):
        abort(400, description="request body must be a JSON object")
    return body


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


# ---------------------------------------------------------------------------
# Compute endpoints (read-only, no writes)
# ---------------------------------------------------------------------------


def _split_meal_names(text: str) -> list[str]:
    """Split free text into meal names on newlines and commas."""
    return [
        name.strip()
        for line in text.split("\n")
        for name in line.split(",")
        if name.strip()
    ]


def _parse_meal_payloads(raw_meals: Any) -> list[ParsedMeal]:
    """Validate raw meal payloads into ParsedMeal models, or abort 400."""
    if not isinstance(raw_meals, list):
        abort(400, description="meals must be a list of meal objects")
    try:
        return [ParsedMeal.model_validate(meal) for meal in raw_meals]
    except ValidationError as exc:
        abort(400, description=f"invalid meal payload: {exc.error_count()} error(s)")


def _parse_shopping_list_payloads(raw_items: Any) -> list[ShoppingListItem]:
    """Validate raw shopping list payloads, or abort 400."""
    if not isinstance(raw_items, list) or not raw_items:
        abort(400, description="shopping_list required")
    try:
        return [ShoppingListItem.model_validate(item) for item in raw_items]
    except ValidationError as exc:
        abort(
            400,
            description=f"invalid shopping list item: {exc.error_count()} error(s)",
        )


def _cart_total(cart: CartSummary) -> Decimal:
    """Sum item and restock costs without float artifacts."""
    return sum(
        (
            Decimal(str(cart_item.estimated_cost))
            for cart_item in [*cart.items, *cart.restock_items]
        ),
        Decimal("0"),
    )


@api_v1.post("/meals/parse")
def post_meals_parse() -> Response:
    """Parse free-text meal names into structured ingredient lists."""
    require_bearer()
    text = str(_json_body().get("text", "")).strip()
    if not text:
        abort(400, description="text required")
    meals = _meal_parser().parse_meals(_split_meal_names(text))
    return jsonify(meals=[meal.model_dump(mode="json") for meal in meals])


@api_v1.post("/shopping-list/preview")
def post_shopping_list_preview() -> Response:
    """Consolidate meals into a shopping list without persisting anything."""
    require_bearer()
    body = _json_body()
    meals = _parse_meal_payloads(body.get("meals", []))
    include_restock = bool(body.get("include_restock", True))
    restock_queue = _pantry_manager().get_restock_queue() if include_restock else []
    items = _consolidator().consolidate(
        meals=meals,
        restock_queue=restock_queue,
        pantry_staples=_recipe_store().get_pantry_staple_names(),
    )
    return jsonify(items=[item.model_dump(mode="json") for item in items])


@api_v1.post("/order/preview")
def post_order_preview() -> Response | tuple[Response, int]:
    """Build a Safeway cart for review without submitting an order."""
    require_bearer()
    shopping_list = _parse_shopping_list_payloads(_json_body().get("shopping_list"))
    try:
        pipeline = _safeway_pipeline()
    except (ConfigError, SafewayPipelineError) as exc:
        return jsonify(error=f"safeway pipeline unavailable: {exc}"), 503
    try:
        cart = pipeline.build_cart_only(shopping_list)
    except SafewayPipelineError as exc:
        return jsonify(error=f"cart build failed: {exc}"), 503
    finally:
        pipeline.close()
    return jsonify(cart=cart.model_dump(mode="json"), total=str(_cart_total(cart)))


# ---------------------------------------------------------------------------
# Low-stakes write endpoints (immediate execution, no confirmation staging)
# ---------------------------------------------------------------------------

# RubotPaul speaks in/out/low/good; the store models on_hand/low/out.
_STATUS_TOKENS: dict[str, InventoryStatus] = {
    "in": InventoryStatus.ON_HAND,
    "good": InventoryStatus.ON_HAND,
    "low": InventoryStatus.LOW,
    "out": InventoryStatus.OUT,
}


def _required_text(body: dict[str, Any], key: str, message: str) -> str:
    """Return a non-empty stripped string field from the body, or abort 400."""
    value = body.get(key)
    if not isinstance(value, str) or not value.strip():
        abort(400, description=message)
    return value.strip()


def _parse_ingredient_payloads(raw_ingredients: Any) -> list[Ingredient]:
    """Validate raw ingredient payloads into Ingredient models, or abort 400."""
    if not isinstance(raw_ingredients, list) or not raw_ingredients:
        abort(400, description="name and ingredients required")
    try:
        return [Ingredient.model_validate(item) for item in raw_ingredients]
    except ValidationError as exc:
        abort(400, description=f"invalid ingredient: {exc.error_count()} error(s)")


def _parse_servings(body: dict[str, Any]) -> int:
    """Return the optional servings override (default 4), or abort 400."""
    servings = body.get("servings", 4)
    if isinstance(servings, bool) or not isinstance(servings, int) or servings < 1:
        abort(400, description="servings must be a positive integer")
    return servings


@api_v1.post("/stock/update")
def post_stock_update() -> Response:
    """Set a tracked item's stock status; writes immediately."""
    require_bearer()
    body = _json_body()
    status = body.get("status")
    if not isinstance(status, str) or status not in _STATUS_TOKENS:
        abort(400, description="status must be one of in/out/low/good")
    item = _required_text(body, "item", "item required")
    manager = _pantry_manager()
    if manager.get_item(item) is None:
        abort(404, description="item not tracked")
    manager.update_status(item, _STATUS_TOKENS[status])
    updated = manager.get_item(item)
    if updated is None:  # pragma: no cover - item removed mid-request
        abort(404, description="item not tracked")
    return jsonify(_item(updated))


@api_v1.post("/stock/add")
def post_stock_add() -> tuple[Response, int]:
    """Start tracking a new inventory item; writes immediately."""
    require_bearer()
    body = _json_body()
    name = _required_text(body, "item", "item and category required")
    raw_category = _required_text(body, "category", "item and category required")
    try:
        category = IngredientCategory(raw_category)
    except ValueError:
        abort(400, description="unknown category")
    manager = _pantry_manager()
    try:
        manager.add_item(
            InventoryItem(ingredient=name, display_name=name, category=category)
        )
    except IntegrityError:
        abort(409, description="item already tracked")
    created = manager.get_item(name)
    if created is None:  # pragma: no cover - item removed mid-request
        abort(404, description="item not tracked")
    return jsonify(_item(created)), 201


@api_v1.post("/restock/clear")
def post_restock_clear() -> Response:
    """Move every low/out item back to on_hand; writes immediately."""
    require_bearer()
    cleared = _pantry_manager().clear_restock_queue()
    return jsonify(ok=True, cleared=cleared)


@api_v1.post("/recipes/save")
def post_recipes_save() -> tuple[Response, int]:
    """Save a new recipe; 409 if the name is already taken."""
    require_bearer()
    body = _json_body()
    name = _required_text(body, "name", "name and ingredients required")
    ingredients = _parse_ingredient_payloads(body.get("ingredients"))
    servings = _parse_servings(body)
    store = _recipe_store()
    if store.get_recipe(name) is not None:
        abort(409, description="recipe with that name exists")
    meal = ParsedMeal(
        name=name,
        servings=servings,
        known_recipe=True,
        needs_confirmation=False,
        purchase_items=[i for i in ingredients if not i.is_pantry_item],
        pantry_items=[i for i in ingredients if i.is_pantry_item],
    )
    recipe_id = store.save_recipe(meal)
    return jsonify(id=recipe_id, **meal.model_dump(mode="json")), 201


@api_v1.delete("/recipes/<int:recipe_id>")
def delete_recipe(recipe_id: int) -> tuple[str, int]:
    """Delete a recipe by id; 404 if unknown."""
    require_bearer()
    store = _recipe_store()
    if store.get_recipe_by_id(recipe_id) is None:
        abort(404, description="recipe not found")
    store.delete_recipe(recipe_id)
    return "", 204


# ---------------------------------------------------------------------------
# Destructive endpoints (staged confirmation: draft -> confirm -> execute)
#
# The chat-reply confirmation pattern from PIVOT.md: these endpoints stage
# a pending_actions row (the audit log) and return an action_id. RubotPaul
# posts the draft to chat and calls /actions/confirm only after the user
# replies "confirm". Nothing destructive happens at staging time.
# ---------------------------------------------------------------------------


def _pending_store() -> PendingActionsStore:
    """Return a PendingActionsStore bound to the app's database."""
    return PendingActionsStore(current_app.config["DATABASE_PATH"])


def _stage_pending(kind: str, payload: dict[str, Any]) -> str:
    """Stage a pending action with the standard TTL; return its action_id."""
    action = _pending_store().insert_pending_action(
        action_id=str(uuid.uuid4()),
        kind=kind,
        payload=payload,
        expires_at=dt.datetime.now(dt.UTC) + CONFIRMATION_TTL,
    )
    return action.action_id


def _parse_cart_payload(body: dict[str, Any]) -> CartSummary:
    """Validate the request's cart into a CartSummary, or abort 400."""
    raw_cart = body.get("cart")
    if raw_cart is None:
        abort(400, description="cart required")
    try:
        cart = CartSummary.model_validate(raw_cart)
    except ValidationError as exc:
        abort(400, description=f"invalid cart: {exc.error_count()} error(s)")
    if not cart.items and not cart.restock_items:
        abort(400, description="cart is empty — nothing to order")
    return cart


@api_v1.post("/order/submit")
def post_order_submit() -> Response:
    """Stage a Safeway order for confirmation. Never submits directly."""
    require_bearer()
    body = _json_body()
    cart = _parse_cart_payload(body)
    total = str(body.get("total") or _cart_total(cart))
    action_id = _stage_pending(
        "safeway_order_submit",
        {"cart": cart.model_dump(mode="json"), "total": total},
    )
    item_count = len(cart.items) + len(cart.restock_items)
    return jsonify(
        status="pending_confirmation",
        action_id=action_id,
        message=(
            f"Staged Safeway order ({item_count} items, ${total}). "
            "Reply 'confirm' to submit."
        ),
    )


@api_v1.post("/brands/set")
def post_brands_set() -> Response:
    """Stage a brand preference rule for confirmation."""
    require_bearer()
    body = _json_body()
    try:
        pref = BrandPreference.model_validate(body)
    except ValidationError as exc:
        abort(400, description=f"invalid brand rule: {exc.error_count()} error(s)")
    action_id = _stage_pending("brands_set", pref.model_dump(mode="json"))
    return jsonify(
        status="pending_confirmation",
        action_id=action_id,
        message=(
            f"Staged brand rule: {pref.preference_type.value} '{pref.brand}' "
            f"for {pref.match_target}. Reply 'confirm' to apply."
        ),
    )


def _parse_preferences_payload(body: dict[str, Any]) -> dict[str, str]:
    """Validate a non-empty string-to-string preferences map, or abort 400."""
    preferences = body.get("preferences")
    if not isinstance(preferences, dict) or not preferences:
        abort(400, description="preferences must be a non-empty object")
    # JSON object keys are always strings; only values need checking.
    if not all(isinstance(value, str) for value in preferences.values()):
        abort(400, description="preference values must be strings")
    return preferences


@api_v1.post("/preferences/set")
def post_preferences_set() -> Response:
    """Stage app-level preference changes for confirmation."""
    require_bearer()
    preferences = _parse_preferences_payload(_json_body())
    action_id = _stage_pending("preferences_set", {"preferences": preferences})
    keys = ", ".join(sorted(preferences))
    return jsonify(
        status="pending_confirmation",
        action_id=action_id,
        message=f"Staged preference change ({keys}). Reply 'confirm' to apply.",
    )


def _load_pending_action(body: dict[str, Any]) -> PendingAction:
    """Fetch the pending action named by the request body, or abort 400/404."""
    action_id = body.get("action_id")
    if not isinstance(action_id, str) or not action_id.strip():
        abort(400, description="action_id required")
    action = _pending_store().get_pending_action(action_id.strip())
    if action is None:
        abort(404, description="unknown action_id")
    return action


def _claim_pending(store: PendingActionsStore, action_id: str) -> None:
    """Atomically claim a pending action as approved, or abort 409.

    The guarded UPDATE in the store is the exactly-once gate: only one
    caller can transition the row out of ``pending``, so a raced double
    confirm cannot execute twice.
    """
    if not store.mark_pending_approved(action_id):
        abort(409, description="action already resolved")


def _approved(action: PendingAction, result: dict[str, Any]) -> Response:
    """Serialize a successful confirmation response."""
    return jsonify(
        status="approved",
        action_id=action.action_id,
        kind=action.kind,
        result=result,
    )


def _confirm_order_submit(
    action: PendingAction, store: PendingActionsStore
) -> Response | tuple[Response, int]:
    """Submit the exact staged cart to Safeway (real money)."""
    cart = CartSummary.model_validate(action.payload["cart"])
    try:
        pipeline = _safeway_pipeline()
    except (ConfigError, SafewayPipelineError) as exc:
        # Not claimed yet: the action stays pending so it can be retried
        # (until it expires) once configuration is fixed.
        return jsonify(error=f"safeway pipeline unavailable: {exc}"), 503
    _claim_pending(store, action.action_id)
    try:
        result = pipeline.submit_cart(cart)
    except SafewayPipelineError as exc:
        return jsonify(error=f"order submission failed: {exc}"), 502
    finally:
        pipeline.close()
    if not result.success:
        return jsonify(error=result.error_message), 502
    confirmation = (
        dataclasses.asdict(result.confirmation) if result.confirmation else {}
    )
    return _approved(
        action, {**confirmation, "items_restocked": result.items_restocked}
    )


def _confirm_brands_set(action: PendingAction, store: PendingActionsStore) -> Response:
    """Apply the staged brand preference rule."""
    pref = BrandPreference.model_validate(action.payload)
    _claim_pending(store, action.action_id)
    pref_id = _recipe_store().add_brand_preference(pref)
    return _approved(action, {"id": pref_id, **pref.model_dump(mode="json")})


def _confirm_preferences_set(
    action: PendingAction, store: PendingActionsStore
) -> Response:
    """Apply the staged app-level preference changes."""
    preferences: dict[str, str] = action.payload["preferences"]
    _claim_pending(store, action.action_id)
    store_ = _recipe_store()
    for key, value in preferences.items():
        store_.set_preference(key, value)
    return _approved(action, {"updated": len(preferences)})


_CONFIRM_EXECUTORS: dict[
    str,
    Callable[[PendingAction, PendingActionsStore], Response | tuple[Response, int]],
] = {
    "safeway_order_submit": _confirm_order_submit,
    "brands_set": _confirm_brands_set,
    "preferences_set": _confirm_preferences_set,
}


@api_v1.post("/actions/confirm")
def post_actions_confirm() -> Response | tuple[Response, int]:
    """Execute a staged action exactly once after chat confirmation."""
    require_bearer()
    action = _load_pending_action(_json_body())
    store = _pending_store()
    if action.status is not PendingActionStatus.PENDING:
        abort(409, description="action already resolved")
    if action.is_expired():
        store.mark_pending_expired(action.action_id)
        abort(410, description="action expired — stage it again")
    executor = _CONFIRM_EXECUTORS.get(action.kind)
    if executor is None:
        abort(400, description=f"unknown action kind: {action.kind}")
    return executor(action, store)


@api_v1.post("/actions/deny")
def post_actions_deny() -> Response:
    """Mark a staged action as denied (audit trail; nothing executes).

    Denial is safe regardless of expiry, so unlike confirm this does not
    check the TTL — a denied-after-expiry action records the user's
    explicit "no" rather than a timeout.
    """
    require_bearer()
    action = _load_pending_action(_json_body())
    if not _pending_store().mark_pending_denied(action.action_id):
        abort(409, description="action already resolved")
    return jsonify(status="denied", action_id=action.action_id, kind=action.kind)
