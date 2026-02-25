"""Order submission and post-order inventory updates.

Submits a built :class:`CartSummary` to the Safeway order API,
handles confirmation and errors, and updates pantry inventory
for successfully ordered items.
"""

from __future__ import annotations

import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from grocery_butler.models import CartItem, CartSummary, FulfillmentType
    from grocery_butler.pantry_manager import PantryManager

logger = logging.getLogger(__name__)


@dataclass
class OrderConfirmation:
    """Details of a successfully submitted order.

    Attributes:
        order_id: Safeway order identifier.
        status: Order status string.
        estimated_time: Estimated fulfillment time.
        total: Final order total.
        fulfillment_type: Selected fulfillment method.
        item_count: Number of items in the order.
    """

    order_id: str
    status: str
    estimated_time: str
    total: float
    fulfillment_type: FulfillmentType
    item_count: int


@dataclass
class OrderResult:
    """Complete result of an order attempt.

    Attributes:
        success: Whether the order was submitted successfully.
        confirmation: Order confirmation if successful.
        error_message: Error description if failed.
        items_restocked: Number of inventory items updated.
    """

    success: bool
    confirmation: OrderConfirmation | None = None
    error_message: str = ""
    items_restocked: int = 0


class OrderService:
    """Submit carts to Safeway and update inventory.

    Args:
        safeway_client: Authenticated Safeway API client.
        pantry_manager: Pantry manager for inventory updates.
    """

    def __init__(
        self,
        safeway_client: Any,
        pantry_manager: PantryManager,
    ) -> None:
        """Initialize the order service.

        Args:
            safeway_client: Safeway API client.
            pantry_manager: Pantry manager for restocking.
        """
        self._client = safeway_client
        self._pantry = pantry_manager

    def submit_order(
        self,
        cart: CartSummary,
    ) -> OrderResult:
        """Submit a cart to Safeway and update inventory.

        Args:
            cart: The built cart summary to submit.

        Returns:
            OrderResult with confirmation or error details.
        """
        if not cart.items and not cart.restock_items:
            return OrderResult(
                success=False,
                error_message="Cart is empty — nothing to order",
            )

        payload = _build_order_payload(cart)

        try:
            response = self._client.post(
                "/abs/pub/web/orders",
                json_data=payload,
            )
            confirmation = _parse_order_response(response, cart)
        except Exception:
            logger.exception("Order submission failed")
            return OrderResult(
                success=False,
                error_message="Order submission failed — check logs",
            )

        if confirmation is None:
            error_msg = (
                response.get("error", "Unknown order error")
                if isinstance(response, dict)
                else "Unknown order error"
            )
            return OrderResult(success=False, error_message=error_msg)

        restocked = self._restock_ordered_items(cart)

        return OrderResult(
            success=True,
            confirmation=confirmation,
            items_restocked=restocked,
        )

    def _restock_ordered_items(self, cart: CartSummary) -> int:
        """Mark ordered restock items as back in stock.

        Args:
            cart: The submitted cart.

        Returns:
            Number of items restocked.
        """
        ingredients = _collect_restock_ingredients(cart)
        if not ingredients:
            return 0

        try:
            return self._pantry.mark_restocked(ingredients)
        except Exception:
            logger.exception("Failed to update inventory after order")
            return 0


# ------------------------------------------------------------------
# Pure helper functions
# ------------------------------------------------------------------


def _build_order_payload(cart: CartSummary) -> dict[str, Any]:
    """Build the API payload for order submission.

    Args:
        cart: Cart summary to convert.

    Returns:
        Dict suitable for JSON submission.
    """
    items = _serialize_cart_items(cart.items + cart.restock_items)
    return {
        "items": items,
        "fulfillmentType": cart.recommended_fulfillment.value,
        "estimatedTotal": cart.estimated_total,
    }


def _serialize_cart_items(
    items: list[CartItem],
) -> list[dict[str, Any]]:
    """Serialize cart items for the order API.

    Args:
        items: Cart items to serialize.

    Returns:
        List of dicts with product_id and quantity.
    """
    return [
        {
            "productId": item.safeway_product.product_id,
            "quantity": item.quantity_to_order,
        }
        for item in items
    ]


def _parse_order_response(
    response: dict[str, Any],
    cart: CartSummary,
) -> OrderConfirmation | None:
    """Parse Safeway order API response into confirmation.

    Args:
        response: Raw API response dict.
        cart: The submitted cart for context.

    Returns:
        OrderConfirmation or None if response indicates failure.
    """
    if response.get("status") == "error":
        return None

    order_id = response.get("orderId")
    if order_id is None:
        return None

    total_items = len(cart.items) + len(cart.restock_items)
    return OrderConfirmation(
        order_id=str(order_id),
        status=str(response.get("status", "confirmed")),
        estimated_time=str(response.get("estimatedTime", "Unknown")),
        total=_safe_float(response.get("total"), cart.estimated_total),
        fulfillment_type=cart.recommended_fulfillment,
        item_count=total_items,
    )


def _safe_float(value: Any, fallback: float) -> float:
    """Safely convert a value to float, returning fallback on failure.

    Args:
        value: Value to convert (may be None, str, or numeric).
        fallback: Default to return if conversion fails.

    Returns:
        Converted float or fallback.
    """
    if value is None:
        return fallback
    try:
        return float(value)
    except (TypeError, ValueError):
        return fallback


def _collect_restock_ingredients(cart: CartSummary) -> list[str]:
    """Extract ingredient names from restock cart items.

    Args:
        cart: Cart summary with restock items.

    Returns:
        List of ingredient names to restock.
    """
    return [item.shopping_list_item.ingredient for item in cart.restock_items]
