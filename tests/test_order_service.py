"""Tests for grocery_butler.order_service module."""

from __future__ import annotations

from typing import Any
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

    def test_integer_order_id_accepted(self) -> None:
        """Test integer orderId (including 0) is accepted."""
        response = {"orderId": 0, "status": "confirmed"}
        cart = _make_cart()
        result = _parse_order_response(response, cart)

        assert result is not None
        assert result.order_id == "0"


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
        api_response: dict[str, Any] | None = None,
        api_error: bool = False,
        submission_enabled: bool = True,
    ) -> OrderService:
        """Create an OrderService with mock dependencies.

        Args:
            api_response: Response from Safeway API.
            api_error: Whether API should raise an exception.
            submission_enabled: Issue #60 gate — defaults to True here so
                the pre-existing submission-path tests in this file keep
                exercising the real submit flow (the OrderService default
                is False; production callers must opt in explicitly).

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

        return OrderService(
            mock_client, mock_pantry, submission_enabled=submission_enabled
        )

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
        """Test a non-timeout API exception returns an unknown (not failed) outcome.

        Issue #61: an unclassified exception from the client call (e.g. a
        5xx or a broken response) is not proof the order wasn't placed, so
        it must map to UNKNOWN — the same as a timeout — rather than FAILED,
        which would wrongly unblock an immediate resubmission.
        """
        from grocery_butler.order_service import OrderOutcome

        service = self._make_service(api_error=True)
        result = service.submit_order(_make_cart())

        assert result.success is False
        assert result.outcome is OrderOutcome.UNKNOWN
        assert "unknown" in result.error_message.lower()

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

    def test_restock_ingredient_collection_failure_doesnt_fail_order(self) -> None:
        """Test a bug collecting restock ingredients doesn't crash a confirmed order.

        Issue #61: this runs after Safeway has already confirmed the order,
        so a bug here must never surface as an unhandled exception out of
        submit_order — that would report a real, already-placed order as an
        unexplained crash instead of a success.
        """
        from unittest.mock import patch

        service = self._make_service(
            api_response={
                "orderId": "ORD-123",
                "status": "confirmed",
                "total": 5.0,
            }
        )
        restock = [_make_cart_item(ingredient="milk")]
        cart = _make_cart(restock_items=restock)

        with patch(
            "grocery_butler.order_service._collect_restock_ingredients",
            side_effect=AttributeError("malformed cart item"),
        ):
            result = service.submit_order(cart)

        assert result.success is True
        assert result.confirmation is not None
        assert result.confirmation.order_id == "ORD-123"
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
# Tests: Issue #60 — order submission descoped for v1.0 by default
# ------------------------------------------------------------------


class TestSubmissionDisabledGate:
    """Tests for the Issue #60 fail-safe order-submission gate."""

    def test_default_construction_blocks_submission(self) -> None:
        """Test default OrderService blocks submission without calling out."""
        from grocery_butler.order_service import ORDER_SUBMISSION_DISABLED_MESSAGE

        mock_client = MagicMock()
        mock_pantry = MagicMock()
        service = OrderService(mock_client, mock_pantry)

        result = service.submit_order(_make_cart())

        assert result.success is False
        assert result.error_message == ORDER_SUBMISSION_DISABLED_MESSAGE
        mock_client.post.assert_not_called()
        mock_pantry.mark_restocked.assert_not_called()

    def test_explicit_disabled_blocks_submission(self) -> None:
        """Test submission_enabled=False explicitly blocks submission."""
        from grocery_butler.order_service import ORDER_SUBMISSION_DISABLED_MESSAGE

        mock_client = MagicMock()
        mock_pantry = MagicMock()
        service = OrderService(mock_client, mock_pantry, submission_enabled=False)

        result = service.submit_order(_make_cart())

        assert result.success is False
        assert result.error_message == ORDER_SUBMISSION_DISABLED_MESSAGE
        mock_client.post.assert_not_called()
        mock_pantry.mark_restocked.assert_not_called()

    def test_disabled_blocks_even_empty_cart(self) -> None:
        """Test the disabled gate fires before any other submission logic.

        Regardless of whether the guard is checked before or after the
        empty-cart check, a disabled service must never reach the client.
        """
        from grocery_butler.order_service import ORDER_SUBMISSION_DISABLED_MESSAGE

        mock_client = MagicMock()
        mock_pantry = MagicMock()
        service = OrderService(mock_client, mock_pantry, submission_enabled=False)

        result = service.submit_order(_make_cart(items=[], restock_items=[]))

        assert result.success is False
        assert result.error_message == ORDER_SUBMISSION_DISABLED_MESSAGE
        mock_client.post.assert_not_called()

    def test_disabled_message_is_actionable(self) -> None:
        """Test the disabled message references Issue #60 and the enable var."""
        from grocery_butler.order_service import ORDER_SUBMISSION_DISABLED_MESSAGE

        message = ORDER_SUBMISSION_DISABLED_MESSAGE
        assert "Issue #60" in message
        assert "SAFEWAY_ORDER_SUBMISSION_ENABLED=true" in message
        assert "unverified" in message.lower()
        assert "v1.0" in message
        # Actionable alternative: build/review still works.
        assert "review" in message.lower() or "build" in message.lower()

    def test_enabled_true_preserves_happy_path(self) -> None:
        """Test submission_enabled=True preserves the existing successful flow."""
        mock_client = MagicMock()
        mock_client.post.return_value = {
            "orderId": "ORD-999",
            "status": "confirmed",
            "total": 8.99,
        }
        mock_pantry = MagicMock()
        mock_pantry.mark_restocked.return_value = 0
        service = OrderService(mock_client, mock_pantry, submission_enabled=True)

        result = service.submit_order(_make_cart())

        assert result.success is True
        assert result.confirmation is not None
        assert result.confirmation.order_id == "ORD-999"
        mock_client.post.assert_called_once()


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


