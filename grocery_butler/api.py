"""Internal JSON API for RubotPaul at ``/api/v1``.

Separate Flask blueprint from the user-facing HTML app: same database and
stores, but bearer-token auth (vendored HMAC middleware), JSON responses,
and no HTML. Registered on the existing web app by ``create_app`` so it
ships in the same Railway web process.
"""

from __future__ import annotations

import dataclasses
import datetime as dt
import logging
import os
import uuid
from decimal import ROUND_HALF_UP, Decimal, InvalidOperation
from typing import TYPE_CHECKING, Any

from flask import Blueprint, abort, current_app, g, jsonify, request
from pydantic import ValidationError

from grocery_butler import order_service
from grocery_butler.auth_middleware import require_bearer
from grocery_butler.claude_utils import make_anthropic_client
from grocery_butler.config import ConfigError, load_config, parse_order_value_cap
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
from grocery_butler.order_service import (
    ORDER_SUBMISSION_DISABLED_MESSAGE,
    OrderOutcome,
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

logger = logging.getLogger(__name__)

api_v1 = Blueprint("api_v1", __name__, url_prefix="/api/v1")

LOG = logging.getLogger("api")

#: How long a staged destructive action stays confirmable.
CONFIRMATION_TTL = dt.timedelta(minutes=5)

#: Endpoints on this blueprint exempt from the auth-by-default hook below.
#: Intentionally empty: every route on ``api_v1`` requires a bearer token.
#: This is the explicit opt-out seam for a future unauthenticated route
#: (e.g. a webhook) -- health endpoints are registered at the app level
#: in ``grocery_butler.app``, not on this blueprint, so they never need
#: an entry here.
_AUTH_EXEMPT_ENDPOINTS: frozenset[str] = frozenset()


@api_v1.before_request
def _enforce_bearer_auth() -> None:
    """Require a valid bearer token for every request on this blueprint.

    Issue #74 AC#2: auth-by-default. Previously each view called
    ``require_bearer()`` individually, so a route that forgot the call
    would silently ship unauthenticated. This ``before_request`` hook is
    now the single enforcement point for the whole blueprint; routes no
    longer call ``require_bearer()`` themselves. The verified caller_id
    is bound to ``flask.g`` so views that stamp the audit trail
    (requester/resolver, issue #75 W1) can read it via
    :func:`_request_caller_id` without re-verifying the token.

    Raises:
        werkzeug.exceptions.HTTPException: Aborts with 401 (via
            :func:`grocery_butler.auth_middleware.require_bearer`) when
            the request's bearer token is missing or invalid for this
            exact request.
    """
    if request.endpoint in _AUTH_EXEMPT_ENDPOINTS:
        return
    g.caller_id = require_bearer()


def _request_caller_id() -> str:
    """Return the caller_id the auth hook verified for this request.

    :func:`_enforce_bearer_auth` binds the token's caller_id to
    ``flask.g`` before any view runs, so views that record the caller in
    the pending-actions audit trail (issue #75 W1) read it here instead
    of re-verifying the bearer token themselves.

    Returns:
        The caller_id embedded in the request's validated bearer token.

    Raises:
        werkzeug.exceptions.HTTPException: Aborts with 401 if no
            caller_id is bound to this request — e.g. a future
            auth-exempt endpoint mistakenly asking for a caller
            identity it never authenticated.
    """
    caller_id = getattr(g, "caller_id", None)
    if not isinstance(caller_id, str):
        abort(401, description="missing bearer token")
    return caller_id


# ---------------------------------------------------------------------------
# Sanitized error messages (Issue #77)
#
# These sites can be reached with internal exception/config detail in the
# message (env var names, vendor error text, stack-trace fragments). The
# response body must stay terse and stable; the raw detail is logged
# server-side instead so operators keep it without leaking it to clients.
# ---------------------------------------------------------------------------
_SAFEWAY_PIPELINE_UNAVAILABLE_MESSAGE = "safeway pipeline unavailable"
_CART_BUILD_FAILED_MESSAGE = "cart build failed"
_ORDER_CONFIG_INVALID_MESSAGE = "order configuration invalid"
_ORDER_SUBMISSION_FAILED_MESSAGE = "order submission failed"

#: Terse, stable "error" text for each mapped OrderOutcome status string
#: (see ``_OUTCOME_RESPONSES`` below). Replaces the raw ``error_message``
#: from the OrderResult, which is logged but never relayed verbatim.
_OUTCOME_ERROR_MESSAGES: dict[str, str] = {
    "unknown": (
        "order outcome unknown — do not resubmit; verify recent Safeway orders"
    ),
    "duplicate_prevented": "a recent identical submission is pending or completed",
}


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


def _order_value_cap() -> Decimal:
    """Return the configured Safeway order-value cap (Issue #73).

    Reads ``SAFEWAY_ORDER_VALUE_CAP_USD`` directly via
    :func:`grocery_butler.config.parse_order_value_cap` rather than
    going through :func:`grocery_butler.config.load_config`, so the
    ``/order/submit`` cap gate does not depend on Safeway credentials or
    ``ANTHROPIC_API_KEY`` being configured the way
    :func:`_safeway_pipeline` does — staging (as opposed to confirming)
    an order must work even before the rest of the Safeway integration
    is configured. The staging-time cap read here and the confirm-time
    cap enforced inside :class:`OrderService` (via ``load_config``) use
    the same env var and parser, which assumes the variable stays
    static for the lifetime of a staged action — changing it mid-flight
    could let the two gates diverge.

    Returns:
        The parsed, non-negative cap.

    Raises:
        ConfigError: If ``SAFEWAY_ORDER_VALUE_CAP_USD`` is set to an
            invalid value.
    """
    raw = os.environ.get("SAFEWAY_ORDER_VALUE_CAP_USD", "300")
    return parse_order_value_cap(raw)


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
    items = _pantry_manager().get_inventory()
    return jsonify(items=[_item(i) for i in items])


@api_v1.get("/pantry")
def get_pantry() -> Response:
    """Return all pantry staples."""
    return jsonify(staples=_recipe_store().get_pantry_staples())


@api_v1.get("/recipes")
def list_recipes() -> Response:
    """Return summary rows for all saved recipes."""
    return jsonify(recipes=_recipe_store().list_recipes())


@api_v1.get("/recipes/<int:recipe_id>")
def get_recipe(recipe_id: int) -> Response | tuple[Response, int]:
    """Return one full recipe by id, or a JSON 404 if unknown."""
    meal = _recipe_store().get_recipe_by_id(recipe_id)
    if meal is None:
        return jsonify(error="recipe not found"), 404
    return jsonify(id=recipe_id, **meal.model_dump(mode="json"))


@api_v1.get("/brands")
def list_brands() -> Response:
    """Return all brand preference rules."""
    prefs = _recipe_store().get_brand_preferences()
    return jsonify(brands=[_brand(p) for p in prefs])


@api_v1.get("/preferences")
def get_preferences() -> Response:
    """Return all app-level preferences as a flat object."""
    return jsonify(_recipe_store().get_all_preferences())


@api_v1.get("/restock")
def get_restock() -> Response:
    """Return the restock queue (items low or out)."""
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


@api_v1.post("/meals/parse")
def post_meals_parse() -> Response:
    """Parse free-text meal names into structured ingredient lists."""
    text = str(_json_body().get("text", "")).strip()
    if not text:
        abort(400, description="text required")
    meals = _meal_parser().parse_meals(_split_meal_names(text))
    return jsonify(meals=[meal.model_dump(mode="json") for meal in meals])


@api_v1.post("/shopping-list/preview")
def post_shopping_list_preview() -> Response:
    """Consolidate meals into a shopping list without persisting anything."""
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
    """Build a Safeway cart for review without submitting an order.

    Returns:
        The built cart and its computed total, or a terse 503 JSON error
        if the Safeway pipeline could not be constructed or the cart
        could not be built. The underlying exception detail is logged
        server-side but never relayed to the client (Issue #77).
    """
    shopping_list = _parse_shopping_list_payloads(_json_body().get("shopping_list"))
    try:
        pipeline = _safeway_pipeline()
    except (ConfigError, SafewayPipelineError):
        logger.warning("Safeway pipeline unavailable for order preview", exc_info=True)
        return jsonify(error=_SAFEWAY_PIPELINE_UNAVAILABLE_MESSAGE), 503
    try:
        cart = pipeline.build_cart_only(shopping_list)
    except SafewayPipelineError:
        logger.exception("Cart build failed for order preview")
        return jsonify(error=_CART_BUILD_FAILED_MESSAGE), 503
    finally:
        pipeline.close()
    total = order_service.compute_cart_total(cart)
    return jsonify(cart=cart.model_dump(mode="json"), total=str(total))


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
    return int(servings)


@api_v1.post("/stock/update")
def post_stock_update() -> Response:
    """Set a tracked item's stock status; writes immediately."""
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
    cleared = _pantry_manager().clear_restock_queue()
    return jsonify(ok=True, cleared=cleared)


@api_v1.post("/recipes/save")
def post_recipes_save() -> tuple[Response, int]:
    """Save a new recipe; 409 if the name is already taken."""
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


def _log_resolution(status: str, action: PendingAction) -> None:
    """Log an INFO transition line naming only the action_id and kind.

    Never includes payload contents (issue #75, W2): every audit-trail
    log line is deliberately restricted to non-sensitive metadata.

    Args:
        status: Transition status word (e.g. ``"confirmed"``, ``"denied"``).
        action: The pending action being transitioned.
    """
    LOG.info("%s action_id=%s kind=%s", status, action.action_id, action.kind)


def _sweep_expired(store: PendingActionsStore) -> None:
    """Sweep past-due pending rows to expired, logging the count if any.

    Shared by every route that touches ``pending_actions`` -- the three
    staging routes and the ``GET /actions`` listing (issue #75, W4) --
    so a row can never sit ``pending`` past its TTL just because the
    audit log was never read.

    Args:
        store: The PendingActionsStore to sweep.
    """
    swept = store.sweep_expired()
    if swept > 0:
        LOG.info("swept expired_count=%s", swept)


def _stage_pending(kind: str, payload: dict[str, Any], *, requester: str) -> str:
    """Stage a pending action with the standard TTL; return its action_id.

    Sweeps past-due pending rows to expired first (issue #75, W4) so the
    lazy sweep runs on every write path, not only on ``GET /actions``.
    Logs an INFO audit line naming the action_id and kind (and, for
    order submissions, the staged total) -- never the payload contents
    (issue #75, W1/W2).

    Args:
        kind: Action kind, e.g. ``safeway_order_submit``.
        payload: JSON-serializable action payload.
        requester: The authenticated caller_id staging this action.

    Returns:
        The new pending action's id.
    """
    store = _pending_store()
    _sweep_expired(store)
    action = store.insert_pending_action(
        action_id=str(uuid.uuid4()),
        kind=kind,
        payload=payload,
        expires_at=dt.datetime.now(dt.UTC) + CONFIRMATION_TTL,
        requester=requester,
    )
    if kind == "safeway_order_submit":
        LOG.info(
            "staged action_id=%s kind=%s total=%s",
            action.action_id,
            kind,
            payload.get("total"),
        )
    else:
        LOG.info("staged action_id=%s kind=%s", action.action_id, kind)
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


def _parse_total(raw: Any) -> Decimal:
    """Parse a client-supplied ``total`` value into a Decimal, or abort 400.

    Booleans are rejected explicitly (``isinstance(True, int)`` is
    ``True`` in Python, so a bare numeric-type check would otherwise
    silently accept ``True``/``False``). Any other non-numeric type
    (dict, list, etc.) is rejected too. Numeric and numeric-string
    values are converted via ``Decimal(str(raw))``, which raises
    ``InvalidOperation`` only for an unparseable string (int/float
    always stringify to something Decimal accepts). Non-finite values
    are rejected as well: Python's json parser accepts the non-standard
    ``Infinity``/``NaN`` literals and Decimal accepts ``"Infinity"`` /
    ``"NaN"`` strings, but quantizing a non-finite Decimal raises
    ``InvalidOperation`` downstream (Gate 2.5 review, Issue #73).

    Args:
        raw: The raw ``total`` value from the request body.

    Returns:
        The parsed value as a Decimal.

    Raises:
        werkzeug.exceptions.HTTPException: 400 if ``raw`` is not a
            parseable number.
    """
    if isinstance(raw, bool) or not isinstance(raw, (int, float, str)):
        abort(400, description="total must be a number")
    try:
        value = Decimal(str(raw))
    except InvalidOperation:
        abort(400, description="total must be a number")
    if not value.is_finite():
        abort(400, description="total must be a finite number")
    return value


def _validate_client_total(body: dict[str, Any], server_total: Decimal) -> None:
    """Validate an optional client-supplied total against the server total.

    A missing ``total`` is accepted (the server total is authoritative
    either way); a present ``total`` must parse as a number and match
    the server-computed total to the cent (Issue #73) — a mismatch (or
    an unparseable value) is rejected rather than trusted verbatim.

    Args:
        body: Parsed JSON request body.
        server_total: Fee-inclusive total computed via
            :func:`grocery_butler.order_service.compute_cart_total`.

    Raises:
        werkzeug.exceptions.HTTPException: 400 if ``total`` is present
            but malformed or does not match ``server_total``.
    """
    raw = body.get("total")
    if raw is None:
        return
    client_total = _parse_total(raw).quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)
    if client_total != server_total:
        abort(
            400,
            description=(
                f"total mismatch: client sent {client_total}, "
                f"server computed {server_total}"
            ),
        )


