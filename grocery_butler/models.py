"""Pydantic models and enums for MealBot.

This is the shared type system. ALL data structures are defined here,
including future Safeway models that aren't used yet.
"""

from __future__ import annotations

import datetime as dt
import logging
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator

logger = logging.getLogger(__name__)

# ---------------------------------------------------------------------------
# Enums
# ---------------------------------------------------------------------------


class IngredientCategory(StrEnum):
    """Grocery store aisle categories."""

    PRODUCE = "produce"
    MEAT = "meat"
    DAIRY = "dairy"
    BAKERY = "bakery"
    PANTRY_DRY = "pantry_dry"
    FROZEN = "frozen"
    BEVERAGES = "beverages"
    DELI = "deli"
    OTHER = "other"


class InventoryStatus(StrEnum):
    """Household inventory status lifecycle."""

    ON_HAND = "on_hand"
    LOW = "low"
    OUT = "out"


class BrandPreferenceType(StrEnum):
    """Whether a brand is preferred or avoided."""

    PREFERRED = "preferred"
    AVOID = "avoid"


class BrandMatchType(StrEnum):
    """Whether a brand preference targets a category or specific ingredient."""

    CATEGORY = "category"
    INGREDIENT = "ingredient"


class Unit(StrEnum):
    """Standardized grocery measurement units."""

    # Volume
    TSP = "tsp"
    TBSP = "tbsp"
    CUP = "cup"
    FL_OZ = "fl_oz"
    ML = "ml"
    L = "l"
    GAL = "gal"
    PINT = "pint"
    QUART = "quart"

    # Weight
    OZ = "oz"
    LB = "lb"
    G = "g"
    KG = "kg"

    # Count
    EACH = "each"
    DOZEN = "dozen"
    BUNCH = "bunch"
    HEAD = "head"
    CLOVE = "clove"
    SLICE = "slice"

    # Packaging
    CAN = "can"
    BAG = "bag"
    BOX = "box"
    JAR = "jar"
    BOTTLE = "bottle"
    PACKAGE = "package"
    BLOCK = "block"
    STICK = "stick"

    # Other
    PINCH = "pinch"
    DASH = "dash"
    TO_TASTE = "to_taste"


_UNIT_ALIASES: dict[str, Unit] = {
    # Weight plurals/variations
    "lbs": Unit.LB,
    "pound": Unit.LB,
    "pounds": Unit.LB,
    "ounce": Unit.OZ,
    "ounces": Unit.OZ,
    "gram": Unit.G,
    "grams": Unit.G,
    "kilogram": Unit.KG,
    "kilograms": Unit.KG,
    # Volume plurals/variations
    "teaspoon": Unit.TSP,
    "teaspoons": Unit.TSP,
    "tablespoon": Unit.TBSP,
    "tablespoons": Unit.TBSP,
    "cups": Unit.CUP,
    "fluid_ounce": Unit.FL_OZ,
    "fluid ounce": Unit.FL_OZ,
    "fluid_oz": Unit.FL_OZ,
    "milliliter": Unit.ML,
    "milliliters": Unit.ML,
    "liter": Unit.L,
    "liters": Unit.L,
    "gallon": Unit.GAL,
    "gallons": Unit.GAL,
    "pints": Unit.PINT,
    "pt": Unit.PINT,
    "quarts": Unit.QUART,
    "qt": Unit.QUART,
    # Count plurals
    "piece": Unit.EACH,
    "pieces": Unit.EACH,
    "cloves": Unit.CLOVE,
    "heads": Unit.HEAD,
    "bunches": Unit.BUNCH,
    "slices": Unit.SLICE,
    # Packaging plurals
    "cans": Unit.CAN,
    "bags": Unit.BAG,
    "boxes": Unit.BOX,
    "jars": Unit.JAR,
    "bottles": Unit.BOTTLE,
    "packages": Unit.PACKAGE,
    "pkg": Unit.PACKAGE,
    "blocks": Unit.BLOCK,
    "sticks": Unit.STICK,
}


