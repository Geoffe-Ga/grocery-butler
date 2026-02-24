"""Pydantic models and enums for MealBot.

This is the shared type system. ALL data structures are defined here,
including future Safeway models that aren't used yet.
"""

from __future__ import annotations

from enum import StrEnum

from pydantic import BaseModel

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
    unit: str
    category: IngredientCategory
    notes: str = ""
    is_pantry_item: bool = False


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
    unit: str
    category: IngredientCategory
    search_term: str
    from_meals: list[str]
    estimated_price: float | None = None


class InventoryItem(BaseModel):
    """A tracked household inventory item."""

    ingredient: str
    display_name: str
    category: IngredientCategory | None = None
    status: InventoryStatus = InventoryStatus.ON_HAND
    current_quantity: float | None = None
    current_unit: str | None = None
    default_quantity: float | None = None
    default_unit: str | None = None
    default_search_term: str | None = None
    notes: str = ""


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
