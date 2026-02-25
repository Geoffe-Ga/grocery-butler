"""Tests for grocery_butler.order_service module."""

from __future__ import annotations

from unittest.mock import MagicMock

from grocery_butler.models import (
    CartItem,
    CartSummary,
    FulfillmentOption,
    FulfillmentType,
    IngredientCategory,
    SafewayProduct,
    ShoppingListItem,
)
from grocery_butler.order_service import (
    OrderService,
    _build_order_payload,
    _collect_restock_ingredients,
    _parse_order_response,
    _safe_float,
    _serialize_cart_items,
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
) -> SafewayProduct:
    """Create a test SafewayProduct.

    Args:
        product_id: Product ID.
        name: Product name.
        price: Product price.

    Returns:
        SafewayProduct for testing.
    """
    return SafewayProduct(
        product_id=product_id,
        name=name,
        price=price,
        size="2 lb",
        in_stock=True,
    )


def _make_cart_item(
    ingredient: str = "chicken thighs",
    product_id: str = "P001",
    price: float = 8.99,
) -> CartItem:
    """Create a test CartItem.

    Args:
        ingredient: Ingredient name.
        product_id: Product ID.
        price: Product price.

    Returns:
        CartItem for testing.
    """
    return CartItem(
        shopping_list_item=_make_item(ingredient=ingredient),
        safeway_product=_make_product(product_id=product_id, price=price),
        quantity_to_order=1,
        estimated_cost=price,
    )


def _make_cart(
    items: list[CartItem] | None = None,
    restock_items: list[CartItem] | None = None,
) -> CartSummary:
    """Create a test CartSummary.

    Args:
        items: Regular cart items.
        restock_items: Restock queue items.

    Returns:
        CartSummary for testing.
    """
    cart_items = [_make_cart_item()] if items is None else items
    restock = [] if restock_items is None else restock_items
    subtotal = sum(i.estimated_cost for i in cart_items + restock)
    return CartSummary(
        items=cart_items,
        failed_items=[],
        substituted_items=[],
        skipped_items=[],
        restock_items=restock,
        subtotal=subtotal,
        fulfillment_options=[
            FulfillmentOption(
                type=FulfillmentType.PICKUP,
                available=True,
                fee=0.0,
                windows=[],
            ),
        ],
        recommended_fulfillment=FulfillmentType.PICKUP,
        estimated_total=subtotal,
    )


# ------------------------------------------------------------------
# Tests: _serialize_cart_items
# ------------------------------------------------------------------


class TestSerializeCartItems:
    """Tests for _serialize_cart_items."""

    def test_serializes_items(self) -> None:
        """Test items are serialized with productId and quantity."""
        items = [_make_cart_item(product_id="A")]
        result = _serialize_cart_items(items)
        assert len(result) == 1
        assert result[0]["productId"] == "A"
        assert result[0]["quantity"] == 1

    def test_empty_list(self) -> None:
        """Test empty list returns empty."""
        assert _serialize_cart_items([]) == []


# ------------------------------------------------------------------
# Tests: _build_order_payload
# ------------------------------------------------------------------


class TestBuildOrderPayload:
    """Tests for _build_order_payload."""

    def test_includes_items_and_fulfillment(self) -> None:
        """Test payload has items, fulfillment type, and total."""
        cart = _make_cart()
        result = _build_order_payload(cart)

        assert "items" in result
        assert result["fulfillmentType"] == "pickup"
        assert result["estimatedTotal"] == cart.estimated_total

    def test_includes_restock_items(self) -> None:
        """Test restock items are included in payload."""
        restock = _make_cart_item(ingredient="milk", product_id="R1")
        cart = _make_cart(restock_items=[restock])
        result = _build_order_payload(cart)

        product_ids = [i["productId"] for i in result["items"]]
        assert "P001" in product_ids
        assert "R1" in product_ids


# ------------------------------------------------------------------
# Tests: _parse_order_response
# ------------------------------------------------------------------


