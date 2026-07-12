"""Tests for grocery_butler.cart_builder module."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

from grocery_butler.cart_builder import (
    MAX_QUANTITY_PER_ITEM,
    CartBuilder,
    QuantityDecision,
    _calculate_quantity,
    _calculate_subtotal,
    _get_fulfillment_fee,
    _parse_fulfillment_response,
    _recommend_fulfillment,
)
from grocery_butler.models import (
    CartItem,
    FulfillmentOption,
    FulfillmentType,
    IngredientCategory,
    SafewayProduct,
    ShoppingListItem,
    SubstitutionOption,
    SubstitutionResult,
    SubstitutionSuitability,
)

# ------------------------------------------------------------------
# Fixtures
# ------------------------------------------------------------------


def _make_item(
    ingredient: str = "chicken thighs",
    quantity: float = 2.0,
    unit: str = "lb",
    search_term: str = "boneless chicken thighs",
) -> ShoppingListItem:
    """Create a test ShoppingListItem.

    Args:
        ingredient: Ingredient name.
        quantity: Desired quantity.
        unit: Unit of measurement.
        search_term: Search term.

    Returns:
        ShoppingListItem for testing.
    """
    return ShoppingListItem(
        ingredient=ingredient,
        quantity=quantity,
        unit=unit,
        category=IngredientCategory.MEAT,
        search_term=search_term,
        from_meals=["Test Meal"],
    )


def _make_product(
    product_id: str = "P001",
    name: str = "Boneless Chicken Thighs",
    price: float = 8.99,
    size: str = "2 lb",
    in_stock: bool = True,
) -> SafewayProduct:
    """Create a test SafewayProduct.

    Args:
        product_id: Product ID.
        name: Product name.
        price: Product price.
        size: Product size.
        in_stock: Whether in stock.

    Returns:
        SafewayProduct for testing.
    """
    return SafewayProduct(
        product_id=product_id,
        name=name,
        price=price,
        size=size,
        in_stock=in_stock,
    )


def _make_cart_item(
    price: float = 8.99,
    quantity: int = 1,
) -> CartItem:
    """Create a test CartItem.

    Args:
        price: Product price.
        quantity: Order quantity.

    Returns:
        CartItem for testing.
    """
    return CartItem(
        shopping_list_item=_make_item(),
        safeway_product=_make_product(price=price),
        quantity_to_order=quantity,
        estimated_cost=round(price * quantity, 2),
    )


@dataclass
class _MockSelectionResult:
    """Mock selection result matching ProductSelector output."""

    item: ShoppingListItem
    product: SafewayProduct | None
    reasoning: str


# ------------------------------------------------------------------
# Tests: _calculate_quantity
# ------------------------------------------------------------------
#
# NOTE: The old float-returning ``_parse_product_size`` helper (and its
# ``TestParseProductSize`` test class) was removed as part of issue #59.
# Its numeric-parsing behavior now lives in ``grocery_butler.units.
# parse_size`` and is covered by ``tests/test_units.py``.


class TestCalculateQuantity:
    """Tests for _calculate_quantity returning a QuantityDecision."""

    def test_exact_match_returns_quantity_one_no_review(self) -> None:
        """Test quantity matches product size exactly."""
        item = _make_item(quantity=2.0)
        product = _make_product(size="2 lb")
        decision = _calculate_quantity(item, product)
        assert decision == QuantityDecision(
            quantity=1, needs_review=False, review_reason=""
        )

    def test_needs_two_products_no_review(self) -> None:
        """Test needing two products."""
        item = _make_item(quantity=3.0)
        product = _make_product(size="2 lb")
        decision = _calculate_quantity(item, product)
        assert decision.quantity == 2
        assert decision.needs_review is False
        assert decision.review_reason == ""

    def test_fractional_rounds_up(self) -> None:
        """Test fractional quantity rounds up."""
        item = _make_item(quantity=2.5)
        product = _make_product(size="2 lb")
        decision = _calculate_quantity(item, product)
        assert decision.quantity == 2

    def test_minimum_one(self) -> None:
        """Test minimum quantity is 1."""
        item = _make_item(quantity=0.5)
        product = _make_product(size="2 lb")
        decision = _calculate_quantity(item, product)
        assert decision.quantity == 1


class TestCalculateQuantityUnparseableSize:
    """Tests for _calculate_quantity when the product size can't be parsed."""

    def test_size_each_flags_unparseable(self) -> None:
        """Test a non-numeric size like 'each' flags unparseable_size."""
        item = _make_item(quantity=2.0)
        product = _make_product(size="each")
        decision = _calculate_quantity(item, product)
        assert decision == QuantityDecision(
            quantity=1, needs_review=True, review_reason="unparseable_size"
        )

    def test_empty_size_flags_unparseable(self) -> None:
        """Test an empty size string flags unparseable_size.

        Regression guard for issue #59: previously an empty ``size``
        silently produced a quantity of 1 with no indication that the
        product's size was unknown.
        """
        item = _make_item(quantity=2.0)
        product = _make_product(size="")
        decision = _calculate_quantity(item, product)
        assert decision.quantity == 1
        assert decision.needs_review is True
        assert decision.review_reason == "unparseable_size"


