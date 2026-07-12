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

#: Issue #60: real order submission is descoped for v1.0. The Safeway
#: checkout API surface (Okta auth, payment method, delivery slot
#: reservation) is unverified against the live API, so submission is
#: gated off by default and this message is returned instead of ever
#: calling the client.
ORDER_SUBMISSION_DISABLED_MESSAGE = (
    "Order submission is descoped for v1.0 (Issue #60): unverified "
    "checkout surface (no payment method, no delivery slot reservation, "
    "and a likely-invalid Okta client id). Build or review the cart "
    "instead — that dry-run path works today. Once the live checkout "
    "flow has been verified end-to-end, set "
    "SAFEWAY_ORDER_SUBMISSION_ENABLED=true to enable real submissions."
)


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
        submission_enabled: Issue #60 fail-safe gate. Real submission to
            Safeway is descoped for v1.0 because the checkout API surface
            is unverified, so this defaults to ``False``. Callers must
            opt in explicitly (see ``SAFEWAY_ORDER_SUBMISSION_ENABLED``).
    """

    def __init__(
        self,
        safeway_client: Any,
        pantry_manager: PantryManager,
        *,
        submission_enabled: bool = False,
    ) -> None:
        """Initialize the order service.

        Args:
            safeway_client: Safeway API client.
            pantry_manager: Pantry manager for restocking.
            submission_enabled: Issue #60 fail-safe gate. Defaults to
                ``False`` so submission is blocked unless explicitly
                enabled by the caller.
        """
        self._client = safeway_client
        self._pantry = pantry_manager
        self._submission_enabled = submission_enabled

    def submit_order(
        self,
        cart: CartSummary,
        *,
        allow_review_items: bool = False,
    ) -> OrderResult:
        """Submit a cart to Safeway and update inventory.

        Before any money is spent, the cart is checked for items flagged
        by unit-aware quantity math (see :attr:`CartItem.needs_review`).
        Flagged items block submission unless ``allow_review_items`` is
        set, which represents an explicit human override after review.

        Args:
            cart: The built cart summary to submit.
            allow_review_items: If True, bypass the review gate and
                submit even if items are flagged as ``needs_review``.
                Defaults to False (safe/blocking).

        Returns:
            OrderResult with confirmation or error details. If order
            submission is disabled (Issue #60), returns a failure result
            with :data:`ORDER_SUBMISSION_DISABLED_MESSAGE` before any
            other validation or API call.
        """
        if not self._submission_enabled:
            return OrderResult(
                success=False,
                error_message=ORDER_SUBMISSION_DISABLED_MESSAGE,
            )

        if not allow_review_items:
            flagged = _collect_review_items(cart)
            if flagged:
                return OrderResult(
                    success=False,
                    error_message=_format_review_block_message(flagged),
                )

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


def _collect_review_items(cart: CartSummary) -> list[CartItem]:
    """Collect cart items flagged as needing human review.

    Args:
        cart: Cart summary with regular and restock items.

    Returns:
        List of items (from ``items`` and ``restock_items``) whose
        ``needs_review`` flag is True, in cart order.
    """
    return [item for item in cart.items + cart.restock_items if item.needs_review]


def _format_review_block_message(flagged: list[CartItem]) -> str:
    """Build the error message for an order blocked pending review.

    Args:
        flagged: Cart items flagged as ``needs_review``.

    Returns:
        Human-readable message naming each flagged ingredient and its
        review reason, stating the order was blocked pending review.
    """
    named = ", ".join(
        f"{item.shopping_list_item.ingredient} ({item.review_reason})"
        for item in flagged
    )
    return (
        "Order blocked pending review: the following items need "
        f"manual review before ordering: {named}. Re-submit with "
        "allow_review_items=True after confirming quantities."
    )


def _collect_restock_ingredients(cart: CartSummary) -> list[str]:
    """Extract ingredient names from restock cart items.

    Args:
        cart: Cart summary with restock items.

    Returns:
        List of ingredient names to restock.
    """
    return [item.shopping_list_item.ingredient for item in cart.restock_items]