class TestParseOrderResponse:
    """Tests for _parse_order_response."""

    def test_successful_response(self) -> None:
        """Test parsing a successful order response."""
        response = {
            "orderId": "ORD-12345",
            "status": "confirmed",
            "estimatedTime": "Today 4-6pm",
            "total": 25.99,
        }
        cart = _make_cart()
        result = _parse_order_response(response, cart)

        assert result is not None
        assert result.order_id == "ORD-12345"
        assert result.status == "confirmed"
        assert result.estimated_time == "Today 4-6pm"
        assert result.total == 25.99

    def test_error_response(self) -> None:
        """Test error status returns None."""
        response = {"status": "error", "error": "Out of delivery slots"}
        assert _parse_order_response(response, _make_cart()) is None

    def test_missing_order_id(self) -> None:
        """Test missing orderId returns None."""
        response = {"status": "confirmed"}
        assert _parse_order_response(response, _make_cart()) is None

    def test_defaults_from_cart(self) -> None:
        """Test missing fields use cart defaults."""
        response = {"orderId": "ORD-1"}
        cart = _make_cart()
        result = _parse_order_response(response, cart)

        assert result is not None
        assert result.total == cart.estimated_total
        assert result.fulfillment_type == FulfillmentType.PICKUP

    def test_item_count_includes_restock(self) -> None:
        """Test item count includes restock items."""
        restock = _make_cart_item(ingredient="milk", product_id="R1")
        cart = _make_cart(restock_items=[restock])
        response = {"orderId": "ORD-1"}
        result = _parse_order_response(response, cart)

        assert result is not None
        assert result.item_count == 2

    def test_malformed_total_uses_cart_fallback(self) -> None:
        """Test non-numeric total falls back to cart estimated_total."""
        response = {"orderId": "ORD-1", "total": "N/A"}
        cart = _make_cart()
        result = _parse_order_response(response, cart)

        assert result is not None
        assert result.total == cart.estimated_total


# ------------------------------------------------------------------
# Tests: _collect_restock_ingredients
# ------------------------------------------------------------------


class TestCollectRestockIngredients:
    """Tests for _collect_restock_ingredients."""

    def test_collects_ingredients(self) -> None:
        """Test ingredient names are collected from restock items."""
        restock = [
            _make_cart_item(ingredient="milk"),
            _make_cart_item(ingredient="eggs"),
        ]
        cart = _make_cart(items=[], restock_items=restock)
        result = _collect_restock_ingredients(cart)

        assert result == ["milk", "eggs"]

    def test_empty_restock(self) -> None:
        """Test empty restock returns empty list."""
        cart = _make_cart(restock_items=[])
        assert _collect_restock_ingredients(cart) == []


# ------------------------------------------------------------------
# Tests: OrderService.submit_order
# ------------------------------------------------------------------


