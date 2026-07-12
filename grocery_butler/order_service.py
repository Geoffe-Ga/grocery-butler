"""Order submission and post-order inventory updates.

Submits a built :class:`CartSummary` to the Safeway order API,
handles confirmation and errors, and updates pantry inventory
for successfully ordered items.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from grocery_butler.safeway_client import SafewayTimeoutError

if TYPE_CHECKING:
    from grocery_butler.models import CartItem, CartSummary, FulfillmentType
    from grocery_butler.pantry_manager import PantryManager

logger = logging.getLogger(__name__)


class OrderOutcome(StrEnum):
    """The definitive outcome of an order-submission attempt.

    Distinguishes a clean success/failure from the two cases that matter
    for the duplicate-order guard (Issue #61):

    * ``UNKNOWN`` — the request timed out, so we can't tell whether
      Safeway received it. Must not be silently retried.
    * ``DUPLICATE`` — a matching cart was already submitted recently and
      this attempt was blocked before ever reaching Safeway.
    """

    SUCCESS = "success"
    FAILED = "failed"
    UNKNOWN = "unknown"
    DUPLICATE = "duplicate"


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
        outcome: Definitive outcome of the attempt. If not given
            explicitly, derived from ``success`` (SUCCESS/FAILED); pass it
            explicitly for the UNKNOWN (timeout) and DUPLICATE (blocked
            resubmission) cases.
    """

    success: bool
    confirmation: OrderConfirmation | None = None
    error_message: str = ""
    items_restocked: int = 0
    outcome: OrderOutcome | None = None

    def __post_init__(self) -> None:
        """Derive ``outcome`` from ``success`` when not given explicitly."""
        if self.outcome is None:
            self.outcome = OrderOutcome.SUCCESS if self.success else OrderOutcome.FAILED


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
        idempotency_key: str | None = None,
    ) -> OrderResult:
        """Submit a cart to Safeway and update inventory.

        Sends a ``clientOrderId`` (the given ``idempotency_key``, or a
        freshly generated UUID4 if none is given) with the order so
        repeated attempts can be correlated. The request disables the
        client's automatic 401-retry-and-resend
        (``retry_on_auth_failure=False``): resending a non-idempotent
        order submission after a token refresh risks a double charge, so
        on a stale-token 401 this call fails outright rather than
        silently resending.

        Args:
            cart: The built cart summary to submit.
            idempotency_key: Client order id to send with the request. A
                UUID4 is generated when not given.

        Returns:
            OrderResult with confirmation or error details.
        """
        if not cart.items and not cart.restock_items:
            return OrderResult(
                success=False,
                error_message="Cart is empty — nothing to order",
            )

        client_order_id = idempotency_key or str(uuid.uuid4())
        payload = _build_order_payload(cart, client_order_id)

        try:
            response = self._client.post(
                "/abs/pub/web/orders",
                json_data=payload,
                retry_on_auth_failure=False,
            )
            confirmation = _parse_order_response(response, cart)
        except SafewayTimeoutError:
            logger.exception("Order submission timed out — outcome unknown")
            return OrderResult(
                success=False,
                outcome=OrderOutcome.UNKNOWN,
                error_message=(
                    "Order outcome unknown — the request timed out. Do not "
                    "resubmit; verify your recent Safeway orders first."
                ),
            )
        except Exception:
            # Any other exception here (a non-401 HTTP error such as a 5xx,
            # a non-timeout transport error, or an unexpected response shape
            # that breaks _parse_order_response) is *not* proof Safeway
            # rejected the order — the request may have reached Safeway and
            # been processed before the error surfaced on our end. Treating
            # this as a definitive FAILED (as a plain non-2xx business
            # rejection is, below) would let the duplicate-order guard
            # unblock an immediate resubmission of a cart that might already
            # be charged (Issue #61). Since we cannot tell, this is UNKNOWN,
            # exactly like a timeout.
            logger.exception("Order submission failed — outcome unknown")
            return OrderResult(
                success=False,
                outcome=OrderOutcome.UNKNOWN,
                error_message=(
                    "Order outcome unknown — the request failed unexpectedly. "
                    "Do not resubmit; verify your recent Safeway orders first."
                ),
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

        Called only after Safeway has already confirmed the order, so any
        failure here — including in collecting the ingredient list itself —
        must never propagate: raising at this point would surface a
        successfully-placed, real-money order as an unhandled exception to
        the caller (Issue #61's ledger would then never learn the order
        succeeded, and the API layer has no handler for it besides a bare
        500).

        Args:
            cart: The submitted cart.

        Returns:
            Number of items restocked, or 0 if inventory update failed.
        """
        try:
            ingredients = _collect_restock_ingredients(cart)
            if not ingredients:
                return 0
            return self._pantry.mark_restocked(ingredients)
        except Exception:
            logger.exception("Failed to update inventory after order")
            return 0


# ------------------------------------------------------------------
# Pure helper functions
# ------------------------------------------------------------------


def _build_order_payload(
    cart: CartSummary,
    idempotency_key: str | None = None,
) -> dict[str, Any]:
    """Build the API payload for order submission.

    Args:
        cart: Cart summary to convert.
        idempotency_key: Client order id to include as ``clientOrderId``
            (Issue #61 duplicate-order guard), so Safeway and our own
            ledger can correlate repeated attempts.

    Returns:
        Dict suitable for JSON submission.
    """
    items = _serialize_cart_items(cart.items + cart.restock_items)
    return {
        "items": items,
        "fulfillmentType": cart.recommended_fulfillment.value,
        "estimatedTotal": cart.estimated_total,
        "clientOrderId": idempotency_key,
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
