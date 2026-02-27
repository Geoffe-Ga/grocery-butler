"""Cart building and fulfillment comparison for Safeway orders.

Assembles a :class:`CartSummary` from shopping list items by selecting
products, handling out-of-stock substitutions, querying fulfillment
options, and calculating totals.
"""

from __future__ import annotations

import logging
import math
import re
from typing import TYPE_CHECKING, Any

from grocery_butler.models import (
    CartItem,
    CartSummary,
    FulfillmentOption,
    FulfillmentType,
    SafewayProduct,
    ShoppingListItem,
    SubstitutionResult,
)

if TYPE_CHECKING:
    from grocery_butler.product_search import ProductSearchService
    from grocery_butler.product_selector import ProductSelector
    from grocery_butler.substitution_service import SubstitutionService

logger = logging.getLogger(__name__)


class CartBuildError(Exception):
    """Raised when cart building encounters an unrecoverable error."""


class CartBuilder:
    """Build a Safeway cart from shopping list items.

    Orchestrates product search, selection, substitution, and
    fulfillment comparison into a complete :class:`CartSummary`.

    Args:
        search_service: Service for searching Safeway products.
        product_selector: Claude-assisted product selector.
        substitution_service: Handles out-of-stock substitutions.
        safeway_client: Authenticated Safeway API client.
    """

    def __init__(
        self,
        search_service: ProductSearchService,
        product_selector: ProductSelector,
        substitution_service: SubstitutionService,
        safeway_client: Any,
    ) -> None:
        """Initialize the cart builder.

        Args:
            search_service: Product search service.
            product_selector: Product selector.
            substitution_service: Substitution service.
            safeway_client: Safeway API client.
        """
        self._search = search_service
        self._selector = product_selector
        self._substitution = substitution_service
        self._client = safeway_client

    def build_cart(
        self,
        items: list[ShoppingListItem],
        restock_items: list[ShoppingListItem] | None = None,
    ) -> CartSummary:
        """Build a complete cart from shopping list items.

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

        fulfillment_options = self._get_fulfillment_options()
        recommended = _recommend_fulfillment(fulfillment_options)
        subtotal = _calculate_subtotal(cart_items, restock_cart)
        fee = _get_fulfillment_fee(fulfillment_options, recommended)
        estimated_total = round(subtotal + fee, 2)

        return CartSummary(
            items=cart_items,
            failed_items=failed,
            substituted_items=substituted,
            restock_items=restock_cart,
            subtotal=subtotal,
            fulfillment_options=fulfillment_options,
            recommended_fulfillment=recommended,
            estimated_total=estimated_total,
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _process_item(
        self,
        item: ShoppingListItem,
    ) -> CartItem | SubstitutionResult | None:
        """Process a single shopping list item into a cart item.

        Args:
            item: The shopping list item to process.

        Returns:
            CartItem if successful, SubstitutionResult if substituted,
            or None if no product found.
        """
        candidates = self._search.search_or_cached(item.search_term)
        if not candidates:
            logger.warning("No products found for '%s'", item.search_term)
            return None

        selection = self._selector.select_product(item, candidates)
        product = selection.product

        if product is None:
            return None

        if not product.in_stock:
            return self._handle_out_of_stock(item, product)

        qty = _calculate_quantity(item, product)
        cost = round(product.price * qty, 2)
        return CartItem(
            shopping_list_item=item,
            safeway_product=product,
            quantity_to_order=qty,
            estimated_cost=cost,
        )

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

    def _get_fulfillment_options(self) -> list[FulfillmentOption]:
        """Query available fulfillment options from Safeway.

        Returns:
            List of available fulfillment options.
        """
        try:
            store_id = self._client.store_id
            response = self._client.get(
                f"/abs/pub/web/stores/{store_id}/fulfillment",
            )
            return _parse_fulfillment_response(response)
        except Exception:
            logger.exception("Failed to fetch fulfillment options")
            return _default_fulfillment_options()


# ------------------------------------------------------------------
# Pure helper functions
# ------------------------------------------------------------------


def _calculate_quantity(
    item: ShoppingListItem,
    product: SafewayProduct,
) -> int:
    """Calculate how many units to order based on item needs.

    Parses the product size to determine unit quantity, then
    calculates how many products are needed to fulfill the
    shopping list quantity.

    Args:
        item: Shopping list item with desired quantity.
        product: The product with size information.

    Returns:
        Number of units to order (minimum 1).
    """
    product_qty = _parse_product_size(product.size)
    if product_qty <= 0:
        return 1

    needed = item.quantity / product_qty
    return max(1, math.ceil(needed))


def _parse_product_size(size: str) -> float:
    """Extract numeric quantity from a product size string.

    Args:
        size: Product size string like '2 lb', '16 oz', '1 gal'.

    Returns:
        Numeric quantity or 0.0 if unparseable.
    """
    match = re.match(r"([\d.]+)", size.strip())
    if match:
        try:
            return float(match.group(1))
        except ValueError:
            return 0.0
    return 0.0


def _calculate_subtotal(
    items: list[CartItem],
    restock_items: list[CartItem],
) -> float:
    """Calculate cart subtotal from all items.

    Args:
        items: Regular cart items.
        restock_items: Restock queue cart items.

    Returns:
        Rounded subtotal.
    """
    total = sum(item.estimated_cost for item in items)
    total += sum(item.estimated_cost for item in restock_items)
    return round(total, 2)


def _get_fulfillment_fee(
    options: list[FulfillmentOption],
    recommended: FulfillmentType,
) -> float:
    """Get the fee for the recommended fulfillment type.

    Args:
        options: Available fulfillment options.
        recommended: The recommended fulfillment type.

    Returns:
        Fee amount, or 0.0 if not found.
    """
    for option in options:
        if option.type == recommended:
            return option.fee
    return 0.0


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
                fee=float(entry.get("fee", 0.0)),
                windows=windows,
                next_window=next_win,
            )
        )
    return options


def _default_fulfillment_options() -> list[FulfillmentOption]:
    """Return default fulfillment options when API is unavailable.

    Returns:
        Pickup and delivery with default values.
    """
    return [
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
