"""Tests for grocery_butler.claude_utils module."""

from __future__ import annotations

import logging
from unittest.mock import patch

from grocery_butler.claude_utils import (
    extract_json_text,
    filter_avoided_brands,
    items_from_string,
    make_anthropic_client,
)
from grocery_butler.models import (
    BrandMatchType,
    BrandPreference,
    BrandPreferenceType,
    IngredientCategory,
    SafewayProduct,
    Unit,
)


class TestExtractJsonText:
    """Tests for extract_json_text helper."""

    def test_plain_json(self):
        """Test plain JSON passes through."""
        assert extract_json_text('{"a": 1}') == '{"a": 1}'

    def test_strips_markdown_fences(self):
        """Test markdown code fences are removed."""
        result = extract_json_text('```json\n{"a": 1}\n```')
        assert result == '{"a": 1}'

    def test_strips_whitespace(self):
        """Test surrounding whitespace is stripped."""
        assert extract_json_text("  {}\n  ") == "{}"


class TestFilterAvoidedBrands:
    """Tests for filter_avoided_brands helper."""

    def test_no_prefs_returns_all(self):
        """Test empty prefs returns all products."""
        products = [
            SafewayProduct(product_id="1", name="A", price=1.0, size=""),
            SafewayProduct(product_id="2", name="B", price=2.0, size=""),
        ]
        assert len(filter_avoided_brands(products, [])) == 2

    def test_filters_avoided(self):
        """Test avoided brands are filtered out."""
        products = [
            SafewayProduct(product_id="1", name="BadBrand Milk", price=1.0, size=""),
            SafewayProduct(product_id="2", name="Good Milk", price=2.0, size=""),
        ]
        prefs = [
            BrandPreference(
                brand="BadBrand",
                preference_type=BrandPreferenceType.AVOID,
                match_type=BrandMatchType.CATEGORY,
                match_target="dairy",
            )
        ]
        result = filter_avoided_brands(products, prefs)
        assert len(result) == 1
        assert result[0].name == "Good Milk"


class TestMakeAnthropicClient:
    """Tests for make_anthropic_client helper."""

    def test_returns_none_on_import_error(self):
        """Test returns None when anthropic import fails."""
        with patch.dict("sys.modules", {"anthropic": None}):
            result = make_anthropic_client("fake-key")
            assert result is None

    def test_logs_warning_on_failure(self, caplog):
        """Test warning is logged on failure."""
        with patch.dict("sys.modules", {"anthropic": None}):
            with caplog.at_level(logging.WARNING, logger="grocery_butler.claude_utils"):
                make_anthropic_client("fake-key")
            assert "Anthropic client unavailable" in caplog.text


class TestItemsFromString:
    """Tests for items_from_string helper."""

    def test_basic_items(self):
        """Test basic comma-separated items."""
        result = items_from_string("milk, eggs, bread")
        assert len(result) == 3
        assert result[0].ingredient == "milk"
        assert result[1].ingredient == "eggs"
        assert result[2].ingredient == "bread"

    def test_empty_string(self):
        """Test empty string returns empty list."""
        assert items_from_string("") == []

    def test_whitespace_only(self):
        """Test whitespace-only string returns empty list."""
        assert items_from_string("  ,  , ") == []

    def test_defaults(self):
        """Test default values are set correctly."""
        result = items_from_string("milk")
        assert len(result) == 1
        item = result[0]
        assert item.quantity == 1.0
        assert item.unit == Unit.EACH
        assert item.category == IngredientCategory.OTHER
        assert item.search_term == "milk"
        assert item.from_meals == ["manual"]

    def test_lowercases_names(self):
        """Test item names are lowercased."""
        result = items_from_string("MILK, Eggs")
        assert result[0].ingredient == "milk"
        assert result[1].ingredient == "eggs"