class TestCalculateQuantityUnitConversion:
    """Tests for _calculate_quantity unit-aware conversion (issue #59).

    Reproduces the reported bugs: dividing raw numeric quantities without
    regard to units caused massive over-orders (500 g item vs. a "5 lb"
    product ordering 100 units) and under-orders (2 lb item vs. a "16 oz"
    product ordering only 1 unit instead of 2).
    """

    def test_500g_item_vs_5lb_product_no_overorder(self) -> None:
        """Test 500 g vs '5 lb' orders 1, not 100 (the reported bug)."""
        item = _make_item(quantity=500.0, unit="g")
        product = _make_product(size="5 lb")
        decision = _calculate_quantity(item, product)
        assert decision.quantity == 1
        assert decision.needs_review is False

    def test_2lb_item_vs_16oz_product_orders_two_exactly(self) -> None:
        """Test 2 lb vs '16 oz' orders exactly 2, not 1 (the reported bug).

        16 oz == 1 lb, so the exact ratio is 2.0. The epsilon applied
        before ``ceil`` must prevent this from rounding up to 3.
        """
        item = _make_item(quantity=2.0, unit="lb")
        product = _make_product(size="16 oz")
        decision = _calculate_quantity(item, product)
        assert decision.quantity == 2
        assert decision.needs_review is False

    def test_500g_item_vs_1lb_product_orders_two(self) -> None:
        """Test 500 g vs '1 lb' (453.59237 g) needs 2 units."""
        item = _make_item(quantity=500.0, unit="g")
        product = _make_product(size="1 lb")
        decision = _calculate_quantity(item, product)
        assert decision.quantity == 2

    def test_8oz_item_vs_1lb_product_orders_one(self) -> None:
        """Test 8 oz vs '1 lb' is exactly half, so 1 unit suffices."""
        item = _make_item(quantity=8.0, unit="oz")
        product = _make_product(size="1 lb")
        decision = _calculate_quantity(item, product)
        assert decision.quantity == 1

    def test_4cup_item_vs_1gal_product_orders_one(self) -> None:
        """Test 4 cups vs '1 gal' (16 cups) needs only 1 unit."""
        item = _make_item(quantity=4.0, unit="cup")
        product = _make_product(size="1 gal")
        decision = _calculate_quantity(item, product)
        assert decision.quantity == 1

    def test_20cup_item_vs_1gal_product_orders_two(self) -> None:
        """Test 20 cups vs '1 gal' (16 cups) needs 2 units."""
        item = _make_item(quantity=20.0, unit="cup")
        product = _make_product(size="1 gal")
        decision = _calculate_quantity(item, product)
        assert decision.quantity == 2

    def test_fractional_excess_still_rounds_up_past_epsilon(self) -> None:
        """Test a genuine fractional excess still rounds up (regression).

        Regression guard for the epsilon subtracted before ``math.ceil``
        in ``_calculate_quantity`` (``needed - _QUANTITY_EPSILON``). 2.05
        lb vs. a "1 lb" product needs 2.05 units, which must still ceil
        to 3 — the tiny epsilon exists only to absorb floating-point
        error on exact ratios, and must never mask a real fractional
        need.
        """
        item = _make_item(quantity=2.05, unit="lb")
        product = _make_product(size="1 lb")
        decision = _calculate_quantity(item, product)
        assert decision.quantity == 3
        assert decision.needs_review is False


