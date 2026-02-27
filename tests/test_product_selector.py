"""Tests for grocery_butler.product_selector module."""

from __future__ import annotations

import json
from unittest.mock import MagicMock, patch

from grocery_butler.models import (
    BrandMatchType,
    BrandPreference,
    BrandPreferenceType,
    IngredientCategory,
    SafewayProduct,
    ShoppingListItem,
)
from grocery_butler.product_selector import (
    ProductSelector,
    _extract_json_text,
    _filter_avoided_brands,
    _format_brand_preferences,
    _get_preferred_brands,
    _heuristic_select,
    _parse_selection_response,
    _product_to_dict,
)

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


def _make_item(
    ingredient: str = "whole milk",
    quantity: float = 1.0,
    unit: str = "gal",
    category: IngredientCategory = IngredientCategory.DAIRY,
    search_term: str = "whole milk gallon",
) -> ShoppingListItem:
    """Create a test ShoppingListItem.

    Args:
        ingredient: Ingredient name.
        quantity: Amount needed.
        unit: Unit of measurement.
        category: Grocery category.
        search_term: Safeway search term.

    Returns:
        A ShoppingListItem for testing.
    """
    return ShoppingListItem(
        ingredient=ingredient,
        quantity=quantity,
        unit=unit,
        category=category,
        search_term=search_term,
        from_meals=["Test Meal"],
    )


def _make_product(
    product_id: str = "UPC001",
    name: str = "Organic Whole Milk",
    price: float = 5.99,
    size: str = "1 gal",
    in_stock: bool = True,
) -> SafewayProduct:
    """Create a test SafewayProduct.

    Args:
        product_id: Product identifier.
        name: Product name.
        price: Product price.
        size: Product size.
        in_stock: Availability.

    Returns:
        A SafewayProduct for testing.
    """
    return SafewayProduct(
        product_id=product_id,
        name=name,
        price=price,
        size=size,
        in_stock=in_stock,
    )


def _make_pref(
    brand: str = "Organic Valley",
    pref_type: BrandPreferenceType = BrandPreferenceType.PREFERRED,
    match_target: str = "milk",
    match_type: BrandMatchType = BrandMatchType.INGREDIENT,
) -> BrandPreference:
    """Create a test BrandPreference.

    Args:
        brand: Brand name.
        pref_type: Preferred or avoid.
        match_target: Ingredient or category name.
        match_type: Whether this targets an ingredient or category.

    Returns:
        A BrandPreference for testing.
    """
    return BrandPreference(
        match_target=match_target,
        match_type=match_type,
        brand=brand,
        preference_type=pref_type,
    )


# ------------------------------------------------------------------
# Tests: _filter_avoided_brands
# ------------------------------------------------------------------


class TestFilterAvoidedBrands:
    """Tests for _filter_avoided_brands."""

    def test_no_prefs_returns_all(self) -> None:
        """Test all products returned when no preferences."""
        products = [_make_product(name="Brand A"), _make_product(name="Brand B")]
        result = _filter_avoided_brands(products, [])
        assert len(result) == 2

    def test_filters_avoided_brand(self) -> None:
        """Test that avoided brand products are removed."""
        products = [
            _make_product(product_id="1", name="Great Value Milk"),
            _make_product(product_id="2", name="Organic Valley Milk"),
        ]
        prefs = [_make_pref("Great Value", BrandPreferenceType.AVOID)]
        result = _filter_avoided_brands(products, prefs)

        assert len(result) == 1
        assert result[0].product_id == "2"

    def test_case_insensitive_matching(self) -> None:
        """Test that brand matching is case-insensitive."""
        products = [_make_product(name="GREAT VALUE Milk")]
        prefs = [_make_pref("great value", BrandPreferenceType.AVOID)]
        result = _filter_avoided_brands(products, prefs)
        assert result == []

    def test_preferred_brands_not_filtered(self) -> None:
        """Test that preferred brands are NOT filtered out."""
        products = [_make_product(name="Organic Valley Milk")]
        prefs = [_make_pref("Organic Valley", BrandPreferenceType.PREFERRED)]
        result = _filter_avoided_brands(products, prefs)
        assert len(result) == 1


# ------------------------------------------------------------------
# Tests: _get_preferred_brands
# ------------------------------------------------------------------


