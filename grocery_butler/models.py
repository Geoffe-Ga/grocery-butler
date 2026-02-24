"""Pydantic models and enums for MealBot.

This is the shared type system. ALL data structures are defined here,
including future Safeway models that aren't used yet.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel, field_validator

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
}


def parse_unit(raw: str) -> Unit:
    """Parse a raw unit string into a Unit enum member.

    Handles exact matches, aliases, and case-insensitive lookup.
    Falls back to Unit.EACH for unrecognized strings.

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
    return Unit.EACH


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
        if isinstance(v, Unit):
            return v
        return parse_unit(str(v))


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
        if isinstance(v, Unit):
            return v
        return parse_unit(str(v))


class InventoryItem(BaseModel):
    """A tracked household inventory item."""

    ingredient: str
    display_name: str
    category: IngredientCategory | None = None
    status: InventoryStatus = InventoryStatus.ON_HAND
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
        if v is None:
            return None
        if isinstance(v, Unit):
            return v
        return parse_unit(str(v))


class InventoryUpdate(BaseModel):
    """An inventory status change parsed from natural language."""

    ingredient: str
    new_status: InventoryStatus
    confidence: float


class BrandPreference(BaseModel):
    """A brand preference rule (preferred or avoided)."""

    match_target: str
    match_type: BrandMatchType
    brand: str
    preference_type: BrandPreferenceType
    notes: str = ""


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
    """A shopping list item mapped to a Safeway product."""

    shopping_list_item: ShoppingListItem
    safeway_product: SafewayProduct
    quantity_to_order: int
    estimated_cost: float


class FulfillmentOption(BaseModel):
    """A fulfillment option (pickup or delivery) with scheduling."""

    type: FulfillmentType
    available: bool
    fee: float
    windows: list[dict]
    next_window: str | None = None


class CartSummary(BaseModel):
    """Complete cart ready for order submission."""

    items: list[CartItem]
    failed_items: list[ShoppingListItem]
    substituted_items: list[SubstitutionResult]
    skipped_items: list[ShoppingListItem]
    restock_items: list[CartItem]
    subtotal: float
    fulfillment_options: list[FulfillmentOption]
    recommended_fulfillment: FulfillmentType
    estimated_total: float
