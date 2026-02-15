"""Tests for grocery_butler.models module."""

from __future__ import annotations

import pytest
from pydantic import ValidationError

from grocery_butler.models import (
    BrandMatchType,
    BrandPreference,
    BrandPreferenceType,
    CartItem,
    CartSummary,
    FulfillmentOption,
    FulfillmentType,
    Ingredient,
    IngredientCategory,
    InventoryItem,
    InventoryStatus,
    InventoryUpdate,
    OrganicPreference,
    ParsedMeal,
    PriceSensitivity,
    SafewayProduct,
    ShoppingListItem,
    SubstitutionOption,
    SubstitutionResult,
    SubstitutionSuitability,
)

# ---------------------------------------------------------------------------
# Enum tests
# ---------------------------------------------------------------------------


class TestIngredientCategory:
    """Tests for IngredientCategory enum."""

    def test_all_values(self) -> None:
        """Test all category values exist."""
        expected = {
            "produce",
            "meat",
            "dairy",
            "bakery",
            "pantry_dry",
            "frozen",
            "beverages",
            "deli",
            "other",
        }
        actual = {c.value for c in IngredientCategory}
        assert actual == expected

    def test_is_str_enum(self) -> None:
        """Test IngredientCategory values are strings."""
        assert IngredientCategory.PRODUCE == "produce"
        assert isinstance(IngredientCategory.PRODUCE, str)


class TestInventoryStatus:
    """Tests for InventoryStatus enum."""

    def test_all_values(self) -> None:
        """Test all status values exist."""
        expected = {"on_hand", "low", "out"}
        actual = {s.value for s in InventoryStatus}
        assert actual == expected


class TestBrandPreferenceType:
    """Tests for BrandPreferenceType enum."""

    def test_values(self) -> None:
        """Test preferred and avoid values."""
        assert BrandPreferenceType.PREFERRED == "preferred"
        assert BrandPreferenceType.AVOID == "avoid"


class TestBrandMatchType:
    """Tests for BrandMatchType enum."""

    def test_values(self) -> None:
        """Test category and ingredient values."""
        assert BrandMatchType.CATEGORY == "category"
        assert BrandMatchType.INGREDIENT == "ingredient"


class TestPriceSensitivity:
    """Tests for PriceSensitivity enum."""

    def test_values(self) -> None:
        """Test all price sensitivity levels."""
        expected = {"budget", "moderate", "premium"}
        actual = {p.value for p in PriceSensitivity}
        assert actual == expected


class TestOrganicPreference:
    """Tests for OrganicPreference enum."""

    def test_values(self) -> None:
        """Test organic preference options."""
        expected = {"yes", "no", "when_reasonable"}
        actual = {o.value for o in OrganicPreference}
        assert actual == expected


class TestFulfillmentType:
    """Tests for FulfillmentType enum."""

    def test_values(self) -> None:
        """Test fulfillment type options."""
        assert FulfillmentType.PICKUP == "pickup"
        assert FulfillmentType.DELIVERY == "delivery"


class TestSubstitutionSuitability:
    """Tests for SubstitutionSuitability enum."""

    def test_values(self) -> None:
        """Test all suitability levels."""
        expected = {"excellent", "good", "acceptable", "poor"}
        actual = {s.value for s in SubstitutionSuitability}
        assert actual == expected


# ---------------------------------------------------------------------------
# Core model tests
# ---------------------------------------------------------------------------


class TestIngredient:
    """Tests for Ingredient model."""

    def test_create_minimal(self) -> None:
        """Test creating an Ingredient with required fields only."""
        ing = Ingredient(
            ingredient="flour",
            quantity=2.0,
            unit="cups",
            category=IngredientCategory.PANTRY_DRY,
        )
        assert ing.ingredient == "flour"
        assert ing.quantity == 2.0
        assert ing.unit == "cups"
        assert ing.category == IngredientCategory.PANTRY_DRY
        assert ing.notes == ""
        assert ing.is_pantry_item is False

    def test_create_full(self) -> None:
        """Test creating an Ingredient with all fields."""
        ing = Ingredient(
            ingredient="butter",
            quantity=1.0,
            unit="tbsp",
            category=IngredientCategory.DAIRY,
            notes="unsalted",
            is_pantry_item=True,
        )
        assert ing.notes == "unsalted"
        assert ing.is_pantry_item is True

    def test_missing_required_field_raises(self) -> None:
        """Test ValidationError on missing required fields."""
        with pytest.raises(ValidationError):
            Ingredient(ingredient="flour", quantity=2.0, unit="cups")  # type: ignore[call-arg]


