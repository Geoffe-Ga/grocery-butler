"""Tests for grocery_butler.substitution_service module."""

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
    SubstitutionSuitability,
)
from grocery_butler.substitution_service import (
    SubstitutionService,
    _extract_json_text,
    _fallback_ranking,
    _filter_avoided,
    _format_brand_prefs,
    _parse_ranking_response,
    _parse_single_ranking,
)

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


def _make_item(
    ingredient: str = "chicken thighs",
    search_term: str = "boneless chicken thighs",
) -> ShoppingListItem:
    """Create a test ShoppingListItem.

    Args:
        ingredient: Ingredient name.
        search_term: Search term.

    Returns:
        ShoppingListItem for testing.
    """
    return ShoppingListItem(
        ingredient=ingredient,
        quantity=2.0,
        unit="lb",
        category=IngredientCategory.MEAT,
        search_term=search_term,
        from_meals=["Test Meal"],
    )


def _make_product(
    product_id: str = "P001",
    name: str = "Boneless Chicken Thighs",
    price: float = 8.99,
    in_stock: bool = True,
) -> SafewayProduct:
    """Create a test SafewayProduct.

    Args:
        product_id: Product ID.
        name: Product name.
        price: Product price.
        in_stock: Whether in stock.

    Returns:
        SafewayProduct for testing.
    """
    return SafewayProduct(
        product_id=product_id,
        name=name,
        price=price,
        size="2 lb",
        in_stock=in_stock,
    )


def _make_pref(
    brand: str = "Tyson",
    pref_type: BrandPreferenceType = BrandPreferenceType.PREFERRED,
) -> BrandPreference:
    """Create a test BrandPreference.

    Args:
        brand: Brand name.
        pref_type: Preference type.

    Returns:
        BrandPreference for testing.
    """
    return BrandPreference(
        match_target="chicken",
        match_type=BrandMatchType.INGREDIENT,
        brand=brand,
        preference_type=pref_type,
    )


# ------------------------------------------------------------------
# Tests: _filter_avoided
# ------------------------------------------------------------------


class TestFilterAvoided:
    """Tests for _filter_avoided."""

    def test_no_prefs(self) -> None:
        """Test all products returned with no preferences."""
        products = [_make_product(name="A"), _make_product(name="B")]
        assert len(_filter_avoided(products, [])) == 2

    def test_removes_avoided(self) -> None:
        """Test avoided brand products are filtered out."""
        products = [
            _make_product(product_id="1", name="Tyson Chicken"),
            _make_product(product_id="2", name="Perdue Chicken"),
        ]
        prefs = [_make_pref("Tyson", BrandPreferenceType.AVOID)]
        result = _filter_avoided(products, prefs)

        assert len(result) == 1
        assert result[0].product_id == "2"


# ------------------------------------------------------------------
# Tests: _format_brand_prefs
# ------------------------------------------------------------------


class TestFormatBrandPrefs:
    """Tests for _format_brand_prefs."""

    def test_empty_prefs(self) -> None:
        """Test empty preferences returns 'None'."""
        assert _format_brand_prefs([]) == "None"

    def test_formats_prefs(self) -> None:
        """Test preferences are formatted correctly."""
        prefs = [
            _make_pref("Tyson", BrandPreferenceType.PREFERRED),
            _make_pref("Generic", BrandPreferenceType.AVOID),
        ]
        result = _format_brand_prefs(prefs)
        assert "prefer Tyson" in result
        assert "avoid Generic" in result


# ------------------------------------------------------------------
# Tests: _parse_ranking_response
# ------------------------------------------------------------------


