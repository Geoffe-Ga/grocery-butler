"""Tests for grocery_butler.consolidator module."""

from __future__ import annotations

import json
from unittest.mock import MagicMock

import pytest

from grocery_butler.claude_utils import extract_json_text
from grocery_butler.consolidator import (
    Consolidator,
    _build_ingredient_text,
    _flatten_meal_ingredients,
    _format_inventory_overrides,
    _format_pantry_staples,
    _format_restock_queue,
    _parse_response_items,
    _parse_shopping_item,
)
from grocery_butler.models import (
    Ingredient,
    IngredientCategory,
    InventoryItem,
    InventoryStatus,
    ParsedMeal,
    ShoppingListItem,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def sample_tacos_meal() -> ParsedMeal:
    """Return a sample tacos ParsedMeal for testing.

    Returns:
        A tacos meal with purchase and pantry items.
    """
    return ParsedMeal(
        name="Chicken Tacos",
        servings=4,
        known_recipe=True,
        needs_confirmation=False,
        purchase_items=[
            Ingredient(
                ingredient="chicken thighs",
                quantity=2.0,
                unit="lbs",
                category=IngredientCategory.MEAT,
            ),
            Ingredient(
                ingredient="corn tortillas",
                quantity=12.0,
                unit="each",
                category=IngredientCategory.BAKERY,
            ),
            Ingredient(
                ingredient="lime",
                quantity=2.0,
                unit="each",
                category=IngredientCategory.PRODUCE,
            ),
        ],
        pantry_items=[
            Ingredient(
                ingredient="olive oil",
                quantity=2.0,
                unit="tbsp",
                category=IngredientCategory.PANTRY_DRY,
                is_pantry_item=True,
            ),
        ],
    )


@pytest.fixture()
def sample_tikka_meal() -> ParsedMeal:
    """Return a sample tikka masala ParsedMeal for testing.

    Returns:
        A tikka masala meal with purchase and pantry items.
    """
    return ParsedMeal(
        name="Chicken Tikka Masala",
        servings=4,
        known_recipe=True,
        needs_confirmation=False,
        purchase_items=[
            Ingredient(
                ingredient="chicken breast",
                quantity=1.5,
                unit="lbs",
                category=IngredientCategory.MEAT,
            ),
            Ingredient(
                ingredient="lime",
                quantity=1.0,
                unit="each",
                category=IngredientCategory.PRODUCE,
            ),
            Ingredient(
                ingredient="tikka masala sauce",
                quantity=1.0,
                unit="jar",
                category=IngredientCategory.PANTRY_DRY,
            ),
        ],
        pantry_items=[
            Ingredient(
                ingredient="salt",
                quantity=1.0,
                unit="tsp",
                category=IngredientCategory.PANTRY_DRY,
                is_pantry_item=True,
            ),
        ],
    )


@pytest.fixture()
def sample_restock_queue() -> list[InventoryItem]:
    """Return a sample restock queue for testing.

    Returns:
        List with low/out inventory items and one on_hand item.
    """
    return [
        InventoryItem(
            ingredient="milk",
            display_name="Whole Milk",
            category=IngredientCategory.DAIRY,
            status=InventoryStatus.OUT,
            default_quantity=1.0,
            default_unit="gallon",
            default_search_term="whole milk gallon",
        ),
        InventoryItem(
            ingredient="butter",
            display_name="Butter",
            category=IngredientCategory.DAIRY,
            status=InventoryStatus.LOW,
            default_quantity=1.0,
            default_unit="lb",
            default_search_term="unsalted butter",
        ),
        InventoryItem(
            ingredient="eggs",
            display_name="Eggs",
            category=IngredientCategory.DAIRY,
            status=InventoryStatus.ON_HAND,
            default_quantity=1.0,
            default_unit="dozen",
        ),
    ]


@pytest.fixture()
def pantry_staples() -> list[str]:
    """Return a sample pantry staples list.

    Returns:
        List of pantry staple names.
    """
    return ["salt", "pepper", "olive oil", "garlic powder"]


@pytest.fixture()
def mock_client() -> MagicMock:
    """Return a mock Anthropic client.

    Returns:
        MagicMock with messages.create() configured.
    """
    return MagicMock()


def _make_claude_response(text: str) -> MagicMock:
    """Build a mock Claude API response with the given text.

    Args:
        text: The text content for the response.

    Returns:
        MagicMock shaped like an Anthropic message response.
    """
    content_block = MagicMock()
    content_block.text = text
    response = MagicMock()
    response.content = [content_block]
    return response


def _valid_consolidation_json() -> str:
    """Return valid JSON for a consolidated shopping list response.

    Returns:
        JSON string representing a consolidated shopping list.
    """
    return json.dumps(
        [
            {
                "ingredient": "chicken thighs",
                "quantity": 2.0,
                "unit": "lbs",
                "category": "meat",
                "search_term": "boneless chicken thighs",
                "from_meals": ["Chicken Tacos"],
                "estimated_price": 8.99,
            },
            {
                "ingredient": "lime",
                "quantity": 3.0,
                "unit": "each",
                "category": "produce",
                "search_term": "limes",
                "from_meals": ["Chicken Tacos", "Chicken Tikka Masala"],
                "estimated_price": None,
            },
            {
                "ingredient": "chicken breast",
                "quantity": 1.5,
                "unit": "lbs",
                "category": "meat",
                "search_term": "boneless chicken breast",
                "from_meals": ["Chicken Tikka Masala"],
                "estimated_price": 7.49,
            },
        ]
    )


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestExtractJsonText:
    """Tests for extract_json_text helper."""

    def test_plain_json(self):
        """Test plain JSON passes through."""
        text = '{"key": "value"}'
        assert extract_json_text(text) == text

    def test_strips_markdown_fences(self):
        """Test markdown code fences are stripped."""
        text = '```json\n[{"key": "value"}]\n```'
        assert extract_json_text(text) == '[{"key": "value"}]'

    def test_strips_plain_fences(self):
        """Test plain code fences are stripped."""
        text = '```\n[{"key": "value"}]\n```'
        assert extract_json_text(text) == '[{"key": "value"}]'

    def test_strips_whitespace(self):
        """Test surrounding whitespace is stripped."""
        text = '  [{"key": "value"}]  '
        assert extract_json_text(text) == '[{"key": "value"}]'


class TestFormatPantryStaples:
    """Tests for _format_pantry_staples helper."""

    def test_empty_list(self):
        """Test empty list returns 'None'."""
        assert _format_pantry_staples([]) == "None"

    def test_single_item(self):
        """Test single item formatting."""
        assert _format_pantry_staples(["salt"]) == "salt"

    def test_multiple_items(self):
        """Test multiple items joined with commas."""
        result = _format_pantry_staples(["salt", "pepper", "olive oil"])
        assert result == "salt, pepper, olive oil"


class TestFormatRestockQueue:
    """Tests for _format_restock_queue helper."""

    def test_empty_queue(self):
        """Test empty queue returns 'None'."""
        assert _format_restock_queue([]) == "None"

    def test_filters_on_hand_items(self, sample_restock_queue: list[InventoryItem]):
        """Test on_hand items are excluded."""
        result = _format_restock_queue(sample_restock_queue)
        assert "Eggs" not in result
        assert "Whole Milk" in result
        assert "Butter" in result

    def test_includes_quantity_info(self, sample_restock_queue: list[InventoryItem]):
        """Test quantity and unit are included."""
        result = _format_restock_queue(sample_restock_queue)
        assert "1.0 gal" in result

    def test_all_on_hand_returns_none(self):
        """Test queue with only on_hand items returns 'None'."""
        queue = [
            InventoryItem(
                ingredient="eggs",
                display_name="Eggs",
                status=InventoryStatus.ON_HAND,
            ),
        ]
        assert _format_restock_queue(queue) == "None"

    def test_item_without_quantity(self):
        """Test item without default_quantity omits qty info."""
        queue = [
            InventoryItem(
                ingredient="sponges",
                display_name="Sponges",
                status=InventoryStatus.OUT,
            ),
        ]
        result = _format_restock_queue(queue)
        assert "Sponges" in result
        assert "qty:" not in result


class TestFormatInventoryOverrides:
    """Tests for _format_inventory_overrides helper."""

    def test_none_input(self):
        """Test None returns 'None'."""
        assert _format_inventory_overrides(None) == "None"

    def test_empty_list(self):
        """Test empty list returns 'None'."""
        assert _format_inventory_overrides([]) == "None"

    def test_single_item(self):
        """Test single override item."""
        assert _format_inventory_overrides(["salt"]) == "salt"

    def test_multiple_items(self):
        """Test multiple override items joined with commas."""
        result = _format_inventory_overrides(["salt", "olive oil"])
        assert result == "salt, olive oil"


class TestParseShoppingItem:
    """Tests for _parse_shopping_item helper."""

    def test_complete_item(self):
        """Test parsing a complete shopping item dictionary."""
        data: dict[str, object] = {
            "ingredient": "chicken thighs",
            "quantity": 2.0,
            "unit": "lbs",
            "category": "meat",
            "search_term": "boneless chicken thighs",
            "from_meals": ["Chicken Tacos"],
            "estimated_price": 8.99,
        }
        result = _parse_shopping_item(data)
        assert result.ingredient == "chicken thighs"
        assert result.quantity == 2.0
        assert result.unit == "lb"
        assert result.category == IngredientCategory.MEAT
        assert result.search_term == "boneless chicken thighs"
        assert result.from_meals == ["Chicken Tacos"]
        assert result.estimated_price == 8.99

    def test_defaults_for_missing_fields(self):
        """Test default values when fields are missing."""
        result = _parse_shopping_item({})
        assert result.ingredient == ""
        assert result.quantity == 0.0
        assert result.unit == "each"
        assert result.category == IngredientCategory.OTHER
        assert result.search_term == ""
        assert result.from_meals == []
        assert result.estimated_price is None

    def test_null_estimated_price(self):
        """Test null estimated_price stays None."""
        data: dict[str, object] = {
            "ingredient": "lime",
            "quantity": 3.0,
            "unit": "each",
            "category": "produce",
            "search_term": "limes",
            "from_meals": ["Tacos"],
            "estimated_price": None,
        }
        result = _parse_shopping_item(data)
        assert result.estimated_price is None

    def test_invalid_category_defaults_to_other(self):
        """Test invalid category string defaults to 'other'."""
        data: dict[str, object] = {
            "ingredient": "mystery item",
            "quantity": 1.0,
            "unit": "each",
            "category": "not_a_real_category",
            "search_term": "mystery",
            "from_meals": [],
        }
        result = _parse_shopping_item(data)
        assert result.category == IngredientCategory.OTHER

    def test_non_list_from_meals(self):
        """Test non-list from_meals defaults to empty list."""
        data: dict[str, object] = {
            "ingredient": "test",
            "quantity": 1.0,
            "unit": "each",
            "category": "other",
            "search_term": "test",
            "from_meals": "not a list",
        }
        result = _parse_shopping_item(data)
        assert result.from_meals == []

    def test_string_quantity_defaults_to_zero(self):
        """Test string quantity defaults to 0.0."""
        data: dict[str, object] = {
            "ingredient": "test",
            "quantity": "not a number",
            "unit": "each",
            "category": "other",
            "search_term": "test",
            "from_meals": [],
        }
        result = _parse_shopping_item(data)
        assert result.quantity == 0.0


class TestParseResponseItems:
    """Tests for _parse_response_items helper."""

    def test_valid_json_array(self):
        """Test parsing a valid JSON array."""
        result = _parse_response_items(_valid_consolidation_json())
        assert result is not None
        assert len(result) == 3
        assert result[0].ingredient == "chicken thighs"

    def test_invalid_json_returns_none(self):
        """Test invalid JSON returns None."""
        assert _parse_response_items("not json at all") is None

    def test_non_array_returns_none(self):
        """Test non-array JSON returns None."""
        assert _parse_response_items('{"key": "value"}') is None

    def test_strips_markdown_fences(self):
        """Test markdown fences are stripped before parsing."""
        fenced = f"```json\n{_valid_consolidation_json()}\n```"
        result = _parse_response_items(fenced)
        assert result is not None
        assert len(result) == 3

    def test_skips_non_dict_items(self):
        """Test non-dict items in array are skipped."""
        data = json.dumps(
            [
                {
                    "ingredient": "lime",
                    "quantity": 1.0,
                    "unit": "each",
                    "category": "produce",
                    "search_term": "lime",
                    "from_meals": ["Test"],
                },
                "not a dict",
                42,
            ]
        )
        result = _parse_response_items(data)
        assert result is not None
        assert len(result) == 1


class TestFlattenMealIngredients:
    """Tests for _flatten_meal_ingredients helper."""

    def test_flattens_single_meal(self, sample_tacos_meal: ParsedMeal):
        """Test flattening a single meal's ingredients."""
        result = _flatten_meal_ingredients([sample_tacos_meal])
        assert len(result) == 3
        ingredients = [r[0] for r in result]
        assert "chicken thighs" in ingredients
        assert "corn tortillas" in ingredients

    def test_flattens_multiple_meals(
        self,
        sample_tacos_meal: ParsedMeal,
        sample_tikka_meal: ParsedMeal,
    ):
        """Test flattening multiple meals' ingredients."""
        result = _flatten_meal_ingredients([sample_tacos_meal, sample_tikka_meal])
        assert len(result) == 6

    def test_empty_meals_list(self):
        """Test empty meals list returns empty."""
        assert _flatten_meal_ingredients([]) == []

    def test_includes_meal_name(self, sample_tacos_meal: ParsedMeal):
        """Test each tuple includes the meal name."""
        result = _flatten_meal_ingredients([sample_tacos_meal])
        assert all(r[4] == "Chicken Tacos" for r in result)


class TestBuildIngredientText:
    """Tests for _build_ingredient_text helper."""

    def test_empty_meals(self):
        """Test empty meals returns 'No meal ingredients.'."""
        assert _build_ingredient_text([]) == "No meal ingredients."

    def test_single_meal(self, sample_tacos_meal: ParsedMeal):
        """Test formatting a single meal."""
        result = _build_ingredient_text([sample_tacos_meal])
        assert "Chicken Tacos" in result
        assert "chicken thighs" in result

    def test_multiple_meals(
        self,
        sample_tacos_meal: ParsedMeal,
        sample_tikka_meal: ParsedMeal,
    ):
        """Test formatting multiple meals."""
        result = _build_ingredient_text([sample_tacos_meal, sample_tikka_meal])
        assert "Chicken Tacos" in result
        assert "Chicken Tikka Masala" in result


# ---------------------------------------------------------------------------
# Consolidator class tests
# ---------------------------------------------------------------------------


class TestConsolidateSimpleSameIngredient:
    """Tests for same ingredient from 2 meals gets summed."""

    def test_quantities_summed(
        self,
        sample_tacos_meal: ParsedMeal,
        sample_tikka_meal: ParsedMeal,
        pantry_staples: list[str],
    ):
        """Test same ingredient from 2 meals has summed quantities."""
        consolidator = Consolidator()
        result = consolidator.consolidate_simple(
            [sample_tacos_meal, sample_tikka_meal],
            [],
            pantry_staples,
        )
        lime_items = [i for i in result if i.ingredient == "lime"]
        assert len(lime_items) == 1
        assert lime_items[0].quantity == 3.0

    def test_from_meals_tracks_both(
        self,
        sample_tacos_meal: ParsedMeal,
        sample_tikka_meal: ParsedMeal,
        pantry_staples: list[str],
    ):
        """Test from_meals tracks both meal names."""
        consolidator = Consolidator()
        result = consolidator.consolidate_simple(
            [sample_tacos_meal, sample_tikka_meal],
            [],
            pantry_staples,
        )
        lime_items = [i for i in result if i.ingredient == "lime"]
        assert len(lime_items) == 1
        assert "Chicken Tacos" in lime_items[0].from_meals
        assert "Chicken Tikka Masala" in lime_items[0].from_meals


class TestConsolidateSimpleDifferentProteinCuts:
    """Tests for different protein cuts staying separate."""

    def test_different_cuts_not_merged(
        self,
        sample_tacos_meal: ParsedMeal,
        sample_tikka_meal: ParsedMeal,
        pantry_staples: list[str],
    ):
        """Test chicken thighs and chicken breast stay separate."""
        consolidator = Consolidator()
        result = consolidator.consolidate_simple(
            [sample_tacos_meal, sample_tikka_meal],
            [],
            pantry_staples,
        )
        thigh_items = [i for i in result if i.ingredient == "chicken thighs"]
        breast_items = [i for i in result if i.ingredient == "chicken breast"]
        assert len(thigh_items) == 1
        assert len(breast_items) == 1
        assert thigh_items[0].quantity == 2.0
        assert breast_items[0].quantity == 1.5


class TestConsolidateSimplePantryStapleExclusion:
    """Tests for pantry staple exclusion."""

    def test_pantry_staple_excluded(self, pantry_staples: list[str]):
        """Test pantry staple ingredient excluded by default."""
        meal = ParsedMeal(
            name="Test Meal",
            servings=4,
            known_recipe=False,
            needs_confirmation=False,
            purchase_items=[
                Ingredient(
                    ingredient="salt",
                    quantity=1.0,
                    unit="tsp",
                    category=IngredientCategory.PANTRY_DRY,
                ),
                Ingredient(
                    ingredient="chicken breast",
                    quantity=1.0,
                    unit="lbs",
                    category=IngredientCategory.MEAT,
                ),
            ],
            pantry_items=[],
        )
        consolidator = Consolidator()
        result = consolidator.consolidate_simple([meal], [], pantry_staples)
        ingredient_names = [i.ingredient for i in result]
        assert "salt" not in ingredient_names
        assert "chicken breast" in ingredient_names

    def test_pantry_exclusion_case_insensitive(self, pantry_staples: list[str]):
        """Test pantry staple matching is case insensitive."""
        meal = ParsedMeal(
            name="Test Meal",
            servings=4,
            known_recipe=False,
            needs_confirmation=False,
            purchase_items=[
                Ingredient(
                    ingredient="Salt",
                    quantity=1.0,
                    unit="tsp",
                    category=IngredientCategory.PANTRY_DRY,
                ),
            ],
            pantry_items=[],
        )
        consolidator = Consolidator()
        result = consolidator.consolidate_simple([meal], [], pantry_staples)
        ingredient_names = [i.ingredient for i in result]
        assert "Salt" not in ingredient_names


class TestConsolidateSimpleRestockItems:
    """Tests for restock items handling."""

    def test_restock_items_appended(
        self,
        sample_restock_queue: list[InventoryItem],
        pantry_staples: list[str],
    ):
        """Test restock items appended with from_meals=['restock']."""
        consolidator = Consolidator()
        result = consolidator.consolidate_simple(
            [],
            sample_restock_queue,
            pantry_staples,
        )
        restock_items = [i for i in result if "restock" in i.from_meals]
        assert len(restock_items) == 2
        ingredient_names = [i.ingredient for i in restock_items]
        assert "milk" in ingredient_names
        assert "butter" in ingredient_names

    def test_restock_excludes_on_hand(
        self,
        sample_restock_queue: list[InventoryItem],
        pantry_staples: list[str],
    ):
        """Test on_hand items are not included in restock."""
        consolidator = Consolidator()
        result = consolidator.consolidate_simple(
            [],
            sample_restock_queue,
            pantry_staples,
        )
        ingredient_names = [i.ingredient for i in result]
        assert "eggs" not in ingredient_names

    def test_restock_uses_default_search_term(
        self,
        sample_restock_queue: list[InventoryItem],
        pantry_staples: list[str],
    ):
        """Test restock items use default_search_term."""
        consolidator = Consolidator()
        result = consolidator.consolidate_simple(
            [],
            sample_restock_queue,
            pantry_staples,
        )
        milk_items = [i for i in result if i.ingredient == "milk"]
        assert len(milk_items) == 1
        assert milk_items[0].search_term == "whole milk gallon"

    def test_restock_fallback_search_term(self, pantry_staples: list[str]):
        """Test restock item without search_term falls back to ingredient name."""
        queue = [
            InventoryItem(
                ingredient="sponges",
                display_name="Sponges",
                status=InventoryStatus.OUT,
            ),
        ]
        consolidator = Consolidator()
        result = consolidator.consolidate_simple([], queue, pantry_staples)
        sponge_items = [i for i in result if i.ingredient == "sponges"]
        assert len(sponge_items) == 1
        assert sponge_items[0].search_term == "sponges"
        assert sponge_items[0].quantity == 1.0
        assert sponge_items[0].unit == "each"
        assert sponge_items[0].category == IngredientCategory.OTHER


class TestConsolidateSimpleEmptyMeals:
    """Tests for empty meals list handling."""

    def test_empty_meals_returns_only_restock(
        self,
        sample_restock_queue: list[InventoryItem],
        pantry_staples: list[str],
    ):
        """Test empty meals list returns only restock items."""
        consolidator = Consolidator()
        result = consolidator.consolidate_simple(
            [],
            sample_restock_queue,
            pantry_staples,
        )
        assert len(result) == 2
        assert all("restock" in i.from_meals for i in result)

    def test_empty_meals_empty_restock(self, pantry_staples: list[str]):
        """Test empty meals and empty restock returns empty list."""
        consolidator = Consolidator()
        result = consolidator.consolidate_simple([], [], pantry_staples)
        assert result == []


class TestConsolidateWithClaude:
    """Tests for Claude-powered consolidation."""

    def test_parses_claude_response(
        self,
        mock_client: MagicMock,
        sample_tacos_meal: ParsedMeal,
        sample_tikka_meal: ParsedMeal,
        pantry_staples: list[str],
    ):
        """Test Claude response parsing into ShoppingListItem list."""
        mock_client.messages.create.return_value = _make_claude_response(
            _valid_consolidation_json()
        )
        consolidator = Consolidator(anthropic_client=mock_client)
        result = consolidator.consolidate(
            [sample_tacos_meal, sample_tikka_meal],
            [],
            pantry_staples,
        )
        assert len(result) == 3
        assert all(isinstance(i, ShoppingListItem) for i in result)

    def test_claude_called_with_prompt(
        self,
        mock_client: MagicMock,
        sample_tacos_meal: ParsedMeal,
        pantry_staples: list[str],
    ):
        """Test Claude is called with a formatted prompt."""
        mock_client.messages.create.return_value = _make_claude_response(
            _valid_consolidation_json()
        )
        consolidator = Consolidator(anthropic_client=mock_client)
        consolidator.consolidate([sample_tacos_meal], [], pantry_staples)
        mock_client.messages.create.assert_called_once()
        call_kwargs = mock_client.messages.create.call_args
        assert call_kwargs[1]["model"] == "claude-sonnet-4-6"

    def test_no_client_uses_simple_fallback(
        self,
        sample_tacos_meal: ParsedMeal,
        pantry_staples: list[str],
    ):
        """Test no client falls back to consolidate_simple."""
        consolidator = Consolidator()
        result = consolidator.consolidate(
            [sample_tacos_meal],
            [],
            pantry_staples,
        )
        assert len(result) > 0
        assert all(isinstance(i, ShoppingListItem) for i in result)

    def test_includes_restock_in_prompt(
        self,
        mock_client: MagicMock,
        sample_tacos_meal: ParsedMeal,
        sample_restock_queue: list[InventoryItem],
        pantry_staples: list[str],
    ):
        """Test restock queue is included in the Claude prompt."""
        mock_client.messages.create.return_value = _make_claude_response(
            _valid_consolidation_json()
        )
        consolidator = Consolidator(anthropic_client=mock_client)
        consolidator.consolidate(
            [sample_tacos_meal],
            sample_restock_queue,
            pantry_staples,
        )
        call_args = mock_client.messages.create.call_args
        prompt = call_args[1]["messages"][0]["content"]
        assert "Whole Milk" in prompt

    def test_includes_inventory_overrides_in_prompt(
        self,
        mock_client: MagicMock,
        sample_tacos_meal: ParsedMeal,
        pantry_staples: list[str],
    ):
        """Test inventory overrides are included in the prompt."""
        mock_client.messages.create.return_value = _make_claude_response(
            _valid_consolidation_json()
        )
        consolidator = Consolidator(anthropic_client=mock_client)
        consolidator.consolidate(
            [sample_tacos_meal],
            [],
            pantry_staples,
            inventory_overrides=["salt", "olive oil"],
        )
        call_args = mock_client.messages.create.call_args
        prompt = call_args[1]["messages"][0]["content"]
        assert "salt" in prompt
        assert "olive oil" in prompt


class TestConsolidateInvalidJsonRetry:
    """Tests for retry on invalid JSON from Claude."""

    def test_retries_on_invalid_json(
        self,
        mock_client: MagicMock,
        sample_tacos_meal: ParsedMeal,
        pantry_staples: list[str],
    ):
        """Test invalid JSON triggers a retry with valid response."""
        mock_client.messages.create.side_effect = [
            _make_claude_response("This is not JSON at all!!!"),
            _make_claude_response(_valid_consolidation_json()),
        ]
        consolidator = Consolidator(anthropic_client=mock_client)
        result = consolidator.consolidate(
            [sample_tacos_meal],
            [],
            pantry_staples,
        )
        assert len(result) == 3
        assert mock_client.messages.create.call_count == 2

    def test_falls_back_after_double_failure(
        self,
        mock_client: MagicMock,
        sample_tacos_meal: ParsedMeal,
        pantry_staples: list[str],
    ):
        """Test fallback to simple consolidation after both attempts fail."""
        mock_client.messages.create.side_effect = [
            _make_claude_response("garbage"),
            _make_claude_response("still garbage"),
        ]
        consolidator = Consolidator(anthropic_client=mock_client)
        result = consolidator.consolidate(
            [sample_tacos_meal],
            [],
            pantry_staples,
        )
        # Falls back to consolidate_simple
        assert len(result) > 0
        assert all(isinstance(i, ShoppingListItem) for i in result)


class TestConsolidateApiError:
    """Tests for API error graceful degradation."""

    def test_api_error_falls_back(
        self,
        mock_client: MagicMock,
        sample_tacos_meal: ParsedMeal,
        pantry_staples: list[str],
    ):
        """Test API error gracefully falls back to simple consolidation."""
        mock_client.messages.create.side_effect = RuntimeError("API down")
        consolidator = Consolidator(anthropic_client=mock_client)
        result = consolidator.consolidate(
            [sample_tacos_meal],
            [],
            pantry_staples,
        )
        assert len(result) > 0
        assert all(isinstance(i, ShoppingListItem) for i in result)

    def test_retry_api_error_falls_back(
        self,
        mock_client: MagicMock,
        sample_tacos_meal: ParsedMeal,
        pantry_staples: list[str],
    ):
        """Test API error on retry falls back to simple consolidation."""
        mock_client.messages.create.side_effect = [
            _make_claude_response("not json"),
            RuntimeError("API down on retry"),
        ]
        consolidator = Consolidator(anthropic_client=mock_client)
        result = consolidator.consolidate(
            [sample_tacos_meal],
            [],
            pantry_staples,
        )
        # Falls back to consolidate_simple
        assert len(result) > 0
        assert all(isinstance(i, ShoppingListItem) for i in result)


class TestConsolidateInventoryOverrides:
    """Tests for pantry staple included when in inventory override list."""

    def test_pantry_staple_in_claude_prompt_when_overridden(
        self,
        mock_client: MagicMock,
        pantry_staples: list[str],
    ):
        """Test pantry staple appears in prompt when overridden."""
        meal = ParsedMeal(
            name="Test",
            servings=4,
            known_recipe=False,
            needs_confirmation=False,
            purchase_items=[
                Ingredient(
                    ingredient="salt",
                    quantity=1.0,
                    unit="tsp",
                    category=IngredientCategory.PANTRY_DRY,
                ),
            ],
            pantry_items=[],
        )
        mock_client.messages.create.return_value = _make_claude_response(
            json.dumps(
                [
                    {
                        "ingredient": "salt",
                        "quantity": 1.0,
                        "unit": "container",
                        "category": "pantry_dry",
                        "search_term": "table salt",
                        "from_meals": ["Test"],
                        "estimated_price": 2.49,
                    }
                ]
            )
        )
        consolidator = Consolidator(anthropic_client=mock_client)
        result = consolidator.consolidate(
            [meal],
            [],
            pantry_staples,
            inventory_overrides=["salt"],
        )
        assert len(result) == 1
        assert result[0].ingredient == "salt"


class TestConsolidatorGetModel:
    """Tests for model name retrieval."""

    def test_returns_correct_model(self):
        """Test model name is the expected Claude model."""
        consolidator = Consolidator()
        assert consolidator._get_model() == "claude-sonnet-4-6"


class TestConsolidatorBuildPrompt:
    """Tests for prompt building."""

    def test_prompt_includes_meal_ingredients(
        self,
        sample_tacos_meal: ParsedMeal,
        pantry_staples: list[str],
    ):
        """Test prompt includes meal ingredient text."""
        consolidator = Consolidator()
        prompt = consolidator._build_prompt(
            [sample_tacos_meal],
            [],
            pantry_staples,
            None,
        )
        assert "Chicken Tacos" in prompt
        assert "chicken thighs" in prompt

    def test_prompt_includes_pantry_staples(
        self,
        pantry_staples: list[str],
    ):
        """Test prompt includes pantry staple names."""
        consolidator = Consolidator()
        prompt = consolidator._build_prompt([], [], pantry_staples, None)
        assert "salt" in prompt
        assert "olive oil" in prompt

    def test_prompt_includes_restock_queue(
        self,
        sample_restock_queue: list[InventoryItem],
        pantry_staples: list[str],
    ):
        """Test prompt includes restock queue items."""
        consolidator = Consolidator()
        prompt = consolidator._build_prompt(
            [],
            sample_restock_queue,
            pantry_staples,
            None,
        )
        assert "Whole Milk" in prompt

    def test_prompt_includes_inventory_overrides(
        self,
        pantry_staples: list[str],
    ):
        """Test prompt includes inventory overrides."""
        consolidator = Consolidator()
        prompt = consolidator._build_prompt(
            [],
            [],
            pantry_staples,
            ["salt", "olive oil"],
        )
        assert "salt" in prompt
        assert "olive oil" in prompt


class TestMergeAndBuildHelpers:
    """Tests for static helper methods on Consolidator."""

    def test_merge_deduplicates(
        self,
        sample_tacos_meal: ParsedMeal,
        sample_tikka_meal: ParsedMeal,
    ):
        """Test _merge_meal_ingredients deduplicates same ingredient."""
        merged = Consolidator._merge_meal_ingredients(
            [sample_tacos_meal, sample_tikka_meal],
            set(),
        )
        assert "lime" in merged
        raw_qty = merged["lime"].get("quantity", 0.0)
        qty = float(raw_qty) if isinstance(raw_qty, (int, float)) else 0.0
        assert qty == 3.0

    def test_merge_excludes_pantry(self, sample_tacos_meal: ParsedMeal):
        """Test pantry items excluded from merged result."""
        # "olive oil" is in purchase_items but we pass it as pantry
        meal = ParsedMeal(
            name="Test",
            servings=4,
            known_recipe=False,
            needs_confirmation=False,
            purchase_items=[
                Ingredient(
                    ingredient="olive oil",
                    quantity=2.0,
                    unit="tbsp",
                    category=IngredientCategory.PANTRY_DRY,
                ),
                Ingredient(
                    ingredient="chicken",
                    quantity=1.0,
                    unit="lbs",
                    category=IngredientCategory.MEAT,
                ),
            ],
            pantry_items=[],
        )
        merged = Consolidator._merge_meal_ingredients(
            [meal],
            {"olive oil"},
        )
        assert "olive oil" not in merged
        assert "chicken" in merged

    def test_build_items_from_merged(self):
        """Test _build_items_from_merged produces ShoppingListItem list."""
        merged: dict[str, dict[str, object]] = {
            "chicken": {
                "ingredient": "chicken",
                "quantity": 2.0,
                "unit": "lbs",
                "category": "meat",
                "from_meals": ["Tacos"],
            },
        }
        result = Consolidator._build_items_from_merged(merged)
        assert len(result) == 1
        assert result[0].ingredient == "chicken"
        assert result[0].search_term == "chicken"

    def test_build_restock_items(self, sample_restock_queue: list[InventoryItem]):
        """Test _build_restock_items filters to low/out status."""
        result = Consolidator._build_restock_items(sample_restock_queue)
        assert len(result) == 2
        assert all("restock" in i.from_meals for i in result)