def _enforce_order_cap(server_total: Decimal, override: bool) -> None:
    """Abort 400 if the total exceeds the order-value cap and isn't overridden.

    Args:
        server_total: Fee-inclusive server-computed total.
        override: Whether the caller explicitly opted to bypass the cap
            (``override_cap`` truthy in the request body).

    Raises:
        werkzeug.exceptions.HTTPException: 400 if ``server_total``
            exceeds the configured cap and ``override`` is False.
        ConfigError: If ``SAFEWAY_ORDER_VALUE_CAP_USD`` is set to an
            invalid value — callers must handle this (see
            :func:`post_order_submit`).
    """
    if override:
        return
    cap = _order_value_cap()
    if server_total > cap:
        abort(
            400,
            description=(
                f"order total ${server_total} exceeds the order-value cap "
                f"of ${cap}. Re-submit with override_cap=true to proceed."
            ),
        )


def _render_review_section(cart: CartSummary) -> str:
    """Render a staged-message clause naming flagged items and reasons.

    Delegates flagged-item collection to
    :func:`grocery_butler.order_service._collect_review_items` (single
    source of truth for what counts as flagged) so the human sees every
    item and its review reason before ever replying "confirm" — per the
    chief-architect's ruling, that visibility is what makes a later
    confirm count as an explicit override of the review gate.

    Args:
        cart: The cart being staged for confirmation.

    Returns:
        Empty string for a cart with no flagged items, otherwise a
        sentence naming each flagged ingredient and its reason code.
    """
    flagged = order_service._collect_review_items(cart)
    if not flagged:
        return ""
    named = ", ".join(
        f"{item.shopping_list_item.ingredient} ({item.review_reason})"
        for item in flagged
    )
    return f" Needs review: {named}."


