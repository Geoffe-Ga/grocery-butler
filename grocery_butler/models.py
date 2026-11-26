"""Pydantic models and enums for MealBot.

This is the shared type system. ALL data structures are defined here.
Core models stay retailer-neutral (ADR 0001): no retailer-specific
product, cart, or fulfillment type belongs in this module.
"""

from __future__ import annotations

import datetime as dt
import logging
from contextlib import suppress
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, Field, field_validator, model_validator

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


class StaplePolicy(StrEnum):
    """How the household wants an item replenished (backlog P1.7)."""

    ALWAYS_KEEP = "always_keep"
    REPLENISH_WHEN_LOW = "replenish_when_low"
    MEALS_ONLY = "meals_only"
    NEVER_SUGGEST = "never_suggest"


class ItemEventType(StrEnum):
    """A kind of change in a household item's audit trail (ADR 0006)."""

    CREATED = "created"
    UPDATED = "updated"
    ALIAS_ADDED = "alias_added"
    ALIAS_REMOVED = "alias_removed"
    MERGED = "merged"
    UNMERGED = "unmerged"


class StockState(StrEnum):
    """What the household believes about an item's stock (ADR 0007).

    ``UNKNOWN`` is never observed. It is what a snapshot reports when there
    is no valid observation, or the latest one has gone stale.
    """

    ON_HAND = "on_hand"
    LOW = "low"
    OUT = "out"
    UNKNOWN = "unknown"


class ObservationSource(StrEnum):
    """Where an inventory observation came from."""

    HUMAN = "human"
    PHOTO = "photo"
    RECEIPT = "receipt"
    ORDER = "order"
    SYSTEM = "system"


class ObservationKind(StrEnum):
    """Whether a ledger row states something or only withdraws a row."""

    OBSERVATION = "observation"
    RETRACTION = "retraction"


class NeedStatus(StrEnum):
    """Where a replenishment need stands (ADR 0008)."""

    SUGGESTED = "suggested"
    APPROVED = "approved"
    DEFERRED = "deferred"
    DISMISSED = "dismissed"
    PLANNED = "planned"
    ORDERED = "ordered"
    RECEIVED = "received"
    CANCELLED = "cancelled"


class NeedReasonKind(StrEnum):
    """Why an item is wanted."""

    LOW_STOCK = "low_stock"
    OUT = "out"
    RECURRING_STAPLE = "recurring_staple"
    RECIPE_DEMAND = "recipe_demand"
    MANUAL_REQUEST = "manual_request"


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
    with suppress(ValueError):
        return Unit(cleaned)
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
    item_id: int | None = None

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
    brand rule or preference change, whether it was ultimately
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


# ---------------------------------------------------------------------------
# Canonical household items (ADR 0006)
# ---------------------------------------------------------------------------


class HouseholdItem(BaseModel):
    """One thing the household keeps, with a stable identity.

    ``name`` is the normalized, unique key. ``merged_into_id`` is set when
    this item was merged into another; readers then use that item instead.
    """

    id: int
    name: str
    display_name: str
    category: IngredientCategory | None = None
    preferred_unit: str | None = None
    staple_policy: StaplePolicy | None = None
    preferred_brand: str | None = None
    preferred_size: str | None = None
    active: bool = True
    merged_into_id: int | None = None


class HouseholdItemUpdate(BaseModel):
    """Fields of a household item a caller may change.

    Only fields that were explicitly set are applied, so ``None`` clears a
    field and an omitted field is left alone.
    """

    display_name: str | None = None
    category: IngredientCategory | None = None
    preferred_unit: str | None = None
    staple_policy: StaplePolicy | None = None
    preferred_brand: str | None = None
    preferred_size: str | None = None


class ItemEvent(BaseModel):
    """One entry in a household item's append-only audit trail."""

    id: int
    item_id: int
    event_type: ItemEventType
    detail: dict[str, Any]
    actor: str
    created_at: str


# ---------------------------------------------------------------------------
# Inventory observation ledger (ADR 0007)
# ---------------------------------------------------------------------------

# Sources allowed to state a quantity. A model never estimates one (ADR 0003).
QUANTITY_SOURCES = frozenset(
    {ObservationSource.HUMAN, ObservationSource.RECEIPT, ObservationSource.ORDER}
)