# ------------------------------------------------------------------
# Issue #61: OrderOutcome, timeout handling, clientOrderId plumbing
#
# The new names (OrderOutcome, SafewayTimeoutError, and the
# idempotency_key/retry_on_auth_failure plumbing) do not exist yet. They
# are imported inside each test body rather than at module scope so the
# pre-existing tests above keep collecting and passing before the
# feature lands.
# ------------------------------------------------------------------


class TestOrderOutcomeDefaults:
    """Tests for OrderOutcome derivation on OrderResult (Issue #61)."""

    def test_success_result_defaults_to_success_outcome(self) -> None:
        """Test a successful OrderResult derives OrderOutcome.SUCCESS."""
        from grocery_butler.order_service import OrderOutcome, OrderResult

        result = OrderResult(success=True)
        assert result.outcome is OrderOutcome.SUCCESS

    def test_failed_result_defaults_to_failed_outcome(self) -> None:
        """Test a failed OrderResult derives OrderOutcome.FAILED."""
        from grocery_butler.order_service import OrderOutcome, OrderResult

        result = OrderResult(success=False)
        assert result.outcome is OrderOutcome.FAILED

    def test_explicit_outcome_is_preserved(self) -> None:
        """Test an explicitly-set outcome is not overridden by success/failed."""
        from grocery_butler.order_service import OrderOutcome, OrderResult

        result = OrderResult(success=False, outcome=OrderOutcome.DUPLICATE)
        assert result.outcome is OrderOutcome.DUPLICATE

    def test_explicit_unknown_outcome_preserved_when_success_true(self) -> None:
        """Test an explicit UNKNOWN outcome is preserved regardless of success."""
        from grocery_butler.order_service import OrderOutcome, OrderResult

        result = OrderResult(success=True, outcome=OrderOutcome.UNKNOWN)
        assert result.outcome is OrderOutcome.UNKNOWN