def _render_fulfillment_section(cart: CartSummary) -> str:
    """Render a staged-message clause warning about unverified fulfillment.

    Mirrors :func:`_render_review_section` but for the fulfillment gate
    (Issue #72): when Safeway's fulfillment options could not be
    confirmed while building the cart, the human must see that warning
    before ever replying "confirm" — per the issue #59 precedent, that
    visibility is what makes a later confirm count as an explicit
    override of the fulfillment gate.

    Args:
        cart: The cart being staged for confirmation.

    Returns:
        Empty string for a cart with verified fulfillment, otherwise a
        sentence warning that fulfillment could not be confirmed.
    """
    if not cart.fulfillment_unverified:
        return ""
    return (
        " Fulfillment options could not be confirmed with Safeway "
        "(unverified) — pickup/delivery availability and fees are not "
        "final."
    )


@api_v1.post("/order/submit")
def post_order_submit() -> Response | tuple[Response, int]:
    """Stage a Safeway order for confirmation. Never submits directly.

    The total is always the server-computed, fee-inclusive
    :func:`~grocery_butler.order_service.compute_cart_total` — an
    optional client-supplied ``total`` is validated against it (Issue
    #73) rather than trusted verbatim. Orders whose total exceeds the
    configured order-value cap are rejected unless the request sets
    ``override_cap`` truthy, in which case the override is persisted in
    the staged payload for :func:`_confirm_order_submit` to forward.

    Authentication is enforced blueprint-wide by the
    :func:`_enforce_bearer_auth` ``before_request`` hook (Issue #74), so
    this route no longer calls ``require_bearer()`` itself; the caller
    identity stamped as requester (issue #75 W1) comes from the hook via
    :func:`_request_caller_id`.

    Returns:
        The staged-action JSON response, or a 503 JSON error if the
        order-value cap configuration itself is invalid.
    """
    caller_id = _request_caller_id()
    body = _json_body()
    cart = _parse_cart_payload(body)
    server_total = order_service.compute_cart_total(cart)
    _validate_client_total(body, server_total)
    override_cap = bool(body.get("override_cap"))
    try:
        _enforce_order_cap(server_total, override_cap)
    except ConfigError:
        logger.exception("Order-value cap configuration invalid")
        return jsonify(error=_ORDER_CONFIG_INVALID_MESSAGE), 503
    total = str(server_total)
    action_id = _stage_pending(
        "safeway_order_submit",
        {
            "cart": cart.model_dump(mode="json"),
            "total": total,
            "allow_over_cap": override_cap,
        },
        requester=caller_id,
    )
    item_count = len(cart.items) + len(cart.restock_items)
    review_section = _render_review_section(cart)
    fulfillment_section = _render_fulfillment_section(cart)
    return jsonify(
        status="pending_confirmation",
        action_id=action_id,
        message=(
            f"Staged Safeway order ({item_count} items, ${total})."
            f"{review_section}{fulfillment_section} Reply 'confirm' to submit."
        ),
    )


