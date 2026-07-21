"""Cart building and fulfillment comparison for Safeway orders.

Assembles a :class:`CartSummary` from shopping list items by selecting
products, handling out-of-stock substitutions, querying fulfillment
options, and calculating totals.
"""

from __future__ import annotations

import logging
import math
from dataclasses import dataclass
from decimal import ROUND_HALF_UP, Decimal
from typing import TYPE_CHECKING, Any

from grocery_butler.models import (
    CENTS,
    CartItem,
    CartSummary,
    FulfillmentOption,
    FulfillmentType,
    SafewayProduct,
    ShoppingListItem,
    SubstitutionResult,
)
from grocery_butler.product_search import ProductSearchError
from grocery_butler.safeway_client import SafewayAPIError
from grocery_butler.units import convert, parse_size

if TYPE_CHECKING:
    from grocery_butler.product_search import ProductSearchService
    from grocery_butler.product_selector import ProductSelector
    from grocery_butler.substitution_service import SubstitutionService

logger = logging.getLogger(__name__)

#: Default maximum number of product units ordered for a single shopping
#: list item, absent an explicit override. Guards against runaway orders
#: caused by unit-mismatch quantity calculations (issue #59).
MAX_QUANTITY_PER_ITEM = 10


class CartBuildError(Exception):
    """Raised when cart building encounters an unrecoverable error."""


@dataclass(frozen=True)
class QuantityDecision:
    """The outcome of a per-item quantity calculation.

    Attributes:
        quantity: Number of product units to order.
        needs_review: Whether the decision should be flagged for human
            review.
        review_reason: Machine-readable reason code for ``needs_review``
            (``"unparseable_size"``, ``"incomparable_units"``, or
            ``"quantity_capped"``), or ``""`` when no review is needed.
    """

    quantity: int
    needs_review: bool
    review_reason: str