class TestSubmitOrder:
    """Tests for OrderService.submit_order."""

    def _make_service(
        self,
        api_response: dict | None = None,
        api_error: bool = False,
    ) -> OrderService:
        """Create an OrderService with mock dependencies.

        Args:
            api_response: Response from Safeway API.
            api_error: Whether API should raise an exception.

        Returns:
            OrderService with mocked client and pantry.
        """
        mock_client = MagicMock()
        if api_error:
            mock_client.post.side_effect = Exception("API error")
        else:
            mock_client.post.return_value = api_response or {}

        mock_pantry = MagicMock()
        mock_pantry.mark_restocked.return_value = 0

        return OrderService(mock_client, mock_pantry)

    def test_successful_order(self) -> None:
        """Test successful order submission."""
        service = self._make_service(
            api_response={
                "orderId": "ORD-123",
                "status": "confirmed",
                "estimatedTime": "Today 4-6pm",
                "total": 8.99,
            }
        )
        result = service.submit_order(_make_cart())

        assert result.success is True
        assert result.confirmation is not None
        assert result.confirmation.order_id == "ORD-123"

    def test_empty_cart_rejected(self) -> None:
        """Test empty cart is rejected without API call."""
        service = self._make_service()
        cart = _make_cart(items=[], restock_items=[])
        result = service.submit_order(cart)

        assert result.success is False
        assert "empty" in result.error_message.lower()

    def test_api_failure(self) -> None:
        """Test API exception returns failure."""
        service = self._make_service(api_error=True)
        result = service.submit_order(_make_cart())

        assert result.success is False
        assert "failed" in result.error_message.lower()

    def test_error_response(self) -> None:
        """Test error response from API."""
        service = self._make_service(
            api_response={"status": "error", "error": "No slots"}
        )
        result = service.submit_order(_make_cart())

        assert result.success is False
        assert result.error_message == "No slots"

    def test_restocks_inventory(self) -> None:
        """Test restock items update inventory on success."""
        service = self._make_service(
            api_response={
                "orderId": "ORD-123",
                "status": "confirmed",
                "total": 5.0,
            }
        )
        service._pantry.mark_restocked.return_value = 2
        restock = [
            _make_cart_item(ingredient="milk"),
            _make_cart_item(ingredient="eggs"),
        ]
        cart = _make_cart(restock_items=restock)
        result = service.submit_order(cart)

        assert result.success is True
        assert result.items_restocked == 2
        service._pantry.mark_restocked.assert_called_once_with(["milk", "eggs"])

    def test_restock_failure_doesnt_fail_order(self) -> None:
        """Test inventory update failure doesn't fail the order."""
        service = self._make_service(
            api_response={
                "orderId": "ORD-123",
                "status": "confirmed",
                "total": 5.0,
            }
        )
        service._pantry.mark_restocked.side_effect = Exception("DB error")
        restock = [_make_cart_item(ingredient="milk")]
        cart = _make_cart(restock_items=restock)
        result = service.submit_order(cart)

        assert result.success is True
        assert result.items_restocked == 0

    def test_no_restock_without_restock_items(self) -> None:
        """Test pantry not called when no restock items."""
        service = self._make_service(
            api_response={
                "orderId": "ORD-123",
                "status": "confirmed",
                "total": 8.99,
            }
        )
        cart = _make_cart(restock_items=[])
        result = service.submit_order(cart)

        assert result.success is True
        assert result.items_restocked == 0
        service._pantry.mark_restocked.assert_not_called()

    def test_unknown_error_response(self) -> None:
        """Test unknown error when no error field in response."""
        service = self._make_service(api_response={"status": "error"})
        result = service.submit_order(_make_cart())

        assert result.success is False
        assert result.error_message == "Unknown order error"

    def test_malformed_total_in_submit(self) -> None:
        """Test malformed total in API response doesn't crash submit."""
        service = self._make_service(
            api_response={
                "orderId": "ORD-123",
                "status": "confirmed",
                "total": "not-a-number",
            }
        )
        result = service.submit_order(_make_cart())

        assert result.success is True
        assert result.confirmation is not None


# ------------------------------------------------------------------
# Tests: _safe_float
# ------------------------------------------------------------------


class TestSafeFloat:
    """Tests for _safe_float."""

    def test_valid_float(self) -> None:
        """Test valid float value passes through."""
        assert _safe_float(25.99, 0.0) == 25.99

    def test_valid_int(self) -> None:
        """Test integer value converts to float."""
        assert _safe_float(10, 0.0) == 10.0

    def test_valid_string(self) -> None:
        """Test numeric string converts to float."""
        assert _safe_float("12.50", 0.0) == 12.50

    def test_none_returns_fallback(self) -> None:
        """Test None returns fallback."""
        assert _safe_float(None, 42.0) == 42.0

    def test_invalid_string_returns_fallback(self) -> None:
        """Test non-numeric string returns fallback."""
        assert _safe_float("N/A", 99.0) == 99.0

    def test_empty_string_returns_fallback(self) -> None:
        """Test empty string returns fallback."""
        assert _safe_float("", 5.0) == 5.0