@api_v1.post("/brands/set")
def post_brands_set() -> Response:
    """Stage a brand preference rule for confirmation."""
    caller_id = _request_caller_id()
    body = _json_body()
    try:
        pref = BrandPreference.model_validate(body)
    except ValidationError as exc:
        abort(400, description=f"invalid brand rule: {exc.error_count()} error(s)")
    action_id = _stage_pending(
        "brands_set", pref.model_dump(mode="json"), requester=caller_id
    )
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
    caller_id = _request_caller_id()
    preferences = _parse_preferences_payload(_json_body())
    action_id = _stage_pending(
        "preferences_set", {"preferences": preferences}, requester=caller_id
    )
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


def _claim_pending(
    store: PendingActionsStore, action_id: str, *, resolver: str
) -> None:
    """Atomically claim a pending action as approved, or abort 409.

    The guarded UPDATE in the store is the exactly-once gate: only one
    caller can transition the row out of ``pending``, so a raced double
    confirm cannot execute twice.

    Args:
        store: Store used to attempt the claim.
        action_id: Identifier of the action to claim.
        resolver: The confirming caller_id, stamped as the resolver
            (issue #75, W1) -- distinct from the requester who staged
            the action.
    """
    if not store.mark_pending_approved(action_id, resolver=resolver):
        abort(409, description="action already resolved")