class CartBuilder:
    """Build a Safeway cart from shopping list items.

    Orchestrates product search, selection, substitution, and
    fulfillment comparison into a complete :class:`CartSummary`.

    Args:
        search_service: Service for searching Safeway products.
        product_selector: Claude-assisted product selector.
        substitution_service: Handles out-of-stock substitutions.
        safeway_client: Authenticated Safeway API client.
        max_quantity_per_item: Maximum product units ordered for any single
            shopping list item.
    """

    def __init__(
        self,
        search_service: ProductSearchService,
        product_selector: ProductSelector,
        substitution_service: SubstitutionService,
        safeway_client: Any,
        max_quantity_per_item: int = MAX_QUANTITY_PER_ITEM,
    ) -> None:
        """Initialize the cart builder.

        Args:
            search_service: Product search service.
            product_selector: Product selector.
            substitution_service: Substitution service.
            safeway_client: Safeway API client.
            max_quantity_per_item: Maximum product units ordered for any
                single shopping list item.
        """
        self._search = search_service
        self._selector = product_selector
        self._substitution = substitution_service
        self._client = safeway_client
        self._max_quantity_per_item = max_quantity_per_item

    def build_cart(
        self,
        items: list[ShoppingListItem],
        restock_items: list[ShoppingListItem] | None = None,
    ) -> CartSummary:
        """Build a complete cart from shopping list items.

        Selected substitutions (out-of-stock items with an
        auto-selected alternative) are converted into priced,
        ``needs_review=True`` cart items so they are included in the
        submitted order rather than silently dropped (issue #70). They
        also remain listed in ``substituted_items`` as a display and
        provenance record.

        Args:
            items: Shopping list items to add to cart.
            restock_items: Optional restock queue items.

        Returns:
            CartSummary with all item categories and totals.
        """
        cart_items: list[CartItem] = []
        failed: list[ShoppingListItem] = []
        substituted: list[SubstitutionResult] = []
        restock_cart: list[CartItem] = []

        all_items = list(items)
        restock_set: set[str] = set()
        if restock_items:
            for ri in restock_items:
                all_items.append(ri)
                restock_set.add(ri.ingredient)

        for item in all_items:
            result = self._process_item(item)
            is_restock = item.ingredient in restock_set

            if result is None:
                failed.append(item)
            elif isinstance(result, CartItem):
                if is_restock:
                    restock_cart.append(result)
                else:
                    cart_items.append(result)
            elif isinstance(result, SubstitutionResult):
                substituted.append(result)

        for sub in substituted:
            substitute_item = _substitution_to_cart_item(
                sub, self._max_quantity_per_item
            )
            if substitute_item is not None:
                cart_items.append(substitute_item)

        fulfillment_options, unverified = self._get_fulfillment_options()
        recommended = _recommend_fulfillment(fulfillment_options)
        subtotal = _calculate_subtotal(cart_items, restock_cart)
        fee = _get_fulfillment_fee(fulfillment_options, recommended)
        estimated_total = (subtotal + fee).quantize(CENTS, rounding=ROUND_HALF_UP)

        return CartSummary(
            items=cart_items,
            failed_items=failed,
            substituted_items=substituted,
            restock_items=restock_cart,
            subtotal=subtotal,
            fulfillment_options=fulfillment_options,
            recommended_fulfillment=recommended,
            estimated_total=estimated_total,
            fulfillment_unverified=unverified,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process_item(
        self,
        item: ShoppingListItem,
    ) -> CartItem | SubstitutionResult | None:
        """Process a single shopping list item into a cart item.

        A ``ProductSearchError`` or ``SafewayAPIError`` raised while
        resolving, substituting, or otherwise handling this item is
        recoverable at the per-item level -- it means this one item
        couldn't be sourced, not that the whole cart build should
        abort (issue #76) -- so it's caught here and logged. A
        ``SafewayAuthError`` is deliberately *not* caught: an
        authentication failure dooms every subsequent request, not
        just this item, so it propagates to the caller rather than
        being masked as a single failed item.

        Args:
            item: The shopping list item to process.

        Returns:
            CartItem if successful, SubstitutionResult if substituted,
            or None if no product was found or a recoverable search/API
            error occurred while processing the item (logged as a
            warning).
        """
        try:
            return self._build_cart_result(item)
        except (ProductSearchError, SafewayAPIError) as exc:
            logger.warning(
                "Search failed for '%s'; adding to failed items: %s",
                item.search_term,
                exc,
            )
            return None

    def _build_cart_result(
        self,
        item: ShoppingListItem,
    ) -> CartItem | SubstitutionResult | None:
        """Resolve a shopping list item into a cart item, without error handling.

        Args:
            item: The shopping list item to process.

        Returns:
            CartItem if successful, SubstitutionResult if substituted,
            or None if no product found.

        Raises:
            ProductSearchError: If product search fails.
            SafewayAPIError: If a Safeway API call fails.
            SafewayAuthError: If Safeway authentication fails.
        """
        product = self._resolve_product(item)
        if product is None:
            return None

        if not product.in_stock:
            return self._handle_out_of_stock(item, product)

        decision = _calculate_quantity(item, product, cap=self._max_quantity_per_item)
        cost = (product.price * decision.quantity).quantize(
            CENTS, rounding=ROUND_HALF_UP
        )
        return CartItem(
            shopping_list_item=item,
            safeway_product=product,
            quantity_to_order=decision.quantity,
            estimated_cost=cost,
            needs_review=decision.needs_review,
            review_reason=decision.review_reason,
        )

    def _resolve_product(
        self,
        item: ShoppingListItem,
    ) -> SafewayProduct | None:
        """Resolve a shopping list item to a product via cache or search.

        On a cache hit, the cached product is re-verified against a
        fresh live search (see
        :meth:`ProductSearchService.reverify_product`) so quantity and
        stock decisions never rely on stale cached data. On a cache
        miss, a full search-and-select flow runs instead.

        Args:
            item: The shopping list item to resolve.

        Returns:
            The resolved SafewayProduct, or None if unresolvable.
        """
        cached = self._search.get_cached_product(item.search_term)
        if cached is not None:
            return self._search.reverify_product(cached)
        return self._search_and_select(item)

    def _search_and_select(
        self,
        item: ShoppingListItem,
    ) -> SafewayProduct | None:
        """Search for and select a product on a cache miss.

        Performs a live search, hands the candidates to the product
        selector, and -- when a selection is made -- caches the
        selected product (not merely the top raw search hit) so future
        lookups hit the cache with the right product.

        Args:
            item: The shopping list item to search for.

        Returns:
            The selected SafewayProduct, or None if no products were
            found or none was selected.
        """
        candidates = self._search.search_products(item.search_term)
        if not candidates:
            logger.warning("No products found for '%s'", item.search_term)
            return None

        selection = self._selector.select_product(item, candidates)
        product = selection.product
        if product is None:
            return None

        self._search.save_mapping(item.search_term, product)
        return product

    def _handle_out_of_stock(
        self,
        item: ShoppingListItem,
        product: SafewayProduct,
    ) -> SubstitutionResult:
        """Handle an out-of-stock product via substitution.

        Args:
            item: The shopping list item.
            product: The out-of-stock product.

        Returns:
            SubstitutionResult with best alternative pre-selected.
        """
        result = self._substitution.find_substitutions(item, product)
        if result.alternatives:
            result.selected = result.alternatives[0]
        return result

    def _get_fulfillment_options(self) -> tuple[list[FulfillmentOption], bool]:
        """Query available fulfillment options from Safeway.

        Issue #72 (HIGH): a fetch failure must never be papered over with
        fabricated pickup/delivery options presented as real availability
        and fees. On failure this returns an empty options list and flags
        the result as unverified so callers can warn the human and
        require an explicit override before submitting.

        Returns:
            A tuple of (fulfillment options, unverified). ``unverified``
            is True when the fetch failed, in which case the options
            list is empty; False (with the parsed options) on success.
        """
        try:
            store_id = self._client.store_id
            response = self._client.get(
                f"/abs/pub/web/stores/{store_id}/fulfillment",
            )
            return _parse_fulfillment_response(response), False
        except Exception:
            logger.exception("Failed to fetch fulfillment options")
            return [], True


# ------------------------------------------------------------------
# Pure helper functions
# ------------------------------------------------------------------


#: Epsilon subtracted before ``math.ceil`` so that quantities landing on
#: (or fractionally above, due to floating-point noise) an exact multiple
#: of the product size don't round up to one unit too many.
_QUANTITY_EPSILON = 1e-9


def _calculate_quantity(
    item: ShoppingListItem,
    product: SafewayProduct,
    cap: int = MAX_QUANTITY_PER_ITEM,
) -> QuantityDecision:
    """Calculate how many units to order based on item needs.

    Parses the product size, converts the shopping list item's quantity
    into the product's unit, and determines how many products are needed
    to fulfill the requested amount. Flags the decision for human review
    when the product size can't be parsed, the units can't be compared
    (different physical dimensions), or the computed quantity exceeds
    ``cap``.

    Args:
        item: Shopping list item with desired quantity and unit.
        product: The product with size information.
        cap: Maximum quantity to order without flagging for review.

    Returns:
        A ``QuantityDecision`` with the quantity to order and any review
        flag/reason.
    """
    parsed_size = parse_size(product.size)
    if parsed_size is None:
        return QuantityDecision(1, True, "unparseable_size")

    product_qty, product_unit = parsed_size
    if product_qty <= 0:
        return QuantityDecision(1, True, "unparseable_size")

    converted_qty = convert(item.quantity, item.unit, product_unit)
    if converted_qty is None:
        return QuantityDecision(1, True, "incomparable_units")

    needed = converted_qty / product_qty
    quantity = max(1, math.ceil(needed - _QUANTITY_EPSILON))

    if quantity > cap:
        return QuantityDecision(cap, True, "quantity_capped")

    return QuantityDecision(quantity, False, "")


def _calculate_subtotal(
    items: list[CartItem],
    restock_items: list[CartItem],
) -> Decimal:
    """Calculate cart subtotal from all items.

    Args:
        items: Regular cart items.
        restock_items: Restock queue cart items.

    Returns:
        Subtotal quantized to cents (Decimal, issue #81).
    """
    total = sum((item.estimated_cost for item in items), Decimal("0"))
    total += sum((item.estimated_cost for item in restock_items), Decimal("0"))
    return total.quantize(CENTS, rounding=ROUND_HALF_UP)


def _get_fulfillment_fee(
    options: list[FulfillmentOption],
    recommended: FulfillmentType,
) -> Decimal:
    """Get the fee for the recommended fulfillment type.

    Args:
        options: Available fulfillment options.
        recommended: The recommended fulfillment type.

    Returns:
        Fee amount (Decimal, issue #81), or ``Decimal("0")`` if not found.
    """
    for option in options:
        if option.type == recommended:
            return option.fee
    return Decimal("0")


def _recommend_fulfillment(
    options: list[FulfillmentOption],
) -> FulfillmentType:
    """Recommend the best fulfillment option.

    Prefers pickup if available (usually free), otherwise delivery.

    Args:
        options: Available fulfillment options.

    Returns:
        Recommended fulfillment type.
    """
    available = [o for o in options if o.available]
    if not available:
        return FulfillmentType.PICKUP

    pickup = [o for o in available if o.type == FulfillmentType.PICKUP]
    if pickup:
        return FulfillmentType.PICKUP

    return available[0].type


def _parse_fulfillment_response(
    response: dict[str, Any],
) -> list[FulfillmentOption]:
    """Parse Safeway fulfillment API response.

    Args:
        response: Raw API response dict.

    Returns:
        List of parsed FulfillmentOption.
    """
    options: list[FulfillmentOption] = []
    for entry in response.get("fulfillmentOptions", []):
        try:
            ftype = FulfillmentType(entry.get("type", "pickup"))
        except ValueError:
            continue
        windows = entry.get("windows", [])
        next_win = windows[0].get("display", None) if windows else None
        options.append(
            FulfillmentOption(
                type=ftype,
                available=bool(entry.get("available", False)),
                # float() first preserves the pre-#81 failure mode for
                # garbage fees (ValueError/TypeError); Decimal(str(...))
                # then reads the float at its exact decimal form.
                fee=Decimal(str(float(entry.get("fee", 0.0)))),
                windows=windows,
                next_window=next_win,
            )
        )
    return options


def _substitution_to_cart_item(
    result: SubstitutionResult,
    cap: int,
) -> CartItem | None:
    """Convert a selected substitution into a priced, review-flagged item.

    A substitution is a machine decision (the best available alternative,
    auto-selected by :meth:`CartBuilder._handle_out_of_stock`) and must be
    confirmed by a human before the order is submitted. Per the
    chief-architect's ruling on issue #70, the resulting ``CartItem`` is
    always flagged ``needs_review=True`` with ``review_reason="substitution"``,
    regardless of whether the quantity calculation itself needed review.

    Args:
        result: A ``SubstitutionResult`` produced while processing an
            out-of-stock shopping list item.
        cap: Maximum quantity to order without flagging for review, passed
            through to the underlying quantity calculation.

    Returns:
        A ``CartItem`` for the selected substitute product, or ``None``
        when ``result.selected`` is unset (no alternative was chosen).
    """
    if result.selected is None:
        return None

    product = result.selected.product
    decision = _calculate_quantity(result.original_item, product, cap=cap)
    cost = (product.price * decision.quantity).quantize(CENTS, rounding=ROUND_HALF_UP)
    return CartItem(
        shopping_list_item=result.original_item,
        safeway_product=product,
        quantity_to_order=decision.quantity,
        estimated_cost=cost,
        needs_review=True,
        review_reason="substitution",
    )