class TestParsedMeal:
    """Tests for ParsedMeal model."""

    def test_create(self) -> None:
        """Test creating a ParsedMeal."""
        flour = Ingredient(
            ingredient="flour",
            quantity=2.0,
            unit="cups",
            category=IngredientCategory.PANTRY_DRY,
        )
        chicken = Ingredient(
            ingredient="chicken breast",
            quantity=1.0,
            unit="lb",
            category=IngredientCategory.MEAT,
        )
        meal = ParsedMeal(
            name="Chicken Parmesan",
            servings=4,
            known_recipe=False,
            needs_confirmation=True,
            purchase_items=[chicken],
            pantry_items=[flour],
        )
        assert meal.name == "Chicken Parmesan"
        assert meal.servings == 4
        assert meal.known_recipe is False
        assert meal.needs_confirmation is True
        assert len(meal.purchase_items) == 1
        assert len(meal.pantry_items) == 1


class TestShoppingListItem:
    """Tests for ShoppingListItem model."""

    def test_create_minimal(self) -> None:
        """Test creating a ShoppingListItem with required fields."""
        item = ShoppingListItem(
            ingredient="chicken breast",
            quantity=2.0,
            unit="lbs",
            category=IngredientCategory.MEAT,
            search_term="boneless chicken breast",
            from_meals=["Chicken Parmesan"],
        )
        assert item.ingredient == "chicken breast"
        assert item.estimated_price is None

    def test_create_with_price(self) -> None:
        """Test ShoppingListItem with estimated price."""
        item = ShoppingListItem(
            ingredient="milk",
            quantity=1.0,
            unit="gallon",
            category=IngredientCategory.DAIRY,
            search_term="whole milk",
            from_meals=["Cereal", "Baking"],
            estimated_price=4.99,
        )
        assert item.estimated_price == 4.99
        assert len(item.from_meals) == 2


class TestInventoryItem:
    """Tests for InventoryItem model."""

    def test_create_defaults(self) -> None:
        """Test InventoryItem default values."""
        item = InventoryItem(
            ingredient="salt",
            display_name="Salt",
        )
        assert item.category is None
        assert item.status == InventoryStatus.ON_HAND
        assert item.default_quantity is None
        assert item.default_unit is None
        assert item.default_search_term is None
        assert item.notes == ""

    def test_create_full(self) -> None:
        """Test InventoryItem with all fields populated."""
        item = InventoryItem(
            ingredient="olive oil",
            display_name="Olive Oil",
            category=IngredientCategory.PANTRY_DRY,
            status=InventoryStatus.LOW,
            default_quantity=1.0,
            default_unit="bottle",
            default_search_term="extra virgin olive oil",
            notes="Prefer Italian",
        )
        assert item.status == InventoryStatus.LOW
        assert item.default_quantity == 1.0


class TestInventoryUpdate:
    """Tests for InventoryUpdate model."""

    def test_create(self) -> None:
        """Test creating an InventoryUpdate."""
        update = InventoryUpdate(
            ingredient="butter",
            new_status=InventoryStatus.OUT,
            confidence=0.95,
        )
        assert update.ingredient == "butter"
        assert update.new_status == InventoryStatus.OUT
        assert update.confidence == 0.95


class TestBrandPreference:
    """Tests for BrandPreference model."""

    def test_create_category_preference(self) -> None:
        """Test creating a category-level brand preference."""
        pref = BrandPreference(
            match_target="dairy",
            match_type=BrandMatchType.CATEGORY,
            brand="Organic Valley",
            preference_type=BrandPreferenceType.PREFERRED,
        )
        assert pref.match_target == "dairy"
        assert pref.match_type == BrandMatchType.CATEGORY
        assert pref.notes == ""

    def test_create_avoid_preference(self) -> None:
        """Test creating an avoid brand preference."""
        pref = BrandPreference(
            match_target="chicken breast",
            match_type=BrandMatchType.INGREDIENT,
            brand="BadBrand",
            preference_type=BrandPreferenceType.AVOID,
            notes="Quality issues",
        )
        assert pref.preference_type == BrandPreferenceType.AVOID
        assert pref.notes == "Quality issues"


# ---------------------------------------------------------------------------
# Future Safeway model tests
# ---------------------------------------------------------------------------