def _approved(action: PendingAction, result: dict[str, Any]) -> Response:
    """Serialize a successful confirmation response."""
    return jsonify(
        status="approved",
        action_id=action.action_id,
        kind=action.kind,
        result=result,
    )


#: Maps a non-success OrderOutcome to its HTTP status and response status
#: string. Outcomes not listed here (FAILED) fall back to the existing
#: 502 "order submission failed" response in ``_order_failure_response``.
_OUTCOME_RESPONSES: dict[OrderOutcome, tuple[str, int]] = {
    OrderOutcome.UNKNOWN: ("unknown", 504),
    OrderOutcome.DUPLICATE: ("duplicate_prevented", 409),
}


def _order_failure_response(
    action: PendingAction, error_message: str, outcome: OrderOutcome | None
) -> tuple[Response, int]:
    """Render the appropriate error response for a failed order submission.

    The raw ``error_message`` surfaced from deep inside the Safeway
    client/pipeline is logged server-side for operators but never
    relayed to the client verbatim -- it can carry internal detail
    (vendor error text, stack-trace fragments) that must not leak into
    the RubotPaul-facing response body (Issue #77). The response always
    keeps a stable ``error`` key, and mapped outcomes additionally keep
    their ``status``/``action_id`` keys so RubotPaul can still route the
    reply.

    Args:
        action: The staged (already-claimed) pending action.
        error_message: Error message from the OrderResult. Logged, but
            never relayed verbatim in the response body.
        outcome: The order's OrderOutcome, if known.

    Returns:
        A (Response, status_code) tuple: 504 for an unknown (timed-out)
        outcome, 409 for a blocked duplicate, or 502 for any other
        failure. The ``error`` field is always a terse, stable message.
    """
    mapped = _OUTCOME_RESPONSES.get(outcome) if outcome is not None else None
    if mapped is None:
        logger.error(
            "Order submission failed for action %s: %s",
            action.action_id,
            error_message,
        )
        return jsonify(error=_ORDER_SUBMISSION_FAILED_MESSAGE), 502
    status, code = mapped
    logger.warning(
        "Order submission failed (%s) for action %s: %s",
        status,
        action.action_id,
        error_message,
    )
    terse_message = _OUTCOME_ERROR_MESSAGES[status]
    response = jsonify(status=status, action_id=action.action_id, error=terse_message)
    return response, code