class TestGetPreferredBrands:
    """Tests for _get_preferred_brands."""

    def test_empty_prefs(self) -> None:
        """Test empty preferences returns empty set."""
        assert _get_preferred_brands([]) == set()

    def test_extracts_preferred_only(self) -> None:
        """Test only preferred brands are extracted."""
        prefs = [
            _make_pref("Organic Valley", BrandPreferenceType.PREFERRED),
            _make_pref("Great Value", BrandPreferenceType.AVOID),
        ]
        result = _get_preferred_brands(prefs)
        assert result == {"organic valley"}

    def test_lowercased(self) -> None:
        """Test that brand names are lowercased."""
        prefs = [_make_pref("Organic Valley", BrandPreferenceType.PREFERRED)]
        result = _get_preferred_brands(prefs)
        assert "organic valley" in result


# ------------------------------------------------------------------
# Tests: _heuristic_select
# ------------------------------------------------------------------


class TestHeuristicSelect:
    """Tests for _heuristic_select."""

    def test_cheapest_when_no_prefs(self) -> None:
        """Test cheapest product selected without preferences."""
        products = [
            _make_product(product_id="1", price=5.99),
            _make_product(product_id="2", price=3.49),
        ]
        result = _heuristic_select(products, set())
        assert result.product_id == "2"

    def test_prefers_in_stock(self) -> None:
        """Test in-stock products preferred over out-of-stock."""
        products = [
            _make_product(product_id="1", price=2.99, in_stock=False),
            _make_product(product_id="2", price=5.99, in_stock=True),
        ]
        result = _heuristic_select(products, set())
        assert result.product_id == "2"

    def test_prefers_preferred_brand(self) -> None:
        """Test preferred brand chosen over cheaper generic."""
        products = [
            _make_product(product_id="1", name="Generic Milk", price=2.99),
            _make_product(product_id="2", name="Organic Valley Milk", price=5.99),
        ]
        result = _heuristic_select(products, {"organic valley"})
        assert result.product_id == "2"

    def test_cheapest_preferred_brand(self) -> None:
        """Test cheapest among preferred brands is selected."""
        products = [
            _make_product(product_id="1", name="Organic Valley 1gal", price=6.99),
            _make_product(product_id="2", name="Organic Valley 0.5gal", price=4.49),
        ]
        result = _heuristic_select(products, {"organic valley"})
        assert result.product_id == "2"

    def test_falls_back_to_out_of_stock(self) -> None:
        """Test fallback to out-of-stock when all are unavailable."""
        products = [
            _make_product(product_id="1", price=5.99, in_stock=False),
            _make_product(product_id="2", price=3.49, in_stock=False),
        ]
        result = _heuristic_select(products, set())
        assert result.product_id == "2"


# ------------------------------------------------------------------
# Tests: _product_to_dict
# ------------------------------------------------------------------


class TestProductToDict:
    """Tests for _product_to_dict."""

    def test_converts_product(self) -> None:
        """Test product is converted to dict with all fields."""
        product = SafewayProduct(
            product_id="UPC001",
            name="Organic Whole Milk",
            price=5.99,
            unit_price=0.05,
            size="1 gal",
            in_stock=True,
        )
        result = _product_to_dict(product)

        assert result["product_id"] == "UPC001"
        assert result["name"] == "Organic Whole Milk"
        assert result["price"] == 5.99
        assert result["unit_price"] == 0.05
        assert result["size"] == "1 gal"
        assert result["in_stock"] is True


# ------------------------------------------------------------------
# Tests: _format_brand_preferences
# ------------------------------------------------------------------


class TestFormatBrandPreferences:
    """Tests for _format_brand_preferences."""

    def test_no_prefs(self) -> None:
        """Test empty preferences message."""
        assert _format_brand_preferences([]) == "No brand preferences set."

    def test_formats_prefs(self) -> None:
        """Test preferences are formatted correctly."""
        prefs = [
            _make_pref("Organic Valley", BrandPreferenceType.PREFERRED),
            _make_pref("Great Value", BrandPreferenceType.AVOID),
        ]
        result = _format_brand_preferences(prefs)
        assert "PREFER: Organic Valley" in result
        assert "AVOID: Great Value" in result


