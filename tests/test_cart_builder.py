"""Tests for grocery_butler.cart_builder module."""

from __future__ import annotations

from dataclasses import dataclass
from unittest.mock import MagicMock

from grocery_butler.cart_builder import (
    CartBuilder,
    _calculate_quantity,
    _calculate_subtotal,
    _default_fulfillment_options,
    _get_fulfillment_fee,
    _parse_fulfillment_response,
    _parse_product_size,
    _recommend_fulfillment,
)
from grocery_butler.models import (
    CartItem,
    FulfillmentOption,
    FulfillmentType,
    IngredientCategory,
    SafewayProduct,
    ShoppingListItem,
    SubstitutionResult,
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
# Tests: _parse_product_size
# ------------------------------------------------------------------


class TestParseProductSize:
    """Tests for _parse_product_size."""

    def test_simple_size(self) -> None:
        """Test parsing '2 lb'."""
        assert _parse_product_size("2 lb") == 2.0

    def test_decimal_size(self) -> None:
        """Test parsing '1.5 gal'."""
        assert _parse_product_size("1.5 gal") == 1.5

    def test_no_number(self) -> None:
        """Test unparseable size returns 0."""
        assert _parse_product_size("each") == 0.0

    def test_empty_string(self) -> None:
        """Test empty string returns 0."""
        assert _parse_product_size("") == 0.0

    def test_leading_whitespace(self) -> None:
        """Test size with leading whitespace."""
        assert _parse_product_size("  16 oz") == 16.0


# ------------------------------------------------------------------
# Tests: _calculate_quantity
# ------------------------------------------------------------------


class TestCalculateQuantity:
    """Tests for _calculate_quantity."""

    def test_exact_match(self) -> None:
        """Test quantity matches product size exactly."""
        item = _make_item(quantity=2.0)
        product = _make_product(size="2 lb")
        assert _calculate_quantity(item, product) == 1

    def test_needs_two(self) -> None:
        """Test needing two products."""
        item = _make_item(quantity=3.0)
        product = _make_product(size="2 lb")
        assert _calculate_quantity(item, product) == 2

    def test_unparseable_size(self) -> None:
        """Test unparseable product size returns 1."""
        item = _make_item(quantity=2.0)
        product = _make_product(size="each")
        assert _calculate_quantity(item, product) == 1

    def test_fractional_rounds_up(self) -> None:
        """Test fractional quantity rounds up."""
        item = _make_item(quantity=2.5)
        product = _make_product(size="2 lb")
        assert _calculate_quantity(item, product) == 2

    def test_minimum_one(self) -> None:
        """Test minimum quantity is 1."""
        item = _make_item(quantity=0.5)
        product = _make_product(size="2 lb")
        assert _calculate_quantity(item, product) == 1


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
# Tests: _default_fulfillment_options
# ------------------------------------------------------------------


class TestDefaultFulfillmentOptions:
    """Tests for _default_fulfillment_options."""

    def test_returns_two_options(self) -> None:
        """Test defaults include pickup and delivery."""
        result = _default_fulfillment_options()
        assert len(result) == 2
        types = {o.type for o in result}
        assert FulfillmentType.PICKUP in types
        assert FulfillmentType.DELIVERY in types

    def test_pickup_is_free(self) -> None:
        """Test default pickup fee is 0."""
        result = _default_fulfillment_options()
        pickup = next(o for o in result if o.type == FulfillmentType.PICKUP)
        assert pickup.fee == 0.0


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

    def test_fulfillment_api_failure_uses_defaults(self) -> None:
        """Test default fulfillment options on API failure."""
        product = _make_product(price=5.0, size="1 lb")
        builder = self._make_builder(
            search_results=[product],
            selection_product=product,
        )
        builder._client.get.side_effect = Exception("API down")
        result = builder.build_cart([_make_item(quantity=1.0)])

        assert len(result.fulfillment_options) == 2
        assert result.recommended_fulfillment == FulfillmentType.PICKUP

    def test_empty_cart(self) -> None:
        """Test building cart with no items."""
        builder = self._make_builder()
        result = builder.build_cart([])

        assert result.items == []
        assert result.failed_items == []
        assert result.subtotal == 0.0