class TestParseRankingResponse:
    """Tests for _parse_ranking_response."""

    def _alts(self) -> list[SafewayProduct]:
        """Create a list of alternatives.

        Returns:
            Two SafewayProduct instances.
        """
        return [
            _make_product(product_id="A", name="Alt A"),
            _make_product(product_id="B", name="Alt B"),
        ]

    def test_valid_response(self) -> None:
        """Test parsing a valid ranking response."""
        text = json.dumps(
            [
                {"index": 0, "suitability": "excellent", "reasoning": "Same cut"},
                {"index": 1, "suitability": "good", "reasoning": "Similar"},
            ]
        )
        result = _parse_ranking_response(text, self._alts())

        assert result is not None
        assert len(result) == 2
        assert result[0].product.product_id == "A"
        assert result[0].suitability == SubstitutionSuitability.EXCELLENT
        assert result[1].suitability == SubstitutionSuitability.GOOD

    def test_with_form_warning(self) -> None:
        """Test form warning is captured."""
        text = json.dumps(
            [
                {
                    "index": 0,
                    "suitability": "acceptable",
                    "form_warning": "This is breast, not thighs",
                    "reasoning": "Different cut",
                },
            ]
        )
        result = _parse_ranking_response(text, self._alts())

        assert result is not None
        assert result[0].form_warning == "This is breast, not thighs"

    def test_invalid_json(self) -> None:
        """Test invalid JSON returns None."""
        assert _parse_ranking_response("not json", self._alts()) is None

    def test_non_array_returns_none(self) -> None:
        """Test non-array JSON returns None."""
        assert _parse_ranking_response('{"a": 1}', self._alts()) is None

    def test_empty_array_returns_none(self) -> None:
        """Test empty array returns None."""
        assert _parse_ranking_response("[]", self._alts()) is None

    def test_strips_markdown_fences(self) -> None:
        """Test markdown fences are stripped."""
        inner = json.dumps(
            [
                {"index": 0, "suitability": "good", "reasoning": "OK"},
            ]
        )
        text = f"```json\n{inner}\n```"
        result = _parse_ranking_response(text, self._alts())

        assert result is not None
        assert len(result) == 1


# ------------------------------------------------------------------
# Tests: _parse_single_ranking
# ------------------------------------------------------------------


class TestParseSingleRanking:
    """Tests for _parse_single_ranking."""

    def _alts(self) -> list[SafewayProduct]:
        """Create alternatives for testing.

        Returns:
            List of SafewayProduct.
        """
        return [_make_product(product_id="A"), _make_product(product_id="B")]

    def test_valid_entry(self) -> None:
        """Test parsing a valid entry."""
        entry = {"index": 0, "suitability": "excellent", "reasoning": "Good"}
        result = _parse_single_ranking(entry, self._alts())

        assert result is not None
        assert result.product.product_id == "A"
        assert result.suitability == SubstitutionSuitability.EXCELLENT

    def test_invalid_index(self) -> None:
        """Test out-of-range index returns None."""
        entry = {"index": 99, "suitability": "good", "reasoning": "Bad"}
        assert _parse_single_ranking(entry, self._alts()) is None

    def test_non_dict_returns_none(self) -> None:
        """Test non-dict entry returns None."""
        assert _parse_single_ranking("not a dict", self._alts()) is None  # type: ignore[arg-type]  # Issue #13: intentionally passing wrong type to test runtime guard

    def test_unknown_suitability_defaults(self) -> None:
        """Test unknown suitability defaults to acceptable."""
        entry = {"index": 0, "suitability": "unknown", "reasoning": "OK"}
        result = _parse_single_ranking(entry, self._alts())

        assert result is not None
        assert result.suitability == SubstitutionSuitability.ACCEPTABLE


# ------------------------------------------------------------------
# Tests: _fallback_ranking
# ------------------------------------------------------------------


class TestFallbackRanking:
    """Tests for _fallback_ranking."""

    def test_sorts_by_price(self) -> None:
        """Test fallback sorts alternatives by price."""
        alts = [
            _make_product(product_id="A", price=9.99),
            _make_product(product_id="B", price=5.99),
            _make_product(product_id="C", price=7.49),
        ]
        result = _fallback_ranking(alts)

        assert len(result) == 3
        assert result[0].product.product_id == "B"
        assert result[1].product.product_id == "C"
        assert result[2].product.product_id == "A"

    def test_all_marked_acceptable(self) -> None:
        """Test all fallback options are marked acceptable."""
        alts = [_make_product()]
        result = _fallback_ranking(alts)
        assert result[0].suitability == SubstitutionSuitability.ACCEPTABLE


# ------------------------------------------------------------------
# Tests: _extract_json_text
# ------------------------------------------------------------------


class TestExtractJsonText:
    """Tests for _extract_json_text."""

    def test_plain_json(self) -> None:
        """Test plain JSON passes through."""
        assert _extract_json_text("[1, 2]") == "[1, 2]"

    def test_strips_fences(self) -> None:
        """Test markdown fences removed."""
        assert _extract_json_text("```json\n[1]\n```") == "[1]"

    def test_fence_with_no_newline(self) -> None:
        """Test fence marker with no newline strips the fence prefix."""
        assert _extract_json_text("```") == ""