# ------------------------------------------------------------------
# Tests: _parse_selection_response
# ------------------------------------------------------------------


class TestParseSelectionResponse:
    """Tests for _parse_selection_response."""

    def _candidates(self) -> list[SafewayProduct]:
        """Create a list of test candidates.

        Returns:
            List of SafewayProduct instances.
        """
        return [
            _make_product(product_id="A", name="Product A"),
            _make_product(product_id="B", name="Product B"),
        ]

    def test_valid_selection(self) -> None:
        """Test parsing a valid selection response."""
        text = json.dumps({"selected_index": 0, "reasoning": "Best match"})
        candidates = self._candidates()
        result = _parse_selection_response(text, candidates)

        assert result is not None
        assert result[0] is not None
        assert result[0].product_id == "A"
        assert result[1] == "Best match"

    def test_no_match_returns_none_product(self) -> None:
        """Test selected_index -1 returns None product."""
        text = json.dumps({"selected_index": -1, "reasoning": "Nothing fits"})
        result = _parse_selection_response(text, self._candidates())

        assert result is not None
        assert result[0] is None
        assert result[1] == "Nothing fits"

    def test_invalid_json(self) -> None:
        """Test invalid JSON returns None."""
        assert _parse_selection_response("not json", self._candidates()) is None

    def test_invalid_index_type(self) -> None:
        """Test non-integer index returns None."""
        text = json.dumps({"selected_index": "zero", "reasoning": "bad"})
        assert _parse_selection_response(text, self._candidates()) is None

    def test_out_of_range_index(self) -> None:
        """Test out-of-range index returns None."""
        text = json.dumps({"selected_index": 99, "reasoning": "bad"})
        assert _parse_selection_response(text, self._candidates()) is None

    def test_strips_markdown_fences(self) -> None:
        """Test markdown fences are stripped."""
        inner = json.dumps({"selected_index": 1, "reasoning": "Good"})
        text = f"```json\n{inner}\n```"
        result = _parse_selection_response(text, self._candidates())

        assert result is not None
        assert result[0] is not None
        assert result[0].product_id == "B"


# ------------------------------------------------------------------
# Tests: _extract_json_text
# ------------------------------------------------------------------


class TestExtractJsonText:
    """Tests for _extract_json_text."""

    def test_plain_json(self) -> None:
        """Test plain JSON passes through."""
        assert _extract_json_text('{"a": 1}') == '{"a": 1}'

    def test_strips_fences(self) -> None:
        """Test markdown fences are removed."""
        result = _extract_json_text('```json\n{"a": 1}\n```')
        assert result == '{"a": 1}'

    def test_strips_whitespace(self) -> None:
        """Test leading/trailing whitespace is stripped."""
        assert _extract_json_text("  {}\n  ") == "{}"


# ------------------------------------------------------------------
# Tests: ProductSelector.select_product
# ------------------------------------------------------------------


