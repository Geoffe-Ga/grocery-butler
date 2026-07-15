"""Order submission and post-order inventory updates.

Submits a built :class:`CartSummary` to the Safeway order API,
handles confirmation and errors, and updates pantry inventory
for successfully ordered items.
"""

from __future__ import annotations

import logging
import uuid
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from grocery_butler.safeway_client import SafewayAuthError, SafewayTimeoutError

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
        submission_enabled: Issue #60 fail-safe gate. Real submission to
            Safeway is descoped for v1.0 because the checkout API surface
            is unverified, so this defaults to ``False``. Callers must
            opt in explicitly (see ``SAFEWAY_ORDER_SUBMISSION_ENABLED``).
        order_value_cap: Issue #73 order-value cap. A cart whose
            fee-inclusive total (see :func:`compute_cart_total`) exceeds
            this amount is blocked before any client call unless the
            caller opts in via ``allow_over_cap``. Defaults to $300.
    """

    def __init__(
        self,
        safeway_client: Any,
        pantry_manager: PantryManager,
        *,
        submission_enabled: bool = False,
        order_value_cap: Decimal = Decimal("300"),
    ) -> None:
        """Initialize the order service.

        Args:
            safeway_client: Safeway API client.
            pantry_manager: Pantry manager for restocking.
            submission_enabled: Issue #60 fail-safe gate. Defaults to
                ``False`` so submission is blocked unless explicitly
                enabled by the caller.
            order_value_cap: Issue #73 order-value cap. Defaults to
                $300; callers should forward the configured
                ``Config.safeway_order_value_cap`` value.
        """
        self._client = safeway_client
        self._pantry = pantry_manager
        self._submission_enabled = submission_enabled
        self._order_value_cap = order_value_cap

    def submit_order(
        self,
        cart: CartSummary,
        idempotency_key: str | None = None,
        *,
        allow_review_items: bool = False,
        allow_unverified_fulfillment: bool = False,
        allow_over_cap: bool = False,
    ) -> OrderResult:
        """Submit a cart to Safeway and update inventory.

        Before any money is spent, the cart is checked for items flagged
        by unit-aware quantity math (see :attr:`CartItem.needs_review`).
        Flagged items block submission unless ``allow_review_items`` is
        set, which represents an explicit human override after review.
        The cart is also checked for unverified fulfillment (Issue #72):
        if Safeway's fulfillment options could not be fetched when the
        cart was built, submission blocks unless
        ``allow_unverified_fulfillment`` is set. Finally, the cart's
        fee-inclusive total (see :func:`compute_cart_total`) is checked
        against ``order_value_cap``; a total exceeding the cap blocks
        submission unless ``allow_over_cap`` is set (Issue #73).

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
            allow_review_items: If True, bypass the review gate and
                submit even if items are flagged as ``needs_review``.
                Defaults to False (safe/blocking).
            allow_unverified_fulfillment: If True, bypass the fulfillment
                gate and submit even if ``cart.fulfillment_unverified``
                is True (Issue #72 explicit human override). Defaults to
                False (safe/blocking).
            allow_over_cap: If True, bypass the order-value cap gate
                (Issue #73) even if the cart total exceeds
                ``order_value_cap``. Defaults to False (safe/blocking).

        Returns:
            OrderResult with confirmation or error details. If order
            submission is disabled (Issue #60), returns a failure result
            with :data:`ORDER_SUBMISSION_DISABLED_MESSAGE` before any
            other validation or API call.
        """
        blocked = self._preflight_block(
            cart,
            allow_review_items=allow_review_items,
            allow_unverified_fulfillment=allow_unverified_fulfillment,
            allow_over_cap=allow_over_cap,
        )
        if blocked is not None:
            return blocked

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
        except SafewayAuthError:
            # An auth failure is definitive, unlike the UNKNOWN cases below.
            # SafewayClient raises SafewayAuthError on exactly two paths:
            # (1) the pre-flight token refresh in _ensure_authenticated()
            # fails before the order request is ever sent, and (2) the order
            # POST itself gets a 401 with retry_on_auth_failure=False, which
            # SafewayClient._request deliberately raises as SafewayAuthError
            # — not plain SafewayAPIError — because a 401 means Safeway
            # rejected the request without processing it (PR #107 round-2
            # review fixed a gap where that path raised SafewayAPIError and
            # fell through to the UNKNOWN handler below). In both paths the
            # order can never have been placed or charged, so this maps to
            # FAILED — an immediately retryable outcome — rather than
            # UNKNOWN, which would make the duplicate-order guard block this
            # cart for the full duplicate window over a transient auth
            # hiccup (Issue #61).
            logger.exception("Order submission failed: Safeway authentication error")
            return OrderResult(
                success=False,
                outcome=OrderOutcome.FAILED,
                error_message=(
                    "Order not submitted — Safeway authentication failed. "
                    "The order never reached Safeway, so it is safe to retry "
                    "once authentication succeeds."
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

    def _preflight_block(
        self,
        cart: CartSummary,
        *,
        allow_review_items: bool,
        allow_unverified_fulfillment: bool,
        allow_over_cap: bool,
    ) -> OrderResult | None:
        """Run the pre-flight gates that block a submission before any I/O.

        Gates run in a deliberate order: the Issue #60 descope gate
        short-circuits first (before any other validation), then the
        Issue #59 review gate (unless overridden), then the Issue #72
        fulfillment gate (unless overridden), then the empty-cart check,
        then the Issue #73 order-value cap gate (unless overridden) —
        all before any HTTP call or ledger write.

        Args:
            cart: The built cart summary to validate.
            allow_review_items: If True, skip the review gate (explicit
                human override).
            allow_unverified_fulfillment: If True, skip the fulfillment
                gate (explicit human override, Issue #72).
            allow_over_cap: If True, skip the order-value cap gate
                (explicit human override, Issue #73).

        Returns:
            A failed OrderResult describing the first gate that blocks
            this submission, or None when the order may proceed.
        """
        if not self._submission_enabled:
            return OrderResult(
                success=False,
                error_message=ORDER_SUBMISSION_DISABLED_MESSAGE,
            )

        if not allow_review_items:
            blocked = review_block_result(cart)
            if blocked is not None:
                return blocked

        if not allow_unverified_fulfillment:
            blocked = fulfillment_block_result(cart)
            if blocked is not None:
                return blocked

        if not cart.items and not cart.restock_items:
            return OrderResult(
                success=False,
                error_message="Cart is empty — nothing to order",
            )

        if not allow_over_cap:
            total = compute_cart_total(cart)
            if total > self._order_value_cap:
                return OrderResult(
                    success=False,
                    error_message=(
                        f"Order total ${total} exceeds the order-value cap "
                        f"of ${self._order_value_cap}. Re-submit with "
                        "allow_over_cap=True to proceed."
                    ),
                )
        return None

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


def compute_cart_total(cart: CartSummary) -> Decimal:
    """Compute the fee-inclusive, server-trusted total for a cart.

    Sums the estimated cost of every regular and restock item and adds
    the recommended fulfillment option's fee (see
    :func:`_recommended_fulfillment_fee`), then quantizes the result to
    cents. Deliberately never reads ``cart.subtotal`` or
    ``cart.estimated_total`` — those fields may be stale or supplied by
    an untrusted client, so this is the single source of truth for what
    a cart actually costs (Issue #73).

    Args:
        cart: Cart summary to total.

    Returns:
        The fee-inclusive total, quantized to cents with ROUND_HALF_UP.
    """
    items_total = sum(
        (Decimal(str(item.estimated_cost)) for item in cart.items + cart.restock_items),
        Decimal("0"),
    )
    total = items_total + _recommended_fulfillment_fee(cart)
    return total.quantize(Decimal("0.01"), rounding=ROUND_HALF_UP)


def _recommended_fulfillment_fee(cart: CartSummary) -> Decimal:
    """Return the fee for the cart's recommended fulfillment option.

    Deliberately does not import
    ``grocery_butler.cart_builder._get_fulfillment_fee`` — sibling
    changes land in ``cart_builder.py`` independently, and this module
    must not couple to it (chief-architect ruling, Issue #73).

    Args:
        cart: Cart summary whose fulfillment options to scan.

    Returns:
        The fee of the fulfillment option matching
        ``cart.recommended_fulfillment``, or ``Decimal("0")`` if no
        option matches.
    """
    for option in cart.fulfillment_options:
        if option.type == cart.recommended_fulfillment:
            return Decimal(str(option.fee))
    return Decimal("0")


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


def review_block_result(cart: CartSummary) -> OrderResult | None:
    """Return the blocking result for a cart pending review, if any.

    Shared by :meth:`OrderService.submit_order` and the pipeline's
    duplicate-order guard so the review gate (Issue #59) is enforced
    once, consistently, *before* any ledger write or HTTP call — a
    review-blocked attempt must never record a duplicate-guard ledger
    row, because it never reaches Safeway (Issue #61).

    Args:
        cart: Cart summary to inspect for ``needs_review`` items.

    Returns:
        A failed :class:`OrderResult` naming every flagged item if any
        item needs review, or None when nothing blocks submission.
    """
    flagged = _collect_review_items(cart)
    if not flagged:
        return None
    return OrderResult(
        success=False,
        error_message=_format_review_block_message(flagged),
    )


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


def fulfillment_block_result(cart: CartSummary) -> OrderResult | None:
    """Return the blocking result for a cart with unverified fulfillment.

    Shared by :meth:`OrderService.submit_order` and the pipeline's
    duplicate-order guard so the fulfillment gate (Issue #72) is
    enforced once, consistently, *before* any ledger write or HTTP call
    — a fulfillment-blocked attempt must never record a duplicate-guard
    ledger row, because it never reaches Safeway (Issue #61).

    Issue #72 (HIGH): when Safeway's fulfillment options could not be
    fetched while building the cart, ``CartBuilder`` previously
    substituted fabricated defaults (free pickup, $9.95 delivery, both
    marked available) instead of surfacing the failure — presenting
    invented availability and fees as if fulfillment had been confirmed.
    The fix flags such a cart ``fulfillment_unverified`` and this
    function blocks it here, honestly, before any money moves.

    Args:
        cart: Cart summary to inspect for ``fulfillment_unverified``.

    Returns:
        A failed :class:`OrderResult` explaining that fulfillment could
        not be confirmed with Safeway, or None when nothing blocks
        submission.
    """
    if not cart.fulfillment_unverified:
        return None
    return OrderResult(
        success=False,
        error_message=(
            "Order blocked — fulfillment options could not be confirmed "
            "with Safeway, so pickup/delivery availability and fees are "
            "unverified. Re-submit with "
            "allow_unverified_fulfillment=True after confirming "
            "fulfillment manually."
        ),
    )


def _collect_restock_ingredients(cart: CartSummary) -> list[str]:
    """Extract ingredient names from restock cart items.

    Args:
        cart: Cart summary with restock items.

    Returns:
        List of ingredient names to restock.
    """
    return [item.shopping_list_item.ingredient for item in cart.restock_items]