class TestSafewayProduct:
    """Tests for SafewayProduct model."""

    def test_create_minimal(self) -> None:
        """Test creating a SafewayProduct with required fields."""
        product = SafewayProduct(
            product_id="SW-001",
            name="Organic Whole Milk",
            price=5.99,
            size="1 gallon",
        )
        assert product.unit_price is None
        assert product.in_stock is True

    def test_create_full(self) -> None:
        """Test creating a SafewayProduct with all fields."""
        product = SafewayProduct(
            product_id="SW-002",
            name="Greek Yogurt",
            price=4.49,
            unit_price=0.28,
            size="16 oz",
            in_stock=False,
        )
        assert product.unit_price == 0.28
        assert product.in_stock is False


class TestSubstitutionOption:
    """Tests for SubstitutionOption model."""

    def test_create(self) -> None:
        """Test creating a SubstitutionOption."""
        product = SafewayProduct(
            product_id="SW-003",
            name="2% Milk",
            price=4.99,
            size="1 gallon",
        )
        option = SubstitutionOption(
            product=product,
            suitability=SubstitutionSuitability.GOOD,
            reasoning="Same brand, different fat content",
        )
        assert option.suitability == SubstitutionSuitability.GOOD
        assert option.form_warning is None

    def test_create_with_warning(self) -> None:
        """Test SubstitutionOption with form warning."""
        product = SafewayProduct(
            product_id="SW-004",
            name="Powdered Milk",
            price=3.99,
            size="25.6 oz",
        )
        option = SubstitutionOption(
            product=product,
            suitability=SubstitutionSuitability.ACCEPTABLE,
            form_warning="Powdered form - requires reconstitution",
            reasoning="Different form factor",
        )
        assert option.form_warning is not None


class TestSubstitutionResult:
    """Tests for SubstitutionResult model."""

    def test_create_no_alternatives(self) -> None:
        """Test SubstitutionResult with no alternatives found."""
        item = ShoppingListItem(
            ingredient="truffle oil",
            quantity=1.0,
            unit="bottle",
            category=IngredientCategory.PANTRY_DRY,
            search_term="truffle oil",
            from_meals=["Fancy Pasta"],
        )
        result = SubstitutionResult(
            status="no_alternatives",
            original_item=item,
            message="No substitutions found",
        )
        assert result.alternatives == []
        assert result.selected is None


class TestCartItem:
    """Tests for CartItem model."""

    def test_create(self) -> None:
        """Test creating a CartItem."""
        shopping_item = ShoppingListItem(
            ingredient="milk",
            quantity=1.0,
            unit="gallon",
            category=IngredientCategory.DAIRY,
            search_term="whole milk",
            from_meals=["Cereal"],
        )
        product = SafewayProduct(
            product_id="SW-001",
            name="Whole Milk",
            price=5.99,
            size="1 gallon",
        )
        cart_item = CartItem(
            shopping_list_item=shopping_item,
            safeway_product=product,
            quantity_to_order=1,
            estimated_cost=5.99,
        )
        assert cart_item.quantity_to_order == 1
        assert cart_item.estimated_cost == 5.99


class TestFulfillmentOption:
    """Tests for FulfillmentOption model."""

    def test_create(self) -> None:
        """Test creating a FulfillmentOption."""
        option = FulfillmentOption(
            type=FulfillmentType.PICKUP,
            available=True,
            fee=0.0,
            windows=[{"date": "2024-01-15", "time": "10:00-12:00"}],
            next_window="2024-01-15 10:00",
        )
        assert option.type == FulfillmentType.PICKUP
        assert option.fee == 0.0
        assert len(option.windows) == 1

    def test_create_unavailable(self) -> None:
        """Test FulfillmentOption when unavailable."""
        option = FulfillmentOption(
            type=FulfillmentType.DELIVERY,
            available=False,
            fee=9.99,
            windows=[],
            next_window=None,
        )
        assert option.available is False
        assert option.next_window is None


class TestCartSummary:
    """Tests for CartSummary model."""

    def test_create_empty_cart(self) -> None:
        """Test creating an empty CartSummary."""
        summary = CartSummary(
            items=[],
            failed_items=[],
            substituted_items=[],
            skipped_items=[],
            restock_items=[],
            subtotal=0.0,
            fulfillment_options=[],
            recommended_fulfillment=FulfillmentType.PICKUP,
            estimated_total=0.0,
        )
        assert summary.subtotal == 0.0
        assert len(summary.items) == 0