class TestSelectProduct:
    """Tests for ProductSelector.select_product."""

    def _make_selector(
        self,
        brand_prefs: list[BrandPreference] | None = None,
        price_sensitivity: str | None = None,
    ) -> ProductSelector:
        """Create a ProductSelector with mocked dependencies.

        Args:
            brand_prefs: Brand preferences to return.
            price_sensitivity: Price sensitivity preference value.

        Returns:
            ProductSelector with mock dependencies.
        """
        mock_client = MagicMock()
        mock_store = MagicMock()
        mock_store.get_brands_for_ingredient.return_value = brand_prefs or []
        mock_store.get_preference.return_value = price_sensitivity
        return ProductSelector(mock_client, mock_store)

    def test_no_candidates_returns_none(self) -> None:
        """Test empty candidates returns no product."""
        selector = self._make_selector()
        result = selector.select_product(_make_item(), [])

        assert result.product is None
        assert "No products available" in result.reasoning

    def test_all_avoided_returns_none(self) -> None:
        """Test all-avoided brands returns no product."""
        selector = self._make_selector(
            brand_prefs=[_make_pref("Organic", BrandPreferenceType.AVOID)]
        )
        candidates = [_make_product(name="Organic Milk")]
        result = selector.select_product(_make_item(), candidates)

        assert result.product is None
        assert "avoided brands" in result.reasoning

    @patch.object(ProductSelector, "_call_claude")
    def test_claude_selection(self, mock_claude: MagicMock) -> None:
        """Test Claude selects a product successfully."""
        mock_claude.return_value = json.dumps(
            {"selected_index": 0, "reasoning": "Best match for whole milk"}
        )
        selector = self._make_selector()
        candidates = [_make_product(product_id="A")]

        result = selector.select_product(_make_item(), candidates)

        assert result.product is not None
        assert result.product.product_id == "A"
        assert "Best match" in result.reasoning

    @patch.object(ProductSelector, "_call_claude")
    def test_claude_returns_no_match(self, mock_claude: MagicMock) -> None:
        """Test Claude returns no match (index -1)."""
        mock_claude.return_value = json.dumps(
            {"selected_index": -1, "reasoning": "Nothing suitable"}
        )
        selector = self._make_selector()
        candidates = [_make_product()]

        result = selector.select_product(_make_item(), candidates)

        assert result.product is None
        assert "Nothing suitable" in result.reasoning

    @patch.object(ProductSelector, "_call_claude")
    def test_fallback_on_claude_failure(self, mock_claude: MagicMock) -> None:
        """Test fallback heuristic when Claude fails."""
        mock_claude.return_value = None
        selector = self._make_selector()
        candidates = [
            _make_product(product_id="1", price=5.99),
            _make_product(product_id="2", price=3.49),
        ]

        result = selector.select_product(_make_item(), candidates)

        assert result.product is not None
        assert result.product.product_id == "2"
        assert "fallback" in result.reasoning

    @patch.object(ProductSelector, "_call_claude")
    def test_fallback_on_bad_json(self, mock_claude: MagicMock) -> None:
        """Test fallback when Claude returns unparseable response."""
        mock_claude.return_value = "I think product A is best"
        selector = self._make_selector()
        candidates = [_make_product()]

        result = selector.select_product(_make_item(), candidates)

        assert result.product is not None
        assert "fallback" in result.reasoning


# ------------------------------------------------------------------
# Tests: ProductSelector.select_products
# ------------------------------------------------------------------


class TestSelectProducts:
    """Tests for ProductSelector.select_products."""

    @patch.object(ProductSelector, "_call_claude")
    def test_selects_multiple(self, mock_claude: MagicMock) -> None:
        """Test batch selection for multiple items."""
        mock_claude.return_value = json.dumps(
            {"selected_index": 0, "reasoning": "Top choice"}
        )
        mock_client = MagicMock()
        mock_store = MagicMock()
        mock_store.get_brands_for_ingredient.return_value = []
        mock_store.get_preference.return_value = None
        selector = ProductSelector(mock_client, mock_store)

        items_and_candidates = [
            (_make_item(ingredient="milk"), [_make_product(product_id="M1")]),
            (_make_item(ingredient="eggs"), [_make_product(product_id="E1")]),
        ]
        results = selector.select_products(items_and_candidates)

        assert len(results) == 2
        assert results[0].product is not None
        assert results[1].product is not None


# ------------------------------------------------------------------
# Tests: ProductSelector._get_price_sensitivity
# ------------------------------------------------------------------


class TestGetPriceSensitivity:
    """Tests for price sensitivity retrieval."""

    def test_default_moderate(self) -> None:
        """Test default price sensitivity is moderate."""
        mock_client = MagicMock()
        mock_store = MagicMock()
        mock_store.get_preference.return_value = None
        selector = ProductSelector(mock_client, mock_store)

        assert selector._get_price_sensitivity() == "moderate"

    def test_returns_stored_value(self) -> None:
        """Test stored price sensitivity is returned."""
        mock_client = MagicMock()
        mock_store = MagicMock()
        mock_store.get_preference.return_value = "budget"
        selector = ProductSelector(mock_client, mock_store)

        assert selector._get_price_sensitivity() == "budget"

    def test_invalid_value_defaults_moderate(self) -> None:
        """Test invalid stored value defaults to moderate."""
        mock_client = MagicMock()
        mock_store = MagicMock()
        mock_store.get_preference.return_value = "invalid"
        selector = ProductSelector(mock_client, mock_store)

        assert selector._get_price_sensitivity() == "moderate"