class TestCalculateQuantityIncomparableUnits:
    """Tests for _calculate_quantity when units differ in dimension."""

    def test_each_item_vs_lb_product_flags_incomparable(self) -> None:
        """Test 2 each vs '5 lb' can't be compared and flags for review."""
        item = _make_item(quantity=2.0, unit="each")
        product = _make_product(size="5 lb")
        decision = _calculate_quantity(item, product)
        assert decision == QuantityDecision(
            quantity=1, needs_review=True, review_reason="incomparable_units"
        )


class TestCalculateQuantityCap:
    """Tests for the per-item quantity cap (issue #59)."""

    def test_large_quantity_capped_at_default_max(self) -> None:
        """Test 100 lb vs '1 lb' is capped at MAX_QUANTITY_PER_ITEM."""
        item = _make_item(quantity=100.0, unit="lb")
        product = _make_product(size="1 lb")
        decision = _calculate_quantity(item, product)
        assert decision == QuantityDecision(
            quantity=MAX_QUANTITY_PER_ITEM,
            needs_review=True,
            review_reason="quantity_capped",
        )
        assert decision.quantity == 10

    def test_large_quantity_capped_at_injected_cap(self) -> None:
        """Test the cap is injectable via the ``cap`` keyword argument."""
        item = _make_item(quantity=100.0, unit="lb")
        product = _make_product(size="1 lb")
        decision = _calculate_quantity(item, product, cap=3)
        assert decision == QuantityDecision(
            quantity=3, needs_review=True, review_reason="quantity_capped"
        )


# ------------------------------------------------------------------
# Tests: _calculate_subtotal
# ------------------------------------------------------------------


class TestCalculateSubtotal:
    """Tests for _calculate_subtotal."""

    def test_items_only(self) -> None:
        """Test subtotal with regular items only."""
        items = [_make_cart_item(price=5.0), _make_cart_item(price=3.0)]
        assert _calculate_subtotal(items, []) == 8.0

    def test_with_restock(self) -> None:
        """Test subtotal includes restock items."""
        items = [_make_cart_item(price=5.0)]
        restock = [_make_cart_item(price=2.0)]
        assert _calculate_subtotal(items, restock) == 7.0

    def test_empty(self) -> None:
        """Test empty lists returns 0."""
        assert _calculate_subtotal([], []) == 0.0


# ------------------------------------------------------------------
# Tests: _recommend_fulfillment
# ------------------------------------------------------------------


class TestRecommendFulfillment:
    """Tests for _recommend_fulfillment."""

    def test_prefers_pickup(self) -> None:
        """Test pickup is preferred when available."""
        options = [
            FulfillmentOption(
                type=FulfillmentType.DELIVERY,
                available=True,
                fee=9.95,
                windows=[],
            ),
            FulfillmentOption(
                type=FulfillmentType.PICKUP,
                available=True,
                fee=0.0,
                windows=[],
            ),
        ]
        assert _recommend_fulfillment(options) == FulfillmentType.PICKUP

    def test_delivery_when_no_pickup(self) -> None:
        """Test delivery when pickup unavailable."""
        options = [
            FulfillmentOption(
                type=FulfillmentType.PICKUP,
                available=False,
                fee=0.0,
                windows=[],
            ),
            FulfillmentOption(
                type=FulfillmentType.DELIVERY,
                available=True,
                fee=9.95,
                windows=[],
            ),
        ]
        assert _recommend_fulfillment(options) == FulfillmentType.DELIVERY

    def test_empty_options(self) -> None:
        """Test defaults to pickup with no options."""
        assert _recommend_fulfillment([]) == FulfillmentType.PICKUP

    def test_none_available(self) -> None:
        """Test defaults to pickup when none available."""
        options = [
            FulfillmentOption(
                type=FulfillmentType.DELIVERY,
                available=False,
                fee=9.95,
                windows=[],
            ),
        ]
        assert _recommend_fulfillment(options) == FulfillmentType.PICKUP


# ------------------------------------------------------------------
# Tests: _get_fulfillment_fee
# ------------------------------------------------------------------


class TestGetFulfillmentFee:
    """Tests for _get_fulfillment_fee."""

    def test_finds_fee(self) -> None:
        """Test finding fee for recommended type."""
        options = [
            FulfillmentOption(
                type=FulfillmentType.PICKUP,
                available=True,
                fee=0.0,
                windows=[],
            ),
            FulfillmentOption(
                type=FulfillmentType.DELIVERY,
                available=True,
                fee=9.95,
                windows=[],
            ),
        ]
        assert _get_fulfillment_fee(options, FulfillmentType.DELIVERY) == 9.95

    def test_missing_type(self) -> None:
        """Test returns 0 when type not found."""
        assert _get_fulfillment_fee([], FulfillmentType.PICKUP) == 0.0