class TestSubmitOrderTimeout:
    """Tests for OrderService.submit_order handling SafewayTimeoutError."""

    def _make_service_with_error(self, error: Exception) -> OrderService:
        """Create an OrderService whose client.post raises the given error.

        Args:
            error: Exception the mocked client should raise on post().

        Returns:
            OrderService wired to a client that always raises ``error``.
        """
        mock_client = MagicMock()
        mock_client.post.side_effect = error
        mock_pantry = MagicMock()
        # Issue #60: submission is gated off by default; these tests
        # exercise the real submission path, so opt in explicitly.
        return OrderService(mock_client, mock_pantry, submission_enabled=True)

    def test_timeout_returns_unknown_outcome(self) -> None:
        """Test SafewayTimeoutError yields a failed result with UNKNOWN outcome."""
        from grocery_butler.order_service import OrderOutcome
        from grocery_butler.safeway_client import SafewayTimeoutError

        service = self._make_service_with_error(SafewayTimeoutError("timed out"))
        result = service.submit_order(_make_cart())

        assert result.success is False
        assert result.outcome is OrderOutcome.UNKNOWN

    def test_timeout_error_message_mentions_unknown_and_timed_out(self) -> None:
        """Test the timeout error message references an unknown outcome and timeout."""
        from grocery_butler.safeway_client import SafewayTimeoutError

        service = self._make_service_with_error(SafewayTimeoutError("timed out"))
        result = service.submit_order(_make_cart())

        message = result.error_message.lower()
        assert "unknown" in message
        assert "timed out" in message

    def test_other_exception_returns_unknown_outcome(self) -> None:
        """Test a non-timeout exception yields UNKNOWN, not FAILED (Issue #61).

        A generic exception from the client call (e.g. a 5xx SafewayAPIError,
        a non-timeout transport error, or a malformed-response parsing bug)
        is not a definitive rejection: the request may have already reached
        and been processed by Safeway. Marking it FAILED would incorrectly
        unblock an immediate resubmission of a cart that might already be
        charged, so it must map to UNKNOWN like a timeout does.
        """
        from grocery_butler.order_service import OrderOutcome

        service = self._make_service_with_error(Exception("boom"))
        result = service.submit_order(_make_cart())

        assert result.success is False
        assert result.outcome is OrderOutcome.UNKNOWN


class TestSubmitOrderClientOrderId:
    """Tests for clientOrderId plumbing and retry_on_auth_failure (Issue #61)."""

    def _make_service(self) -> tuple[OrderService, MagicMock]:
        """Create an OrderService with a mock client returning a success payload.

        Returns:
            Tuple of (service, mock_client) so tests can inspect call args.
        """
        mock_client = MagicMock()
        mock_client.post.return_value = {
            "orderId": "ORD-1",
            "status": "confirmed",
            "total": 8.99,
        }
        mock_pantry = MagicMock()
        mock_pantry.mark_restocked.return_value = 0
        # Issue #60: submission is gated off by default; these tests
        # exercise the real submission path, so opt in explicitly.
        service = OrderService(mock_client, mock_pantry, submission_enabled=True)
        return service, mock_client

    def test_explicit_idempotency_key_becomes_client_order_id(self) -> None:
        """Test a passed idempotency_key is used as clientOrderId in the payload."""
        service, mock_client = self._make_service()

        service.submit_order(_make_cart(), idempotency_key="key-123")

        _args, kwargs = mock_client.post.call_args
        payload = kwargs.get("json_data") or _args[1]
        assert payload["clientOrderId"] == "key-123"

    def test_generated_client_order_id_is_a_uuid(self) -> None:
        """Test a generated clientOrderId parses as a UUID when none is given."""
        import uuid

        service, mock_client = self._make_service()

        service.submit_order(_make_cart())

        _args, kwargs = mock_client.post.call_args
        payload = kwargs.get("json_data") or _args[1]
        client_order_id = payload["clientOrderId"]
        assert isinstance(client_order_id, str)
        assert client_order_id
        uuid.UUID(client_order_id)  # raises ValueError if not a valid UUID

    def test_post_called_with_retry_on_auth_failure_false(self) -> None:
        """Test submit_order disables the client's automatic 401-retry-and-resend."""
        service, mock_client = self._make_service()

        service.submit_order(_make_cart())

        _args, kwargs = mock_client.post.call_args
        assert kwargs.get("retry_on_auth_failure") is False
