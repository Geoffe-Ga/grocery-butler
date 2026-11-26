"""Tests for grocery_butler.models module."""

from __future__ import annotations

import logging

import pytest
from pydantic import ValidationError

from grocery_butler.models import (
    BrandMatchType,
    BrandPreference,
    BrandPreferenceType,
    Ingredient,
    IngredientCategory,
    InventoryItem,
    InventoryStatus,
    InventoryUpdate,
    OrganicPreference,
    ParsedMeal,
    PriceSensitivity,
    ShoppingListItem,
    Unit,
    _coerce_unit,
    _coerce_unit_optional,
    coerce_category,
    coerce_category_optional,
    parse_unit,
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


class TestUnit:
    """Tests for Unit enum."""

    def test_all_members_are_lowercase(self) -> None:
        """Test all Unit values are lowercase strings."""
        for member in Unit:
            assert member.value == member.value.lower()

    def test_unit_is_str(self) -> None:
        """Test Unit members are strings (StrEnum)."""
        assert isinstance(Unit.LB, str)
        assert Unit.LB == "lb"

    def test_known_members_exist(self) -> None:
        """Test all expected unit members are defined.

        Includes pint/quart/stick, added for issue #69 (Unit enum lacked
        these units, forcing recipes that used them through the silent
        parse_unit -> EACH fallback).
        """
        expected = {
            "tsp",
            "tbsp",
            "cup",
            "fl_oz",
            "ml",
            "l",
            "gal",
            "pint",
            "quart",
            "oz",
            "lb",
            "g",
            "kg",
            "each",
            "dozen",
            "bunch",
            "head",
            "clove",
            "slice",
            "can",
            "bag",
            "box",
            "jar",
            "bottle",
            "package",
            "block",
            "stick",
            "pinch",
            "dash",
            "to_taste",
        }
        actual = {m.value for m in Unit}
        assert expected == actual


class TestParseUnit:
    """Tests for parse_unit function."""

    def test_exact_match(self) -> None:
        """Test parse_unit with exact enum values."""
        assert parse_unit("lb") == Unit.LB
        assert parse_unit("tsp") == Unit.TSP
        assert parse_unit("each") == Unit.EACH

    def test_alias_weight(self) -> None:
        """Test weight aliases normalize correctly."""
        assert parse_unit("lbs") == Unit.LB
        assert parse_unit("pound") == Unit.LB
        assert parse_unit("pounds") == Unit.LB
        assert parse_unit("ounce") == Unit.OZ
        assert parse_unit("ounces") == Unit.OZ

    def test_alias_volume(self) -> None:
        """Test volume aliases normalize correctly."""
        assert parse_unit("cups") == Unit.CUP
        assert parse_unit("teaspoon") == Unit.TSP
        assert parse_unit("teaspoons") == Unit.TSP
        assert parse_unit("tablespoon") == Unit.TBSP
        assert parse_unit("tablespoons") == Unit.TBSP
        assert parse_unit("gallon") == Unit.GAL
        assert parse_unit("gallons") == Unit.GAL

    def test_alias_count(self) -> None:
        """Test count aliases normalize correctly."""
        assert parse_unit("piece") == Unit.EACH
        assert parse_unit("pieces") == Unit.EACH
        assert parse_unit("cloves") == Unit.CLOVE
        assert parse_unit("heads") == Unit.HEAD

    def test_alias_packaging(self) -> None:
        """Test packaging aliases normalize correctly."""
        assert parse_unit("cans") == Unit.CAN
        assert parse_unit("jars") == Unit.JAR
        assert parse_unit("bottles") == Unit.BOTTLE
        assert parse_unit("pkg") == Unit.PACKAGE

    def test_case_insensitive(self) -> None:
        """Test parse_unit is case-insensitive."""
        assert parse_unit("LB") == Unit.LB
        assert parse_unit("Cups") == Unit.CUP
        assert parse_unit("GALLON") == Unit.GAL

    def test_whitespace_stripped(self) -> None:
        """Test parse_unit strips whitespace."""
        assert parse_unit("  lb  ") == Unit.LB
        assert parse_unit("\tcup\n") == Unit.CUP

    def test_empty_string_defaults_to_each(self) -> None:
        """Test empty/blank strings default to EACH."""
        assert parse_unit("") == Unit.EACH
        assert parse_unit("   ") == Unit.EACH

    def test_unknown_defaults_to_each(self) -> None:
        """Test unrecognized strings default to EACH."""
        assert parse_unit("foobar") == Unit.EACH
        assert parse_unit("xyz123") == Unit.EACH


class TestParseUnitNewUnits:
    """Tests for parse_unit support of pint/quart/stick units (issue #69)."""

    def test_pint_and_aliases(self) -> None:
        """Test 'pint', 'pt', and 'pints' all resolve to Unit.PINT."""
        assert parse_unit("pint") == Unit.PINT
        assert parse_unit("pt") == Unit.PINT
        assert parse_unit("pints") == Unit.PINT

    def test_quart_and_aliases(self) -> None:
        """Test 'quart', 'qt', and 'quarts' all resolve to Unit.QUART."""
        assert parse_unit("quart") == Unit.QUART
        assert parse_unit("qt") == Unit.QUART
        assert parse_unit("quarts") == Unit.QUART

    def test_stick_and_aliases(self) -> None:
        """Test 'stick' and 'sticks' both resolve to Unit.STICK."""
        assert parse_unit("stick") == Unit.STICK
        assert parse_unit("sticks") == Unit.STICK


class TestParseUnitWarningLogging:
    """Tests for parse_unit's WARNING log on unknown unit fallback (issue #69).

    Currently ``parse_unit`` silently falls back to ``Unit.EACH`` for any
    unrecognized token, with no logging at all -- masking data-quality
    problems from LLM output or user input. Post-fix, an unrecognized
    non-blank token must log a WARNING containing the raw token, while a
    blank/empty token continues to fall back silently.
    """

    def test_unknown_unit_logs_warning_with_raw_token(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test an unrecognized unit token logs a WARNING containing the token."""
        with caplog.at_level(logging.WARNING, logger="grocery_butler.models"):
            result = parse_unit("zorbml")
        assert result == Unit.EACH
        assert len(caplog.records) == 1
        assert "zorbml" in caplog.text

    def test_empty_string_emits_no_warning(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test blank input falls back to EACH silently, with no WARNING logged."""
        with caplog.at_level(logging.WARNING, logger="grocery_butler.models"):
            result = parse_unit("")
        assert result == Unit.EACH
        assert len(caplog.records) == 0


class TestCoerceUnit:
    """Tests for the _coerce_unit module-level helper."""

    def test_unit_passthrough(self) -> None:
        """Test Unit enum members are returned unchanged."""
        assert _coerce_unit(Unit.LB) is Unit.LB

    def test_string_alias_normalized(self) -> None:
        """Test alias strings are resolved to Unit members."""
        assert _coerce_unit("pounds") == Unit.LB

    def test_exact_string_normalized(self) -> None:
        """Test exact unit strings are resolved."""
        assert _coerce_unit("lb") == Unit.LB

    def test_unknown_string_defaults_to_each(self) -> None:
        """Test unrecognized strings default to EACH."""
        assert _coerce_unit("foobar") == Unit.EACH


class TestCoerceUnitOptional:
    """Tests for the _coerce_unit_optional module-level helper."""

    def test_none_returns_none(self) -> None:
        """Test None input is propagated as None."""
        assert _coerce_unit_optional(None) is None

    def test_unit_passthrough(self) -> None:
        """Test Unit enum members are returned unchanged."""
        assert _coerce_unit_optional(Unit.GAL) is Unit.GAL

    def test_string_alias_normalized(self) -> None:
        """Test alias strings are resolved."""
        assert _coerce_unit_optional("gallon") == Unit.GAL

    def test_exact_string_normalized(self) -> None:
        """Test exact unit strings are resolved."""
        assert _coerce_unit_optional("gal") == Unit.GAL


class TestCoerceCategory:
    """Tests for the coerce_category module-level helper (Issue #68)."""

    def test_known_value_returns_enum(self) -> None:
        """Test a known category string maps to its enum member."""
        assert coerce_category("produce") == IngredientCategory.PRODUCE

    def test_unknown_value_returns_other(self) -> None:
        """Test an unrecognized category string degrades to OTHER."""
        assert coerce_category("spices") == IngredientCategory.OTHER

    def test_unknown_value_logs_warning(self, caplog: pytest.LogCaptureFixture) -> None:
        """Test an unrecognized category string logs a WARNING mentioning it."""
        with caplog.at_level(logging.WARNING, logger="grocery_butler.models"):
            coerce_category("spices")

        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert len(warnings) == 1
        assert "spices" in warnings[0].message

    def test_none_returns_other(self) -> None:
        """Test None input degrades to OTHER."""
        assert coerce_category(None) == IngredientCategory.OTHER

    def test_empty_string_returns_other_silently(
        self, caplog: pytest.LogCaptureFixture
    ) -> None:
        """Test an empty string degrades to OTHER without logging a WARNING."""
        with caplog.at_level(logging.WARNING, logger="grocery_butler.models"):
            result = coerce_category("")

        assert result == IngredientCategory.OTHER
        warnings = [r for r in caplog.records if r.levelno == logging.WARNING]
        assert warnings == []

    def test_existing_enum_passthrough(self) -> None:
        """Test an existing IngredientCategory member passes through unchanged."""
        assert coerce_category(IngredientCategory.MEAT) == IngredientCategory.MEAT


class TestCoerceCategoryOptional:
    """Tests for the coerce_category_optional module-level helper (Issue #68)."""

    def test_none_returns_none(self) -> None:
        """Test None input is preserved as None (not coerced to OTHER)."""
        assert coerce_category_optional(None) is None

    def test_invalid_returns_other(self) -> None:
        """Test an invalid category string degrades to OTHER."""
        assert coerce_category_optional("legacy_snacks") == IngredientCategory.OTHER


class TestUnitValidatorOnModels:
    """Tests for unit field_validator integration on models."""

    def test_ingredient_normalizes_alias(self) -> None:
        """Test Ingredient auto-normalizes unit aliases."""
        ing = Ingredient(
            ingredient="chicken",
            quantity=2.0,
            unit="lbs",
            category=IngredientCategory.MEAT,
        )
        assert ing.unit == Unit.LB

    def test_shopping_list_item_normalizes_alias(self) -> None:
        """Test ShoppingListItem auto-normalizes unit aliases."""
        item = ShoppingListItem(
            ingredient="milk",
            quantity=1.0,
            unit="gallon",
            category=IngredientCategory.DAIRY,
            search_term="milk",
            from_meals=["cereal"],
        )
        assert item.unit == Unit.GAL

    def test_inventory_item_normalizes_default_unit(self) -> None:
        """Test InventoryItem auto-normalizes default_unit aliases."""
        item = InventoryItem(
            ingredient="milk",
            display_name="Milk",
            default_unit="gallon",
        )
        assert item.default_unit == Unit.GAL

    def test_inventory_item_default_unit_none_preserved(self) -> None:
        """Test InventoryItem preserves None default_unit."""
        item = InventoryItem(
            ingredient="salt",
            display_name="Salt",
        )
        assert item.default_unit is None

    def test_ingredient_empty_unit_becomes_each(self) -> None:
        """Test empty unit string becomes EACH on Ingredient."""
        ing = Ingredient(
            ingredient="egg",
            quantity=2.0,
            unit="",
            category=IngredientCategory.OTHER,
        )
        assert ing.unit == Unit.EACH


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
        assert ing.unit == "cup"
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


class TestIngredientNamesAreNotBlank:
    """A blank name is refused when the model is built (issue #198)."""

    @pytest.mark.parametrize("name", ["", " ", "\t\n", "   "])
    def test_ingredient_refuses_a_blank_name(self, name: str) -> None:
        """An ingredient with no name never exists, so no store can see one."""
        with pytest.raises(ValidationError, match="An ingredient needs a name"):
            Ingredient(
                ingredient=name,
                quantity=1.0,
                unit="each",
                category=IngredientCategory.OTHER,
            )

    @pytest.mark.parametrize("name", ["", " ", "\t\n"])
    def test_inventory_item_refuses_a_blank_name(self, name: str) -> None:
        """The same rule for a tracked inventory item."""
        with pytest.raises(ValidationError, match="An ingredient needs a name"):
            InventoryItem(ingredient=name, display_name="Something")

    def test_a_real_name_is_kept_exactly_as_given(self) -> None:
        """The check refuses blanks; it does not rewrite names."""
        item = InventoryItem(ingredient=" Olive Oil ", display_name="Olive Oil")

        assert item.ingredient == " Olive Oil "