def parse_unit(raw: str) -> Unit:
    """Parse a raw unit string into a Unit enum member.

    Handles exact matches, aliases, and case-insensitive lookup.
    Falls back to Unit.EACH for unrecognized strings, emitting a WARNING
    log with the raw token so silent unit rewrites are detectable
    (issue #69). Empty or blank input also falls back to Unit.EACH but
    stays silent, since a missing unit is a legitimate default.

    Args:
        raw: Raw unit string from LLM output, database, or user input.

    Returns:
        Matching Unit enum member.
    """
    if not raw or not raw.strip():
        return Unit.EACH
    cleaned = raw.strip().lower()
    try:
        return Unit(cleaned)
    except ValueError:
        pass
    result = _UNIT_ALIASES.get(cleaned)
    if result is not None:
        return result
    logger.warning("parse_unit: unknown unit %r; falling back to Unit.EACH", raw)
    return Unit.EACH


def coerce_category(raw: object) -> IngredientCategory:
    """Coerce a raw value into an IngredientCategory, defaulting to OTHER.

    Handles exact matches (case-insensitive), existing enum members, and
    ``None``/empty values. Unrecognized strings degrade to
    ``IngredientCategory.OTHER`` with a warning logged; ``None`` and empty
    strings degrade silently since they represent "no data" rather than
    "bad data".

    Args:
        raw: Raw category value from LLM output, database, or user input.

    Returns:
        Matching IngredientCategory enum member, or OTHER if unrecognized.
    """
    if isinstance(raw, IngredientCategory):
        return raw
    text = "" if raw is None else str(raw).strip().lower()
    if not text:
        return IngredientCategory.OTHER
    try:
        return IngredientCategory(text)
    except ValueError:
        logger.warning("Unknown ingredient category %r; using OTHER", raw)
        return IngredientCategory.OTHER


def coerce_category_optional(raw: object) -> IngredientCategory | None:
    """Coerce a raw value into an IngredientCategory, preserving None.

    Args:
        raw: Raw category value, or None.

    Returns:
        Matching IngredientCategory enum member, None if input was None,
        or OTHER if the input was a non-None, unrecognized value.
    """
    if raw is None:
        return None
    return coerce_category(raw)


def _coerce_unit(v: object) -> Unit:
    """Coerce a raw value to a Unit enum member.

    Args:
        v: Raw value (string, Unit, or other).

    Returns:
        Normalized Unit enum member.
    """
    if isinstance(v, Unit):
        return v
    return parse_unit(str(v))


def _coerce_unit_optional(v: object) -> Unit | None:
    """Coerce a raw value to a Unit enum member, allowing None.

    Args:
        v: Raw value (string, Unit, None, or other).

    Returns:
        Normalized Unit enum member, or None if input is None.
    """
    if v is None:
        return None
    return _coerce_unit(v)


class PriceSensitivity(StrEnum):
    """User's price sensitivity for product selection."""

    BUDGET = "budget"
    MODERATE = "moderate"
    PREMIUM = "premium"


class OrganicPreference(StrEnum):
    """User's organic preference."""

    YES = "yes"
    NO = "no"
    WHEN_REASONABLE = "when_reasonable"


class FulfillmentType(StrEnum):
    """Safeway order fulfillment type."""

    PICKUP = "pickup"
    DELIVERY = "delivery"


class SubstitutionSuitability(StrEnum):
    """How suitable a substitution is for the original item."""

    EXCELLENT = "excellent"
    GOOD = "good"
    ACCEPTABLE = "acceptable"
    POOR = "poor"


# ---------------------------------------------------------------------------
# Core models (used now)
# ---------------------------------------------------------------------------


class Ingredient(BaseModel):
    """A single ingredient with quantity and category."""

    ingredient: str
    quantity: float
    unit: Unit
    category: IngredientCategory
    notes: str = ""
    is_pantry_item: bool = False

    @field_validator("unit", mode="before")
    @classmethod
    def _normalize_unit(cls, v: object) -> Unit:
        """Normalize raw unit strings to Unit enum members.

        Args:
            v: Raw value (string, Unit, or other).

        Returns:
            Normalized Unit enum member.
        """
        return _coerce_unit(v)