# ------------------------------------------------------------------
# Tests: SubstitutionService.find_substitutions
# ------------------------------------------------------------------


class TestFindSubstitutions:
    """Tests for SubstitutionService.find_substitutions."""

    def _make_service(
        self,
        search_results: list[SafewayProduct] | None = None,
        brand_prefs: list[BrandPreference] | None = None,
    ) -> SubstitutionService:
        """Create a SubstitutionService with mocks.

        Args:
            search_results: Products returned by search.
            brand_prefs: Brand preferences to return.

        Returns:
            SubstitutionService with mock dependencies.
        """
        mock_client = MagicMock()
        mock_search = MagicMock()
        mock_search.search_products.return_value = search_results or []
        mock_store = MagicMock()
        mock_store.get_brands_for_ingredient.return_value = brand_prefs or []
        return SubstitutionService(mock_client, mock_search, mock_store)

    def test_no_alternatives(self) -> None:
        """Test no alternatives returns no_alternatives status."""
        service = self._make_service(search_results=[])
        original = _make_product(in_stock=False)
        result = service.find_substitutions(_make_item(), original)

        assert result.status == "no_alternatives"
        assert result.alternatives == []

    def test_filters_original_product(self) -> None:
        """Test original product is excluded from alternatives."""
        original = _make_product(product_id="ORIG", in_stock=False)
        service = self._make_service(
            search_results=[
                _make_product(product_id="ORIG", in_stock=True),
                _make_product(product_id="ALT1", name="Alternative", in_stock=True),
            ]
        )

        with patch.object(SubstitutionService, "_rank_with_claude") as mock_rank:
            mock_rank.return_value = _fallback_ranking(
                [_make_product(product_id="ALT1")]
            )
            result = service.find_substitutions(_make_item(), original)

        assert result.status == "alternatives_found"

    def test_all_avoided(self) -> None:
        """Test all alternatives from avoided brands."""
        service = self._make_service(
            search_results=[
                _make_product(product_id="A", name="BadBrand Chicken"),
            ],
            brand_prefs=[_make_pref("BadBrand", BrandPreferenceType.AVOID)],
        )
        original = _make_product(product_id="ORIG", in_stock=False)
        result = service.find_substitutions(_make_item(), original)

        assert result.status == "all_avoided"

    @patch.object(SubstitutionService, "_call_claude")
    def test_claude_ranking(self, mock_claude: MagicMock) -> None:
        """Test Claude ranks alternatives successfully."""
        mock_claude.return_value = json.dumps(
            [
                {"index": 0, "suitability": "excellent", "reasoning": "Same cut"},
            ]
        )
        service = self._make_service(
            search_results=[
                _make_product(product_id="ALT1", name="Alt Chicken"),
            ]
        )
        original = _make_product(product_id="ORIG", in_stock=False)
        result = service.find_substitutions(_make_item(), original)

        assert result.status == "alternatives_found"
        assert len(result.alternatives) == 1
        assert result.alternatives[0].suitability == SubstitutionSuitability.EXCELLENT

    @patch.object(SubstitutionService, "_call_claude")
    def test_fallback_on_claude_failure(self, mock_claude: MagicMock) -> None:
        """Test fallback ranking when Claude fails."""
        mock_claude.return_value = None
        service = self._make_service(
            search_results=[
                _make_product(product_id="A", name="Alt A", price=9.99),
                _make_product(product_id="B", name="Alt B", price=5.99),
            ]
        )
        original = _make_product(product_id="ORIG", in_stock=False)
        result = service.find_substitutions(_make_item(), original)

        assert result.status == "alternatives_found"
        assert len(result.alternatives) == 2
        # Cheapest first
        assert result.alternatives[0].product.product_id == "B"

    def test_excludes_out_of_stock_alternatives(self) -> None:
        """Test out-of-stock alternatives are excluded."""
        service = self._make_service(
            search_results=[
                _make_product(product_id="A", in_stock=False),
                _make_product(product_id="B", in_stock=True, name="Good"),
            ]
        )
        original = _make_product(product_id="ORIG", in_stock=False)

        with patch.object(SubstitutionService, "_rank_with_claude") as mock_rank:
            mock_rank.return_value = _fallback_ranking([_make_product(product_id="B")])
            result = service.find_substitutions(_make_item(), original)

        assert result.status == "alternatives_found"