class NewObservation(BaseModel):
    """An inventory observation about to be appended to the ledger.

    ``state`` must be a stated state, never ``UNKNOWN``. ``confidence``
    defaults to 1 for a human statement and is required for a photo.
    ``observed_at`` defaults to the time of recording.
    """

    item_id: int
    state: StockState
    source: ObservationSource
    actor: str = Field(min_length=1)
    quantity: float | None = Field(default=None, ge=0)
    unit: str | None = None
    confidence: float | None = Field(default=None, ge=0, le=1)
    observed_at: dt.datetime | None = None
    evidence_ref: str | None = None
    notes: str = ""

    @field_validator("state")
    @classmethod
    def _state_is_stated(cls, value: StockState) -> StockState:
        """Refuse ``UNKNOWN``: it is derived, never observed.

        Args:
            value: The stated state.

        Returns:
            The state unchanged.

        Raises:
            ValueError: If the state is ``UNKNOWN``.
        """
        if value is StockState.UNKNOWN:
            msg = "An observation states on_hand, low or out; unknown is derived."
            raise ValueError(msg)
        return value

    @field_validator("observed_at")
    @classmethod
    def _observed_at_is_aware(cls, value: dt.datetime | None) -> dt.datetime | None:
        """Refuse a time without a zone, which can't be ordered safely.

        Args:
            value: When the observation was made, if given.

        Returns:
            The time unchanged.

        Raises:
            ValueError: If the time is naive.
        """
        if value is not None and value.tzinfo is None:
            msg = "observed_at must carry a time zone."
            raise ValueError(msg)
        return value

    @model_validator(mode="after")
    def _quantity_rules(self) -> NewObservation:
        """Apply ADR 0003: who may state a quantity, and that it has a unit.

        Returns:
            The observation unchanged.

        Raises:
            ValueError: If the source can't state a quantity, or a quantity
                and its unit are not given together.
        """
        if self.quantity is not None and self.source not in QUANTITY_SOURCES:
            msg = f"A {self.source.value} observation can't state a quantity."
            raise ValueError(msg)
        if (self.quantity is None) != (self.unit is None):
            msg = "A quantity and its unit must be given together."
            raise ValueError(msg)
        return self

    @model_validator(mode="after")
    def _confidence_rules(self) -> NewObservation:
        """Require a photo's confidence and default a human's to 1.

        Returns:
            The observation, with a human's confidence defaulted.

        Raises:
            ValueError: If a photo observation states no confidence.
        """
        if self.confidence is None and self.source is ObservationSource.PHOTO:
            msg = "A photo observation must state its confidence."
            raise ValueError(msg)
        if self.confidence is None and self.source is ObservationSource.HUMAN:
            self.confidence = 1.0
        return self


class InventoryObservation(BaseModel):
    """One row of the append-only observation ledger.

    ``superseded_by_id`` is derived: it names the later row that corrected
    or retracted this one, if any. A retraction has no ``state``.
    """

    id: int
    item_id: int
    kind: ObservationKind
    state: StockState | None
    quantity: float | None
    unit: str | None
    source: ObservationSource
    confidence: float | None
    actor: str
    observed_at: dt.datetime
    recorded_at: dt.datetime
    evidence_ref: str | None
    notes: str
    supersedes_id: int | None
    superseded_by_id: int | None


class InventorySnapshot(BaseModel):
    """What we currently believe about an item, and why.

    ``state`` is ``UNKNOWN`` when there is no valid observation or the
    latest one is older than ``fresh_for_days``; ``observation`` and
    ``stated_state`` then still say what was last stated.
    """

    item_id: int
    state: StockState
    stated_state: StockState | None
    stale: bool
    age_days: float | None
    fresh_for_days: int
    observation: InventoryObservation | None


# ---------------------------------------------------------------------------
# Replenishment needs (ADR 0008)
# ---------------------------------------------------------------------------

OPEN_NEED_STATUSES = frozenset(
    {
        NeedStatus.SUGGESTED,
        NeedStatus.APPROVED,
        NeedStatus.DEFERRED,
        NeedStatus.PLANNED,
        NeedStatus.ORDERED,
    }
)


class NeedReason(BaseModel):
    """One reason an item is wanted.

    ``source_key`` says which source the reason came from (for example
    ``inventory`` or ``recipe:12``), so the same source can't add the same
    reason twice. A withdrawn reason stays, with ``active`` false.
    """

    id: int
    kind: NeedReasonKind
    source_key: str
    detail: dict[str, Any]
    active: bool
    created_at: dt.datetime


class ReplenishmentNeed(BaseModel):
    """A household item that someone or something thinks should be bought."""

    id: int
    item_id: int
    status: NeedStatus
    deferred_until: dt.datetime | None
    reasons: list[NeedReason]
    created_at: dt.datetime
    updated_at: dt.datetime

    @property
    def is_open(self) -> bool:
        """Whether the need is still in play.

        Returns:
            True unless the need is dismissed, received or cancelled.
        """
        return self.status in OPEN_NEED_STATUSES

    @property
    def active_reasons(self) -> list[NeedReason]:
        """The reasons that still hold.

        Returns:
            Reasons that have not been withdrawn.
        """
        return [reason for reason in self.reasons if reason.active]


class NeedEvent(BaseModel):
    """One status change in a need's audit trail."""

    id: int
    need_id: int
    from_status: NeedStatus | None
    to_status: NeedStatus
    actor: str
    note: str
    created_at: dt.datetime