class ParsedMeal(BaseModel):
    """A meal decomposed into its ingredient lists."""

    name: str
    servings: int
    known_recipe: bool
    needs_confirmation: bool
    purchase_items: list[Ingredient]
    pantry_items: list[Ingredient]


class ShoppingListItem(BaseModel):
    """A single item on the consolidated shopping list."""

    ingredient: str
    quantity: float
    unit: Unit
    category: IngredientCategory
    search_term: str
    from_meals: list[str]
    estimated_price: float | None = None

    @field_validator("unit", mode="before")
    @classmethod
    def _normalize_unit(cls, v: object) -> Unit:
        """Normalize raw unit strings to Unit enum members.

        Args:
            v: Raw value (string, Unit, or other).

        Returns:
            Normalized Unit enum member.
        """
        return _coerce_unit(v)


class InventoryItem(BaseModel):
    """A tracked household inventory item."""

    ingredient: str
    display_name: str
    category: IngredientCategory | None = None
    status: InventoryStatus = InventoryStatus.ON_HAND
    current_quantity: float | None = None
    current_unit: str | None = None
    default_quantity: float | None = None
    default_unit: Unit | None = None
    default_search_term: str | None = None
    notes: str = ""

    @field_validator("default_unit", mode="before")
    @classmethod
    def _normalize_default_unit(cls, v: object) -> Unit | None:
        """Normalize raw unit strings to Unit enum members.

        Args:
            v: Raw value (string, Unit, None, or other).

        Returns:
            Normalized Unit enum member, or None.
        """
        return _coerce_unit_optional(v)


class InventoryUpdate(BaseModel):
    """An inventory status change parsed from natural language."""

    ingredient: str
    new_status: InventoryStatus
    confidence: float


class BrandPreference(BaseModel):
    """A brand preference rule (preferred or avoided).

    The ``id`` field is the database row id. It is ``None`` for models
    built in memory and is populated only when read back from the store.
    """

    match_target: str
    match_type: BrandMatchType
    brand: str
    preference_type: BrandPreferenceType
    notes: str = ""
    id: int | None = None


# ---------------------------------------------------------------------------
# Future Safeway models (define now, use in Phase 3)
# ---------------------------------------------------------------------------


class SafewayProduct(BaseModel):
    """A product from Safeway's catalog."""

    product_id: str
    name: str
    price: float
    unit_price: float | None = None
    size: str
    in_stock: bool = True


class SubstitutionOption(BaseModel):
    """A potential substitution for an out-of-stock item."""

    product: SafewayProduct
    suitability: SubstitutionSuitability
    form_warning: str | None = None
    reasoning: str


class SubstitutionResult(BaseModel):
    """The outcome of a substitution flow for one item."""

    status: str
    original_item: ShoppingListItem
    alternatives: list[SubstitutionOption] = []
    selected: SubstitutionOption | None = None
    message: str = ""


class CartItem(BaseModel):
    """A shopping list item mapped to a Safeway product.

    Attributes:
        shopping_list_item: The originating shopping list item.
        safeway_product: The matched Safeway product.
        quantity_to_order: Number of product units to order.
        estimated_cost: Estimated cost for ``quantity_to_order`` units.
            Must be finite — JSON's non-standard ``Infinity``/``NaN``
            literals are rejected at validation (issue #73), keeping
            non-finite values out of server-side total computation.
        needs_review: Whether the item needs human review (e.g. an
            unparseable product size, incomparable units, a quantity
            capped by the per-item maximum, or an auto-selected
            substitution).
        review_reason: Machine-readable reason code for ``needs_review``
            (``"unparseable_size"``, ``"incomparable_units"``,
            ``"quantity_capped"``, or ``"substitution"``), or ``""``
            when no review is needed.
    """

    shopping_list_item: ShoppingListItem
    safeway_product: SafewayProduct
    quantity_to_order: int
    estimated_cost: float = Field(allow_inf_nan=False)
    needs_review: bool = False
    review_reason: str = ""