def _mark_order_failed(store: PendingActionsStore, action: PendingAction) -> None:
    """Resolve a claimed order action as failed and log the transition.

    Args:
        store: Store used to resolve the action.
        action: The claimed (approved) pending action.
    """
    store.mark_pending_failed(action.action_id)
    _log_resolution("failed", action)


def _submit_claimed_order(
    action: PendingAction,
    store: PendingActionsStore,
    pipeline: SafewayPipeline,
    cart: CartSummary,
) -> Response | tuple[Response, int]:
    """Submit an already-claimed order's cart and resolve its outcome.

    Any post-claim failure -- a raised ``SafewayPipelineError`` or a
    ``result.success is False`` outcome -- resolves the row as
    ``failed`` (issue #75, W3), so a claimed action is never left
    stuck ``approved`` with no record that execution actually failed.

    Forwards ``action.payload["allow_over_cap"]`` (set at staging time
    by ``post_order_submit`` when the human passed ``override_cap``) to
    :meth:`SafewayPipeline.submit_cart` so the Issue #73 order-value cap
    gate can be bypassed exactly when it was already approved at
    staging time.

    Args:
        action: The staged (already-claimed) pending action.
        store: Store used to resolve the action's final status.
        pipeline: An already-built, enabled Safeway pipeline.
        cart: The exact staged cart to submit.

    Returns:
        The success or failure response for this confirmation.
    """
    try:
        # The staged message (post_order_submit) already named every
        # flagged item and its reason code, so replying "confirm" IS the
        # human's explicit override of the review gate (chief-architect
        # ruling, issue #59 round 3). The staged message also already
        # warned when fulfillment could not be confirmed with Safeway, so
        # this same "confirm" is likewise the explicit human override of
        # the fulfillment gate (issue #59 precedent, issue #72). The
        # action_id doubles as the idempotency key so the duplicate-order
        # ledger and Safeway's clientOrderId correlate with the staged
        # action (Issue #61).
        result = pipeline.submit_cart(
            cart,
            idempotency_key=action.action_id,
            allow_review_items=True,
            allow_unverified_fulfillment=True,
            allow_over_cap=bool(action.payload.get("allow_over_cap", False)),
        )
    except SafewayPipelineError:
        # The raised detail is logged for operators (Issue #77) but the
        # client only ever sees the terse, stable message; the claimed
        # row is still resolved as failed (issue #75, W3). The pipeline
        # is closed by _confirm_order_submit's try/finally (W6).
        logger.exception("Order submission failed for action %s", action.action_id)
        _mark_order_failed(store, action)
        return jsonify(error=_ORDER_SUBMISSION_FAILED_MESSAGE), 502
    if not result.success:
        _mark_order_failed(store, action)
        return _order_failure_response(action, result.error_message, result.outcome)
    _log_resolution("confirmed", action)
    confirmation = (
        dataclasses.asdict(result.confirmation) if result.confirmation else {}
    )
    return _approved(
        action, {**confirmation, "items_restocked": result.items_restocked}
    )


