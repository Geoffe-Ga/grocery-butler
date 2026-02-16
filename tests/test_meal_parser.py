"""Tests for grocery_butler.meal_parser module."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from grocery_butler.meal_parser import (
    MealParser,
    _build_stub_meal,
    _extract_json_text,
    _parse_ingredient,
    _parse_meal_from_dict,
    _scale_ingredients,
)
from grocery_butler.models import Ingredient, IngredientCategory, ParsedMeal
from grocery_butler.recipe_store import RecipeStore, normalize_recipe_name

if TYPE_CHECKING:
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a temporary database path.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        Path string for a fresh database.
    """
    return str(tmp_path / "test.db")


@pytest.fixture()
def store(db_path: str) -> RecipeStore:
    """Return a RecipeStore backed by a fresh temporary database.

    Args:
        db_path: Path to temporary database.

    Returns:
        Initialized RecipeStore instance.
    """
    return RecipeStore(db_path)


@pytest.fixture()
def sample_meal() -> ParsedMeal:
    """Return a sample ParsedMeal for testing.

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
def mock_client() -> MagicMock:
    """Return a mock Anthropic client.

    Returns:
        MagicMock with messages.create() configured.
    """
    client = MagicMock()
    return client


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


def _valid_decomposition_json(
    name: str = "Chicken Tikka Masala",
    servings: int = 4,
) -> str:
    """Return valid JSON for a meal decomposition response.

    Args:
        name: Meal name.
        servings: Number of servings.

    Returns:
        JSON string representing a decomposed meal.
    """
    return json.dumps(
        [
            {
                "name": name,
                "servings": servings,
                "known_recipe": False,
                "needs_confirmation": True,
                "purchase_items": [
                    {
                        "ingredient": "chicken thighs, boneless",
                        "quantity": 2.0,
                        "unit": "lbs",
                        "category": "meat",
                        "notes": "",
                        "is_pantry_item": False,
                    },
                    {
                        "ingredient": "tikka masala sauce",
                        "quantity": 1.0,
                        "unit": "jar",
                        "category": "pantry_dry",
                        "notes": "",
                        "is_pantry_item": False,
                    },
                ],
                "pantry_items": [
                    {
                        "ingredient": "olive oil",
                        "quantity": 2.0,
                        "unit": "tbsp",
                        "category": "pantry_dry",
                        "notes": "",
                        "is_pantry_item": True,
                    },
                ],
            }
        ]
    )


# ---------------------------------------------------------------------------
# Helper function tests
# ---------------------------------------------------------------------------


class TestBuildStubMeal:
    """Tests for _build_stub_meal helper."""

    def test_returns_parsed_meal(self):
        """Test stub returns a valid ParsedMeal."""
        result = _build_stub_meal("Test Meal", 4)
        assert isinstance(result, ParsedMeal)
        assert result.name == "Test Meal"
        assert result.servings == 4

    def test_stub_flags(self):
        """Test stub has correct flags set."""
        result = _build_stub_meal("Test Meal", 2)
        assert result.known_recipe is False
        assert result.needs_confirmation is True
        assert result.purchase_items == []
        assert result.pantry_items == []


class TestExtractJsonText:
    """Tests for _extract_json_text helper."""

    def test_plain_json(self):
        """Test plain JSON passes through."""
        text = '{"key": "value"}'
        assert _extract_json_text(text) == text

    def test_strips_markdown_fences(self):
        """Test markdown code fences are stripped."""
        text = '```json\n{"key": "value"}\n```'
        assert _extract_json_text(text) == '{"key": "value"}'

    def test_strips_plain_fences(self):
        """Test plain code fences are stripped."""
        text = '```\n{"key": "value"}\n```'
        assert _extract_json_text(text) == '{"key": "value"}'

    def test_strips_whitespace(self):
        """Test surrounding whitespace is stripped."""
        text = '  {"key": "value"}  '
        assert _extract_json_text(text) == '{"key": "value"}'


class TestParseIngredient:
    """Tests for _parse_ingredient helper."""

    def test_parses_complete_dict(self):
        """Test parsing a complete ingredient dictionary."""
        data: dict[str, object] = {
            "ingredient": "chicken thighs",
            "quantity": 2.0,
            "unit": "lbs",
            "category": "meat",
            "notes": "boneless",
            "is_pantry_item": False,
        }
        result = _parse_ingredient(data)
        assert result.ingredient == "chicken thighs"
        assert result.quantity == 2.0
        assert result.unit == "lbs"
        assert result.category == IngredientCategory.MEAT
        assert result.notes == "boneless"
        assert result.is_pantry_item is False

    def test_defaults_for_missing_fields(self):
        """Test default values when fields are missing."""
        result = _parse_ingredient({})
        assert result.ingredient == ""
        assert result.quantity == 0.0
        assert result.unit == ""
        assert result.category == IngredientCategory.OTHER
        assert result.notes == ""
        assert result.is_pantry_item is False


class TestParseMealFromDict:
    """Tests for _parse_meal_from_dict helper."""

    def test_parses_complete_dict(self):
        """Test parsing a complete meal dictionary."""
        data = json.loads(_valid_decomposition_json())
        result = _parse_meal_from_dict(data[0])
        assert result.name == "Chicken Tikka Masala"
        assert result.servings == 4
        assert result.known_recipe is False
        assert result.needs_confirmation is True
        assert len(result.purchase_items) == 2
        assert len(result.pantry_items) == 1

    def test_defaults_for_missing_fields(self):
        """Test default values when fields are missing."""
        result = _parse_meal_from_dict({})
        assert result.name == ""
        assert result.servings == 4
        assert result.purchase_items == []
        assert result.pantry_items == []

    def test_handles_non_list_items(self):
        """Test non-list purchase/pantry items default to empty."""
        data: dict[str, object] = {
            "name": "Test",
            "servings": 4,
            "purchase_items": "not a list",
            "pantry_items": 42,
        }
        result = _parse_meal_from_dict(data)
        assert result.purchase_items == []
        assert result.pantry_items == []


class TestScaleIngredients:
    """Tests for _scale_ingredients helper."""

    def test_scales_up(self):
        """Test scaling from 4 to 8 servings doubles quantities."""
        items = [
            Ingredient(
                ingredient="chicken",
                quantity=2.0,
                unit="lbs",
                category=IngredientCategory.MEAT,
            ),
        ]
        result = _scale_ingredients(items, 4, 8)
        assert result[0].quantity == 4.0

    def test_scales_down(self):
        """Test scaling from 4 to 2 servings halves quantities."""
        items = [
            Ingredient(
                ingredient="chicken",
                quantity=2.0,
                unit="lbs",
                category=IngredientCategory.MEAT,
            ),
        ]
        result = _scale_ingredients(items, 4, 2)
        assert result[0].quantity == 1.0

    def test_zero_original_servings(self):
        """Test zero original servings returns items unchanged."""
        items = [
            Ingredient(
                ingredient="chicken",
                quantity=2.0,
                unit="lbs",
                category=IngredientCategory.MEAT,
            ),
        ]
        result = _scale_ingredients(items, 0, 4)
        assert result[0].quantity == 2.0

    def test_zero_target_servings(self):
        """Test zero target servings returns items unchanged."""
        items = [
            Ingredient(
                ingredient="chicken",
                quantity=2.0,
                unit="lbs",
                category=IngredientCategory.MEAT,
            ),
        ]
        result = _scale_ingredients(items, 4, 0)
        assert result[0].quantity == 2.0

    def test_empty_list(self):
        """Test empty ingredient list returns empty."""
        assert _scale_ingredients([], 4, 8) == []

    def test_preserves_other_fields(self):
        """Test scaling only affects quantity."""
        items = [
            Ingredient(
                ingredient="chicken",
                quantity=2.0,
                unit="lbs",
                category=IngredientCategory.MEAT,
                notes="boneless",
                is_pantry_item=False,
            ),
        ]
        result = _scale_ingredients(items, 4, 8)
        assert result[0].ingredient == "chicken"
        assert result[0].unit == "lbs"
        assert result[0].category == IngredientCategory.MEAT
        assert result[0].notes == "boneless"


# ---------------------------------------------------------------------------
# MealParser tests
# ---------------------------------------------------------------------------


class TestMealParserKnownRecipe:
    """Tests for known recipe resolution."""

    def test_returns_stored_recipe(self, store: RecipeStore, sample_meal: ParsedMeal):
        """Test known recipe is returned without Claude call."""
        store.save_recipe(sample_meal)
        parser = MealParser(store)
        results = parser.parse_meals(["Chicken Tacos"])

        assert len(results) == 1
        assert results[0].name == "Chicken Tacos"
        assert results[0].known_recipe is True
        assert results[0].needs_confirmation is False

    def test_no_claude_call_for_known(
        self, store: RecipeStore, sample_meal: ParsedMeal, mock_client: MagicMock
    ):
        """Test Claude is not called for known recipes."""
        store.save_recipe(sample_meal)
        parser = MealParser(store, anthropic_client=mock_client)
        parser.parse_meals(["Chicken Tacos"])

        mock_client.messages.create.assert_not_called()

    def test_case_insensitive_lookup(self, store: RecipeStore, sample_meal: ParsedMeal):
        """Test recipe lookup is case insensitive."""
        store.save_recipe(sample_meal)
        parser = MealParser(store)
        results = parser.parse_meals(["CHICKEN TACOS"])

        assert len(results) == 1
        assert results[0].name == "Chicken Tacos"

    def test_substring_match(self, store: RecipeStore, sample_meal: ParsedMeal):
        """Test substring matching finds stored recipes."""
        store.save_recipe(sample_meal)
        parser = MealParser(store)
        results = parser.parse_meals(["chicken"])

        assert len(results) == 1
        assert results[0].name == "Chicken Tacos"


class TestMealParserUnknownRecipe:
    """Tests for unknown recipe decomposition via Claude."""

    def test_calls_claude_for_unknown(self, store: RecipeStore, mock_client: MagicMock):
        """Test Claude is called for unknown meals."""
        mock_client.messages.create.return_value = _make_claude_response(
            _valid_decomposition_json()
        )
        parser = MealParser(store, anthropic_client=mock_client)
        results = parser.parse_meals(["Chicken Tikka Masala"])

        assert len(results) == 1
        assert results[0].needs_confirmation is True
        assert mock_client.messages.create.called

    def test_unknown_recipe_has_ingredients(
        self, store: RecipeStore, mock_client: MagicMock
    ):
        """Test Claude-parsed meal has structured ingredients."""
        mock_client.messages.create.return_value = _make_claude_response(
            _valid_decomposition_json()
        )
        parser = MealParser(store, anthropic_client=mock_client)
        results = parser.parse_meals(["Chicken Tikka Masala"])

        meal = results[0]
        assert len(meal.purchase_items) == 2
        assert len(meal.pantry_items) == 1
        assert meal.purchase_items[0].ingredient == "chicken thighs, boneless"

    def test_unknown_recipe_flags(self, store: RecipeStore, mock_client: MagicMock):
        """Test unknown recipe has correct flags."""
        mock_client.messages.create.return_value = _make_claude_response(
            _valid_decomposition_json()
        )
        parser = MealParser(store, anthropic_client=mock_client)
        results = parser.parse_meals(["Chicken Tikka Masala"])

        assert results[0].known_recipe is False
        assert results[0].needs_confirmation is True


class TestClaudeResponseParsing:
    """Tests for Claude response JSON parsing."""

    def test_parses_valid_json(self, store: RecipeStore, mock_client: MagicMock):
        """Test valid JSON response is parsed into ParsedMeal."""
        mock_client.messages.create.return_value = _make_claude_response(
            _valid_decomposition_json()
        )
        parser = MealParser(store, anthropic_client=mock_client)
        results = parser.parse_meals(["Chicken Tikka Masala"])

        assert isinstance(results[0], ParsedMeal)
        assert results[0].name == "Chicken Tikka Masala"
        assert results[0].servings == 4

    def test_parses_single_object_response(
        self, store: RecipeStore, mock_client: MagicMock
    ):
        """Test Claude returning a single object instead of array."""
        single_obj = json.dumps(
            {
                "name": "Simple Pasta",
                "servings": 4,
                "known_recipe": False,
                "needs_confirmation": True,
                "purchase_items": [
                    {
                        "ingredient": "spaghetti",
                        "quantity": 1.0,
                        "unit": "lbs",
                        "category": "pantry_dry",
                        "notes": "",
                        "is_pantry_item": False,
                    },
                ],
                "pantry_items": [],
            }
        )
        mock_client.messages.create.return_value = _make_claude_response(single_obj)
        parser = MealParser(store, anthropic_client=mock_client)
        results = parser.parse_meals(["Simple Pasta"])

        assert results[0].name == "Simple Pasta"
        assert len(results[0].purchase_items) == 1

    def test_parses_response_with_markdown_fences(
        self, store: RecipeStore, mock_client: MagicMock
    ):
        """Test markdown fences are stripped from response."""
        fenced = f"```json\n{_valid_decomposition_json()}\n```"
        mock_client.messages.create.return_value = _make_claude_response(fenced)
        parser = MealParser(store, anthropic_client=mock_client)
        results = parser.parse_meals(["Chicken Tikka Masala"])

        assert results[0].name == "Chicken Tikka Masala"

    def test_ingredient_categories_validated(
        self, store: RecipeStore, mock_client: MagicMock
    ):
        """Test ingredient categories are proper enums."""
        mock_client.messages.create.return_value = _make_claude_response(
            _valid_decomposition_json()
        )
        parser = MealParser(store, anthropic_client=mock_client)
        results = parser.parse_meals(["Chicken Tikka Masala"])

        for item in results[0].purchase_items:
            assert isinstance(item.category, IngredientCategory)


class TestClaudeInvalidJsonRetry:
    """Tests for retry on invalid JSON."""

    def test_retries_on_invalid_json(self, store: RecipeStore, mock_client: MagicMock):
        """Test invalid JSON triggers a retry with valid response."""
        mock_client.messages.create.side_effect = [
            # First call: fuzzy match (no recipes, skipped)
            # First decomposition call: garbage
            _make_claude_response("This is not JSON at all!!!"),
            # Retry call: valid JSON
            _make_claude_response(_valid_decomposition_json()),
        ]
        parser = MealParser(store, anthropic_client=mock_client)
        results = parser.parse_meals(["Chicken Tikka Masala"])

        assert results[0].name == "Chicken Tikka Masala"
        assert mock_client.messages.create.call_count == 2

    def test_returns_stub_after_double_failure(
        self, store: RecipeStore, mock_client: MagicMock
    ):
        """Test stub returned when both attempts fail."""
        mock_client.messages.create.side_effect = [
            _make_claude_response("garbage"),
            _make_claude_response("still garbage"),
        ]
        parser = MealParser(store, anthropic_client=mock_client)
        results = parser.parse_meals(["Chicken Tikka Masala"])

        assert results[0].needs_confirmation is True
        assert results[0].purchase_items == []


class TestRecipeNameNormalization:
    """Tests for recipe name normalization in meal parsing."""

    def test_normalize_strips_articles(self):
        """Test articles are stripped from recipe names."""
        assert normalize_recipe_name("The Best Tacos") == "best tacos"
        assert normalize_recipe_name("A Simple Pasta") == "simple pasta"

    def test_normalize_possessives(self):
        """Test possessives are normalized."""
        assert normalize_recipe_name("Mom's Tacos") == "moms tacos"

    def test_normalize_case(self):
        """Test case is normalized."""
        assert normalize_recipe_name("CHICKEN TACOS") == "chicken tacos"

    def test_normalize_punctuation(self):
        """Test punctuation is removed."""
        assert normalize_recipe_name("mac & cheese!") == "mac cheese"


class TestFuzzyMatching:
    """Tests for Claude-powered fuzzy recipe matching."""

    def test_fuzzy_match_finds_recipe(
        self, store: RecipeStore, sample_meal: ParsedMeal, mock_client: MagicMock
    ):
        """Test fuzzy matching finds a stored recipe via Claude."""
        store.save_recipe(sample_meal)
        match_response = json.dumps({"match": "Chicken Tacos", "confidence": 0.95})
        mock_client.messages.create.return_value = _make_claude_response(match_response)
        parser = MealParser(store, anthropic_client=mock_client)
        results = parser.parse_meals(["tacos with chicken"])

        assert results[0].name == "Chicken Tacos"
        assert results[0].known_recipe is True

    def test_fuzzy_match_low_confidence_falls_through(
        self, store: RecipeStore, sample_meal: ParsedMeal, mock_client: MagicMock
    ):
        """Test low confidence fuzzy match falls through to decomposition."""
        store.save_recipe(sample_meal)
        match_response = json.dumps({"match": "Chicken Tacos", "confidence": 0.3})
        decomp_response = _valid_decomposition_json(
            name="Tacos With Chicken",
        )
        mock_client.messages.create.side_effect = [
            _make_claude_response(match_response),
            _make_claude_response(decomp_response),
        ]
        parser = MealParser(store, anthropic_client=mock_client)
        results = parser.parse_meals(["tacos with chicken"])

        assert results[0].needs_confirmation is True

    def test_fuzzy_match_null_match(
        self, store: RecipeStore, sample_meal: ParsedMeal, mock_client: MagicMock
    ):
        """Test null match falls through to decomposition."""
        store.save_recipe(sample_meal)
        match_response = json.dumps({"match": None, "confidence": 0.0})
        decomp_response = _valid_decomposition_json(
            name="Something Completely Different",
        )
        mock_client.messages.create.side_effect = [
            _make_claude_response(match_response),
            _make_claude_response(decomp_response),
        ]
        parser = MealParser(store, anthropic_client=mock_client)
        results = parser.parse_meals(["something completely different"])

        assert results[0].needs_confirmation is True

    def test_fuzzy_match_no_recipes(self, store: RecipeStore, mock_client: MagicMock):
        """Test fuzzy matching is skipped when no recipes exist."""
        decomp_response = _valid_decomposition_json()
        mock_client.messages.create.return_value = _make_claude_response(
            decomp_response
        )
        parser = MealParser(store, anthropic_client=mock_client)
        parsed = parser.parse_meals(["Chicken Tikka Masala"])

        # Only decomposition call, no fuzzy match call
        assert mock_client.messages.create.call_count == 1
        assert parsed[0].name == "Chicken Tikka Masala"

    def test_fuzzy_match_invalid_json(
        self, store: RecipeStore, sample_meal: ParsedMeal, mock_client: MagicMock
    ):
        """Test invalid fuzzy match response falls through."""
        store.save_recipe(sample_meal)
        mock_client.messages.create.side_effect = [
            _make_claude_response("not json"),
            _make_claude_response(_valid_decomposition_json(name="Unknown Meal")),
        ]
        parser = MealParser(store, anthropic_client=mock_client)
        results = parser.parse_meals(["unknown meal"])

        assert results[0].needs_confirmation is True


class TestServingSizeAdjustment:
    """Tests for serving size adjustment."""

    def test_scales_up_servings(self, store: RecipeStore, sample_meal: ParsedMeal):
        """Test scaling up from 4 to 8 servings."""
        store.save_recipe(sample_meal)
        parser = MealParser(store)
        results = parser.parse_meals(["Chicken Tacos"], servings=8)

        assert results[0].servings == 8
        chicken = next(
            i for i in results[0].purchase_items if "chicken" in i.ingredient
        )
        assert chicken.quantity == 4.0

    def test_scales_down_servings(self, store: RecipeStore, sample_meal: ParsedMeal):
        """Test scaling down from 4 to 2 servings."""
        store.save_recipe(sample_meal)
        parser = MealParser(store)
        results = parser.parse_meals(["Chicken Tacos"], servings=2)

        assert results[0].servings == 2
        chicken = next(
            i for i in results[0].purchase_items if "chicken" in i.ingredient
        )
        assert chicken.quantity == 1.0

    def test_no_scaling_when_same_servings(
        self, store: RecipeStore, sample_meal: ParsedMeal
    ):
        """Test no scaling when target equals default."""
        store.save_recipe(sample_meal)
        parser = MealParser(store)
        results = parser.parse_meals(["Chicken Tacos"], servings=4)

        assert results[0].servings == 4
        chicken = next(
            i for i in results[0].purchase_items if "chicken" in i.ingredient
        )
        assert chicken.quantity == 2.0

    def test_scales_pantry_items_too(self, store: RecipeStore, sample_meal: ParsedMeal):
        """Test pantry items are also scaled."""
        store.save_recipe(sample_meal)
        parser = MealParser(store)
        results = parser.parse_meals(["Chicken Tacos"], servings=8)

        olive_oil = results[0].pantry_items[0]
        assert olive_oil.quantity == 4.0

    def test_default_servings_from_config(
        self, store: RecipeStore, sample_meal: ParsedMeal
    ):
        """Test default servings comes from config when provided."""
        store.save_recipe(sample_meal)
        config = MagicMock()
        config.default_servings = 4
        config.default_units = "imperial"
        parser = MealParser(store, config=config)
        results = parser.parse_meals(["Chicken Tacos"])

        assert results[0].servings == 4

    def test_default_servings_from_store(
        self, store: RecipeStore, sample_meal: ParsedMeal
    ):
        """Test default servings from store preference."""
        store.save_recipe(sample_meal)
        store.set_preference("default_servings", "4")
        parser = MealParser(store)
        results = parser.parse_meals(["Chicken Tacos"])

        assert results[0].servings == 4

    def test_default_servings_invalid_pref(
        self, store: RecipeStore, sample_meal: ParsedMeal
    ):
        """Test fallback when store preference is invalid."""
        store.save_recipe(sample_meal)
        store.set_preference("default_servings", "not_a_number")
        parser = MealParser(store)
        results = parser.parse_meals(["Chicken Tacos"])

        # Falls back to 4
        assert results[0].servings == 4


class TestGracefulDegradation:
    """Tests for graceful degradation without anthropic_client."""

    def test_no_client_returns_stub(self, store: RecipeStore):
        """Test stub returned when no anthropic_client provided."""
        parser = MealParser(store)
        results = parser.parse_meals(["Unknown Fancy Dish"])

        assert len(results) == 1
        assert results[0].name == "Unknown Fancy Dish"
        assert results[0].known_recipe is False
        assert results[0].needs_confirmation is True
        assert results[0].purchase_items == []
        assert results[0].pantry_items == []

    def test_no_client_still_finds_known(
        self, store: RecipeStore, sample_meal: ParsedMeal
    ):
        """Test known recipes still work without client."""
        store.save_recipe(sample_meal)
        parser = MealParser(store)
        results = parser.parse_meals(["Chicken Tacos"])

        assert results[0].name == "Chicken Tacos"
        assert results[0].known_recipe is True

    def test_no_client_multiple_meals(self, store: RecipeStore):
        """Test multiple unknown meals without client."""
        parser = MealParser(store)
        results = parser.parse_meals(["Dish A", "Dish B"])

        assert len(results) == 2
        assert all(r.needs_confirmation for r in results)

    def test_api_error_returns_stub(self, store: RecipeStore, mock_client: MagicMock):
        """Test API error gracefully returns stub."""
        mock_client.messages.create.side_effect = RuntimeError("API down")
        parser = MealParser(store, anthropic_client=mock_client)
        results = parser.parse_meals(["Unknown Meal"])

        assert results[0].needs_confirmation is True
        assert results[0].purchase_items == []


class TestEmptyMealList:
    """Tests for empty meal list handling."""

    def test_empty_list_returns_empty(self, store: RecipeStore):
        """Test empty meal list returns empty list."""
        parser = MealParser(store)
        results = parser.parse_meals([])
        assert results == []

    def test_empty_list_no_claude_call(
        self, store: RecipeStore, mock_client: MagicMock
    ):
        """Test no Claude call for empty list."""
        parser = MealParser(store, anthropic_client=mock_client)
        parser.parse_meals([])
        mock_client.messages.create.assert_not_called()


class TestSaveParsedMeal:
    """Tests for save_parsed_meal delegation."""

    def test_delegates_to_recipe_store(
        self, store: RecipeStore, sample_meal: ParsedMeal
    ):
        """Test save_parsed_meal delegates to recipe_store.save_recipe."""
        parser = MealParser(store)
        parser.save_parsed_meal(sample_meal)

        result = store.get_recipe("Chicken Tacos")
        assert result is not None
        assert result.name == "Chicken Tacos"

    def test_save_with_mock_store(self, sample_meal: ParsedMeal):
        """Test save_parsed_meal calls save_recipe on the store."""
        mock_store = MagicMock(spec=RecipeStore)
        parser = MealParser(mock_store)
        parser.save_parsed_meal(sample_meal)

        mock_store.save_recipe.assert_called_once_with(sample_meal)


class TestMultipleMeals:
    """Tests for parsing multiple meals at once."""

    def test_mix_known_and_unknown(
        self,
        store: RecipeStore,
        sample_meal: ParsedMeal,
        mock_client: MagicMock,
    ):
        """Test parsing a mix of known and unknown meals."""
        store.save_recipe(sample_meal)
        mock_client.messages.create.return_value = _make_claude_response(
            _valid_decomposition_json()
        )
        parser = MealParser(store, anthropic_client=mock_client)
        results = parser.parse_meals(["Chicken Tacos", "Chicken Tikka Masala"])

        assert len(results) == 2
        assert results[0].name == "Chicken Tacos"
        assert results[0].known_recipe is True
        assert results[1].needs_confirmation is True


class TestConfigurationHelpers:
    """Tests for configuration helper methods."""

    def test_get_units_from_config(self, store: RecipeStore):
        """Test units from config override store preference."""
        config = MagicMock()
        config.default_servings = 4
        config.default_units = "metric"
        parser = MealParser(store, config=config)
        assert parser._get_units() == "metric"

    def test_get_units_from_store(self, store: RecipeStore):
        """Test units from store preference."""
        parser = MealParser(store)
        assert parser._get_units() == "imperial"

    def test_get_dietary_restrictions(self, store: RecipeStore):
        """Test dietary restrictions from store preference."""
        store.set_preference("dietary_restrictions", "vegetarian")
        parser = MealParser(store)
        assert parser._get_dietary_restrictions() == "vegetarian"

    def test_get_dietary_restrictions_empty(self, store: RecipeStore):
        """Test empty dietary restrictions."""
        parser = MealParser(store)
        assert parser._get_dietary_restrictions() == ""

    def test_get_model(self, store: RecipeStore):
        """Test model name is returned."""
        parser = MealParser(store)
        assert "claude" in parser._get_model()


class TestDecompositionPromptBuilding:
    """Tests for prompt building."""

    def test_prompt_includes_meal_name(self, store: RecipeStore):
        """Test decomposition prompt includes the meal name."""
        parser = MealParser(store)
        prompt = parser._build_decomposition_prompt("Chicken Tikka Masala", 4)
        assert "Chicken Tikka Masala" in prompt

    def test_prompt_includes_pantry_staples(self, store: RecipeStore):
        """Test prompt includes pantry staple names."""
        parser = MealParser(store)
        prompt = parser._build_decomposition_prompt("Test Meal", 4)
        assert "salt" in prompt
        assert "olive oil" in prompt

    def test_prompt_includes_servings(self, store: RecipeStore):
        """Test prompt includes the servings count."""
        parser = MealParser(store)
        prompt = parser._build_decomposition_prompt("Test Meal", 6)
        assert "6" in prompt


class TestExtractMealFromData:
    """Tests for _extract_meal_from_data static method."""

    def test_array_with_matching_name(self):
        """Test extracting from array by matching name."""
        data = json.loads(_valid_decomposition_json())
        result = MealParser._extract_meal_from_data(data, "Chicken Tikka Masala")
        assert result is not None
        assert result.name == "Chicken Tikka Masala"

    def test_array_first_item_fallback(self):
        """Test first item returned when no name match."""
        data = json.loads(_valid_decomposition_json())
        result = MealParser._extract_meal_from_data(data, "Nonexistent Meal")
        assert result is not None
        assert result.name == "Chicken Tikka Masala"

    def test_empty_array(self):
        """Test empty array returns None."""
        result = MealParser._extract_meal_from_data([], "Test")
        assert result is None

    def test_dict_input(self):
        """Test single dict is parsed directly."""
        data = json.loads(_valid_decomposition_json())[0]
        result = MealParser._extract_meal_from_data(data, "Chicken Tikka Masala")
        assert result is not None

    def test_non_dict_non_list(self):
        """Test unexpected type returns None."""
        result = MealParser._extract_meal_from_data("just a string", "Test")
        assert result is None

    def test_array_with_non_dict_items(self):
        """Test array containing non-dict items is skipped."""
        data = ["not a dict", 42, None]
        result = MealParser._extract_meal_from_data(data, "Test")
        assert result is None


class TestRetryOnApiError:
    """Tests for retry mechanism on API errors."""

    def test_retry_api_error_returns_stub(
        self, store: RecipeStore, mock_client: MagicMock
    ):
        """Test API error on retry returns stub."""
        mock_client.messages.create.side_effect = [
            _make_claude_response("not json"),
            RuntimeError("API down on retry"),
        ]
        parser = MealParser(store, anthropic_client=mock_client)
        results = parser.parse_meals(["Unknown Meal"])

        assert results[0].needs_confirmation is True
        assert results[0].purchase_items == []


class TestFuzzyMatchWithNoClient:
    """Tests for fuzzy matching behavior without a client."""

    def test_no_client_skips_fuzzy_match(
        self, store: RecipeStore, sample_meal: ParsedMeal
    ):
        """Test fuzzy matching is skipped when no client."""
        store.save_recipe(sample_meal)
        parser = MealParser(store)
        # "tacos with chicken" won't match substring "chicken tacos"
        # but store.find_recipe might still match via substring
        result = parser._try_fuzzy_match("something completely random")
        assert result is None