# ------------------------------------------------------------------
# Tests: _parse_fulfillment_response
# ------------------------------------------------------------------


class TestParseFulfillmentResponse:
    """Tests for _parse_fulfillment_response."""

    def test_valid_response(self) -> None:
        """Test parsing a valid fulfillment response."""
        response = {
            "fulfillmentOptions": [
                {
                    "type": "pickup",
                    "available": True,
                    "fee": 0.0,
                    "windows": [{"display": "Today 4-6pm"}],
                },
                {
                    "type": "delivery",
                    "available": True,
                    "fee": 9.95,
                    "windows": [],
                },
            ]
        }
        result = _parse_fulfillment_response(response)
        assert len(result) == 2
        assert result[0].type == FulfillmentType.PICKUP
        assert result[0].next_window == "Today 4-6pm"
        assert result[1].fee == 9.95
        assert result[1].next_window is None

    def test_empty_response(self) -> None:
        """Test empty response returns empty list."""
        assert _parse_fulfillment_response({}) == []

    def test_invalid_type_skipped(self) -> None:
        """Test entries with invalid type are skipped."""
        response = {
            "fulfillmentOptions": [
                {"type": "teleport", "available": True, "fee": 0},
            ]
        }
        assert _parse_fulfillment_response(response) == []


# ------------------------------------------------------------------
# Tests: CartBuilder.build_cart
# ------------------------------------------------------------------