def _confirm_order_submit(
    action: PendingAction, store: PendingActionsStore, resolver: str
) -> Response | tuple[Response, int]:
    """Submit the exact staged cart to Safeway (real money)."""
    cart = CartSummary.model_validate(action.payload["cart"])
    try:
        pipeline = _safeway_pipeline()
    except (ConfigError, SafewayPipelineError):
        # Not claimed yet: the action stays pending so it can be retried
        # (until it expires) once configuration is fixed. The detail is
        # logged for operators but never relayed to the client (Issue #77).
        logger.exception("Safeway pipeline unavailable while confirming order")
        return jsonify(error=_SAFEWAY_PIPELINE_UNAVAILABLE_MESSAGE), 503
    if not pipeline.order_submission_enabled:
        # Issue #60: order submission is descoped for v1.0. Not claimed —
        # the action stays pending so it can be retried once the flag is
        # flipped on after live verification.
        pipeline.close()
        return jsonify(error=ORDER_SUBMISSION_DISABLED_MESSAGE), 501
    # W6 (issue #75): the claim happens *inside* this try/finally so a
    # raced claim that aborts 409 still closes the pipeline already built
    # for this request — a bare pre-claim abort would leak it otherwise.
    try:
        _claim_pending(store, action.action_id, resolver=resolver)
        return _submit_claimed_order(action, store, pipeline, cart)
    finally:
        pipeline.close()


def _confirm_brands_set(
    action: PendingAction, store: PendingActionsStore, resolver: str
) -> Response:
    """Apply the staged brand preference rule."""
    pref = BrandPreference.model_validate(action.payload)
    _claim_pending(store, action.action_id, resolver=resolver)
    pref_id = _recipe_store().add_brand_preference(pref)
    _log_resolution("confirmed", action)
    return _approved(action, {**pref.model_dump(mode="json"), "id": pref_id})


def _confirm_preferences_set(
    action: PendingAction, store: PendingActionsStore, resolver: str
) -> Response:
    """Apply the staged app-level preference changes atomically.

    Uses :meth:`RecipeStore.set_preferences` -- one connection, one
    commit for every staged key -- so a mid-way failure leaves the
    store completely untouched instead of half-applied (issue #75, W5).
    """
    preferences: dict[str, str] = action.payload["preferences"]
    _claim_pending(store, action.action_id, resolver=resolver)
    updated = _recipe_store().set_preferences(preferences)
    _log_resolution("confirmed", action)
    return _approved(action, {"updated": updated})


_CONFIRM_EXECUTORS: dict[
    str,
    Callable[
        [PendingAction, PendingActionsStore, str], Response | tuple[Response, int]
    ],
] = {
    "safeway_order_submit": _confirm_order_submit,
    "brands_set": _confirm_brands_set,
    "preferences_set": _confirm_preferences_set,
}