class FulfillmentOption(BaseModel):
    """A fulfillment option (pickup or delivery) with scheduling.

    The ``fee`` must be finite — JSON's non-standard ``Infinity``/``NaN``
    literals are rejected at validation (issue #73), keeping non-finite
    values out of server-side total computation.
    """

    type: FulfillmentType
    available: bool
    fee: float = Field(allow_inf_nan=False)
    windows: list[dict[str, Any]]
    next_window: str | None = None


class CartSummary(BaseModel):
    """Complete cart ready for order submission.

    Attributes:
        items: Regular cart items.
        failed_items: Shopping list items no product could be found for.
        substituted_items: Out-of-stock items with substitution results.
        skipped_items: Items explicitly skipped by the caller.
        restock_items: Restock-queue cart items.
        subtotal: Sum of all item costs before fulfillment fees. Must
            be finite (issue #73).
        fulfillment_options: Fulfillment options fetched from Safeway, or
            an empty list when the fetch failed (see
            ``fulfillment_unverified``).
        recommended_fulfillment: The recommended fulfillment type.
        estimated_total: Subtotal plus the recommended fulfillment fee.
            Must be finite (issue #73).
        fulfillment_unverified: True when Safeway's fulfillment options
            could not be fetched, so ``fulfillment_options`` is empty and
            ``estimated_total`` excludes any fulfillment fee (issue #72).
            Callers must warn the human and require an explicit override
            before submitting an order built from such a cart -- never
            treat the absence of a fee as confirmation that fulfillment
            is free. Defaults to False (verified).
    """

    items: list[CartItem]
    failed_items: list[ShoppingListItem]
    substituted_items: list[SubstitutionResult]
    skipped_items: list[ShoppingListItem] = []
    restock_items: list[CartItem]
    subtotal: float = Field(allow_inf_nan=False)
    fulfillment_options: list[FulfillmentOption]
    recommended_fulfillment: FulfillmentType
    estimated_total: float = Field(allow_inf_nan=False)
    fulfillment_unverified: bool = False


class PendingActionStatus(StrEnum):
    """Lifecycle status of a staged pending action."""

    PENDING = "pending"
    APPROVED = "approved"
    DENIED = "denied"
    EXPIRED = "expired"
    FAILED = "failed"


class PendingAction(BaseModel):
    """A destructive action staged for confirmation (audit-log row).

    Rows live in the ``pending_actions`` table and record every staged
    Safeway submission or preference change, whether it was ultimately
    approved, denied, expired, or (after being claimed) failed.

    Attributes:
        requester: The caller_id that staged this action.
        resolver: The caller_id that resolved this action (approved or
            denied it), or None for a system-initiated resolution such
            as TTL expiry, which has no resolving caller.
    """

    action_id: str
    kind: str
    payload: dict[str, Any]
    status: PendingActionStatus = PendingActionStatus.PENDING
    requester: str = "rubotpaul"
    resolver: str | None = None
    expires_at: dt.datetime
    created_at: dt.datetime | None = None
    resolved_at: dt.datetime | None = None

    def is_expired(self, now: dt.datetime | None = None) -> bool:
        """Check whether the confirmation deadline has passed.

        Naive datetimes (as stored by SQLite) are interpreted as UTC.

        Args:
            now: Reference time; defaults to the current UTC time.

        Returns:
            True if ``expires_at`` is earlier than ``now``.
        """
        reference = now if now is not None else dt.datetime.now(dt.UTC)
        expires = self.expires_at
        if expires.tzinfo is None:
            expires = expires.replace(tzinfo=dt.UTC)
        if reference.tzinfo is None:
            reference = reference.replace(tzinfo=dt.UTC)
        return reference > expires