class TestBuildCart:
    """Tests for CartBuilder.build_cart."""

    def _make_builder(
        self,
        search_results: list[SafewayProduct] | None = None,
        selection_product: SafewayProduct | None = None,
        fulfillment_response: dict | None = None,
    ) -> CartBuilder:
        """Create a CartBuilder with mock dependencies.

        Args:
            search_results: Products returned by search.
            selection_product: Product returned by selector.
            fulfillment_response: Fulfillment API response.

        Returns:
            CartBuilder with mocked services.
        """
        mock_search = MagicMock()
        mock_search.search_or_cached.return_value = search_results or []

        mock_selector = MagicMock()
        item = _make_item()
        mock_selector.select_product.return_value = _MockSelectionResult(
            item=item,
            product=selection_product,
            reasoning="Test selection",
        )

        mock_substitution = MagicMock()
        mock_substitution.find_substitutions.return_value = SubstitutionResult(
            status="no_alternatives",
            original_item=item,
            message="No alternatives",
        )

        mock_client = MagicMock()
        mock_client.store_id = "1234"
        mock_client.get.return_value = fulfillment_response or {}

        return CartBuilder(
            search_service=mock_search,
            product_selector=mock_selector,
            substitution_service=mock_substitution,
            safeway_client=mock_client,
        )

    def test_successful_cart(self) -> None:
        """Test building cart with available products."""
        product = _make_product(price=8.99, size="2 lb")
        builder = self._make_builder(
            search_results=[product],
            selection_product=product,
        )
        result = builder.build_cart([_make_item()])

        assert len(result.items) == 1
        assert result.items[0].safeway_product.product_id == "P001"
        assert result.items[0].quantity_to_order == 1
        assert result.items[0].estimated_cost == 8.99
        assert result.failed_items == []

    def test_no_products_found(self) -> None:
        """Test item goes to failed when no products found."""
        builder = self._make_builder(search_results=[])
        result = builder.build_cart([_make_item()])

        assert len(result.failed_items) == 1
        assert result.items == []

    def test_selector_returns_none(self) -> None:
        """Test item goes to failed when selector returns None."""
        builder = self._make_builder(
            search_results=[_make_product()],
            selection_product=None,
        )
        result = builder.build_cart([_make_item()])

        assert len(result.failed_items) == 1

    def test_out_of_stock_triggers_substitution(self) -> None:
        """Test out-of-stock product triggers substitution flow."""
        product = _make_product(in_stock=False)
        builder = self._make_builder(
            search_results=[product],
            selection_product=product,
        )
        result = builder.build_cart([_make_item()])

        assert len(result.substituted_items) == 1
        assert result.substituted_items[0].status == "no_alternatives"

    def test_out_of_stock_selects_best_alternative(self) -> None:
        """Test that selected is set to first alternative when available."""
        oos_product = _make_product(in_stock=False)
        alt_product = _make_product(
            product_id="ALT1", name="Organic Chicken Thighs", price=10.99
        )
        alt_option = SubstitutionOption(
            product=alt_product,
            suitability=SubstitutionSuitability.GOOD,
            reasoning="Similar cut",
        )
        sub_result = SubstitutionResult(
            status="alternatives_found",
            original_item=_make_item(),
            alternatives=[alt_option],
            message="Found 1 alternative(s)",
        )

        mock_search = MagicMock()
        mock_search.search_or_cached.return_value = [oos_product]

        mock_selector = MagicMock()
        mock_selector.select_product.return_value = _MockSelectionResult(
            item=_make_item(),
            product=oos_product,
            reasoning="Test",
        )

        mock_substitution = MagicMock()
        mock_substitution.find_substitutions.return_value = sub_result

        mock_client = MagicMock()
        mock_client.store_id = "1234"
        mock_client.get.return_value = {}

        builder = CartBuilder(
            search_service=mock_search,
            product_selector=mock_selector,
            substitution_service=mock_substitution,
            safeway_client=mock_client,
        )
        cart = builder.build_cart([_make_item()])

        assert len(cart.substituted_items) == 1
        sub = cart.substituted_items[0]
        assert sub.selected is not None
        assert sub.selected.product.name == "Organic Chicken Thighs"

    def test_restock_items_separated(self) -> None:
        """Test restock items go to restock_items list."""
        product = _make_product(price=3.99, size="1 gal")
        builder = self._make_builder(
            search_results=[product],
            selection_product=product,
        )
        restock = _make_item(
            ingredient="milk",
            search_term="whole milk",
            quantity=1.0,
        )
        result = builder.build_cart([], restock_items=[restock])

        assert len(result.restock_items) == 1
        assert result.items == []

    def test_subtotal_calculation(self) -> None:
        """Test subtotal sums item costs."""
        product = _make_product(price=5.0, size="1 lb")
        builder = self._make_builder(
            search_results=[product],
            selection_product=product,
        )
        items = [_make_item(quantity=1.0), _make_item(quantity=1.0)]
        result = builder.build_cart(items)

        assert result.subtotal == 10.0

    def test_estimated_total_includes_fee(self) -> None:
        """Test estimated total includes fulfillment fee."""
        product = _make_product(price=10.0, size="1 lb")
        fulfillment = {
            "fulfillmentOptions": [
                {
                    "type": "pickup",
                    "available": True,
                    "fee": 0.0,
                    "windows": [],
                },
            ]
        }
        builder = self._make_builder(
            search_results=[product],
            selection_product=product,
            fulfillment_response=fulfillment,
        )
        result = builder.build_cart([_make_item(quantity=1.0)])

        assert result.estimated_total == 10.0

    def test_fulfillment_api_failure_marks_cart_unverified_no_fabricated_options(
        self,
    ) -> None:
        """Test a fulfillment-fetch failure marks the cart unverified (issue #72).

        Regression guard for issue #72 (HIGH): previously any exception
        raised while fetching fulfillment options was swallowed by
        ``_get_fulfillment_options`` and replaced with fabricated
        defaults (free pickup, $9.95 delivery, both marked
        ``available=True``) via the now-deleted
        ``_default_fulfillment_options`` helper -- presenting invented
        availability and fees as real. On failure, ``build_cart`` must
        instead return an empty ``fulfillment_options`` list and flag
        ``fulfillment_unverified=True`` so callers know fulfillment was
        never actually confirmed with Safeway.
        """
        product = _make_product(price=5.0, size="1 lb")
        builder = self._make_builder(
            search_results=[product],
            selection_product=product,
        )
        builder._client.get.side_effect = Exception("API down")
        result = builder.build_cart([_make_item(quantity=1.0)])

        assert result.fulfillment_options == []
        assert result.fulfillment_unverified is True

    def test_fulfillment_api_failure_estimated_total_has_no_fabricated_fee(
        self,
    ) -> None:
        """Test a fulfillment-fetch failure never adds the fabricated $9.95 fee.

        With no fulfillment options available, ``_recommend_fulfillment``
        falls back to ``PICKUP`` and ``_get_fulfillment_fee`` returns
        ``0.0``, so the estimated total on an unverified cart must equal
        the subtotal exactly -- never the subtotal plus an invented
        delivery fee (issue #72). ``fulfillment_options`` recommending
        free pickup alone can't distinguish this fix from the old
        fabricated-defaults behavior (which also recommended free
        pickup), so this also asserts the options list is empty --
        i.e. no fabricated delivery option carrying the $9.95 fee is
        present anywhere in it.
        """
        product = _make_product(price=5.0, size="1 lb")
        builder = self._make_builder(
            search_results=[product],
            selection_product=product,
        )
        builder._client.get.side_effect = Exception("API down")
        result = builder.build_cart([_make_item(quantity=1.0)])

        assert result.estimated_total == result.subtotal
        assert result.recommended_fulfillment == FulfillmentType.PICKUP
        assert result.fulfillment_options == []

    def test_fulfillment_api_success_marks_cart_verified(self) -> None:
        """Test a successful fulfillment fetch leaves the cart verified.

        On the happy path -- fulfillment options fetched without error --
        ``fulfillment_unverified`` must be ``False`` and the parsed
        options from the API response must be used as before (issue #72).
        """
        product = _make_product(price=5.0, size="1 lb")
        fulfillment = {
            "fulfillmentOptions": [
                {"type": "pickup", "available": True, "fee": 0.0, "windows": []},
            ]
        }
        builder = self._make_builder(
            search_results=[product],
            selection_product=product,
            fulfillment_response=fulfillment,
        )
        result = builder.build_cart([_make_item(quantity=1.0)])

        assert result.fulfillment_unverified is False
        assert len(result.fulfillment_options) == 1
        assert result.fulfillment_options[0].type == FulfillmentType.PICKUP

    def test_empty_cart(self) -> None:
        """Test building cart with no items."""
        builder = self._make_builder()
        result = builder.build_cart([])

        assert result.items == []
        assert result.failed_items == []
        assert result.subtotal == 0.0

    def test_unparseable_product_size_flags_cart_item_for_review(self) -> None:
        """Test build_cart propagates needs_review/review_reason (issue #59).

        A product with an unparseable size (e.g. "each") must produce a
        CartItem carrying ``needs_review=True`` and
        ``review_reason="unparseable_size"`` rather than silently
        defaulting to a quantity of 1 with no signal to the user.
        """
        product = _make_product(price=8.99, size="each")
        builder = self._make_builder(
            search_results=[product],
            selection_product=product,
        )
        result = builder.build_cart([_make_item()])

        assert len(result.items) == 1
        cart_item = result.items[0]
        assert cart_item.quantity_to_order == 1
        assert cart_item.needs_review is True
        assert cart_item.review_reason == "unparseable_size"

    def test_normal_product_size_leaves_cart_item_unflagged(self) -> None:
        """Test a normal, parseable product size leaves review fields unset."""
        product = _make_product(price=8.99, size="2 lb")
        builder = self._make_builder(
            search_results=[product],
            selection_product=product,
        )
        result = builder.build_cart([_make_item(quantity=2.0)])

        assert len(result.items) == 1
        cart_item = result.items[0]
        assert cart_item.needs_review is False
        assert cart_item.review_reason == ""

    def test_max_quantity_per_item_is_injectable_on_builder(self) -> None:
        """Test CartBuilder accepts a max_quantity_per_item override.

        With a low cap, a large order requirement must be capped and the
        resulting CartItem flagged with review_reason="quantity_capped".
        """
        product = _make_product(price=1.0, size="1 lb")
        mock_search = MagicMock()
        mock_search.search_or_cached.return_value = [product]

        item = _make_item(quantity=100.0, unit="lb")
        mock_selector = MagicMock()
        mock_selector.select_product.return_value = _MockSelectionResult(
            item=item,
            product=product,
            reasoning="Test selection",
        )

        mock_substitution = MagicMock()
        mock_client = MagicMock()
        mock_client.store_id = "1234"
        mock_client.get.return_value = {}

        builder = CartBuilder(
            search_service=mock_search,
            product_selector=mock_selector,
            substitution_service=mock_substitution,
            safeway_client=mock_client,
            max_quantity_per_item=3,
        )
        result = builder.build_cart([item])

        assert len(result.items) == 1
        cart_item = result.items[0]
        assert cart_item.quantity_to_order == 3
        assert cart_item.needs_review is True
        assert cart_item.review_reason == "quantity_capped"