@api_v1.post("/actions/confirm")
def post_actions_confirm() -> Response | tuple[Response, int]:
    """Execute a staged action exactly once after chat confirmation."""
    caller_id = _request_caller_id()
    action = _load_pending_action(_json_body())
    store = _pending_store()
    if action.status is not PendingActionStatus.PENDING:
        abort(409, description="action already resolved")
    if action.is_expired():
        store.mark_pending_expired(action.action_id)
        _log_resolution("expired", action)
        abort(410, description="action expired — stage it again")
    executor = _CONFIRM_EXECUTORS.get(action.kind)
    if executor is None:
        abort(400, description=f"unknown action kind: {action.kind}")
    return executor(action, store, caller_id)


@api_v1.post("/actions/deny")
def post_actions_deny() -> Response:
    """Mark a staged action as denied (audit trail; nothing executes).

    Denial is safe regardless of expiry, so unlike confirm this does not
    check the TTL — a denied-after-expiry action records the user's
    explicit "no" rather than a timeout.
    """
    resolver = _request_caller_id()
    action = _load_pending_action(_json_body())
    if not _pending_store().mark_pending_denied(action.action_id, resolver=resolver):
        abort(409, description="action already resolved")
    _log_resolution("denied", action)
    return jsonify(status="denied", action_id=action.action_id, kind=action.kind)


# ---------------------------------------------------------------------------
# GET /actions and /actions/<action_id> — read access to the audit log (W4)
# ---------------------------------------------------------------------------

#: Default and hard-cap page sizes for GET /actions (W4, issue #75).
_DEFAULT_ACTIONS_LIMIT = 50
_MAX_ACTIONS_LIMIT = 200


def _pending_action_json(action: PendingAction) -> dict[str, Any]:
    """Serialize a PendingAction to a JSON-safe dict."""
    return action.model_dump(mode="json")


def _parse_actions_query() -> tuple[int, PendingActionStatus | None]:
    """Parse and validate the ``?limit`` and ``?status`` query params.

    Returns:
        Tuple of (clamped limit, optional status filter). An omitted or
        over-cap ``?limit`` is clamped into ``[1, _MAX_ACTIONS_LIMIT]``
        rather than rejected.

    Raises:
        HTTPException: 400 if ``limit`` is not an integer, or if
            ``status`` is present but not a recognized
            :class:`PendingActionStatus` value.
    """
    raw_limit = request.args.get("limit", str(_DEFAULT_ACTIONS_LIMIT))
    try:
        limit = int(raw_limit)
    except ValueError:
        abort(400, description="limit must be an integer")
    limit = max(1, min(limit, _MAX_ACTIONS_LIMIT))

    raw_status = request.args.get("status")
    if raw_status is None:
        return limit, None
    try:
        status = PendingActionStatus(raw_status)
    except ValueError:
        abort(400, description="unknown status value")
    return limit, status


@api_v1.get("/actions")
def get_actions() -> Response:
    """List staged/resolved actions, sweeping expired rows first (W4).

    Reading the audit log first sweeps any past-due pending rows to
    ``expired`` so the returned statuses are current, not stale.
    Authentication is enforced by the blueprint's
    :func:`_enforce_bearer_auth` hook (Issue #74).
    """
    limit, status = _parse_actions_query()
    store = _pending_store()
    _sweep_expired(store)
    actions = store.list_pending_actions(limit=limit, status=status)
    return jsonify(
        actions=[_pending_action_json(a) for a in actions], count=len(actions)
    )


@api_v1.get("/actions/<action_id>")
def get_action(action_id: str) -> Response | tuple[Response, int]:
    """Return one staged/resolved action by id, or a JSON 404 (W4).

    Authentication is enforced by the blueprint's
    :func:`_enforce_bearer_auth` hook (Issue #74).
    """
    action = _pending_store().get_pending_action(action_id)
    if action is None:
        return jsonify(error="unknown action_id"), 404
    return jsonify(_pending_action_json(action))
