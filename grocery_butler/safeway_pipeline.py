"""End-to-end Safeway ordering pipeline.

Bootstraps all Safeway services from a :class:`Config`, wires them
together, and exposes a two-step flow: build cart → submit order.
"""

from __future__ import annotations

import logging
from typing import TYPE_CHECKING, Any

from grocery_butler.cart_builder import CartBuilder
from grocery_butler.order_service import OrderResult, OrderService
from grocery_butler.pantry_manager import PantryManager
from grocery_butler.product_search import ProductSearchService
from grocery_butler.product_selector import ProductSelector
from grocery_butler.recipe_store import RecipeStore
from grocery_butler.safeway_client import SafewayClient
from grocery_butler.substitution_service import SubstitutionService

if TYPE_CHECKING:
    from grocery_butler.config import Config
    from grocery_butler.models import CartSummary, ShoppingListItem

logger = logging.getLogger(__name__)


class SafewayPipelineError(Exception):
    """Raised when the pipeline encounters an unrecoverable error."""


class SafewayPipeline:
    """Orchestrate the full Safeway ordering pipeline.

    Bootstraps all required services from a :class:`Config` object and
    provides high-level methods to build carts and submit orders.

    Args:
        config: Application configuration with Safeway credentials.
        db_path: Path to the SQLite database.
        anthropic_client: Optional Anthropic API client for Claude calls.
    """

    def __init__(
        self,
        config: Config,
        db_path: str,
        anthropic_client: Any = None,
    ) -> None:
        """Initialize the pipeline and bootstrap all services.

        Args:
            config: Application configuration with Safeway credentials.
            db_path: Path to the SQLite database.
            anthropic_client: Optional Anthropic API client.

        Raises:
            SafewayPipelineError: If required Safeway config is missing.
        """
        if not config.safeway_username or not config.safeway_password:
            raise SafewayPipelineError(
                "Safeway credentials required: set SAFEWAY_USERNAME "
                "and SAFEWAY_PASSWORD in .env"
            )
        if not config.safeway_store_id:
            raise SafewayPipelineError(
                "Safeway store ID required: set SAFEWAY_STORE_ID in .env"
            )

        self._client = SafewayClient(
            username=config.safeway_username,
            password=config.safeway_password,
            store_id=config.safeway_store_id,
        )

        recipe_store = RecipeStore(db_path)
        search_service = ProductSearchService(self._client, db_path)
        selector = ProductSelector(anthropic_client, recipe_store)
        substitution = SubstitutionService(
            anthropic_client, search_service, recipe_store
        )

        self._cart_builder = CartBuilder(
            search_service, selector, substitution, self._client
        )

        pantry_manager = PantryManager(db_path, anthropic_client)
        self._order_service = OrderService(self._client, pantry_manager)

    def run(
        self,
        items: list[ShoppingListItem],
        restock_items: list[ShoppingListItem] | None = None,
    ) -> OrderResult:
        """Execute the full pipeline: build cart then submit order.

        Args:
            items: Shopping list items to order.
            restock_items: Optional restock items to include.

        Returns:
            OrderResult with confirmation or error details.

        Raises:
            SafewayPipelineError: If authentication fails.
        """
        self._authenticate()
        cart = self._cart_builder.build_cart(items, restock_items)
        return self._order_service.submit_order(cart)

    def build_cart_only(
        self,
        items: list[ShoppingListItem],
        restock_items: list[ShoppingListItem] | None = None,
    ) -> CartSummary:
        """Build cart without submitting (for review).

        Args:
            items: Shopping list items to order.
            restock_items: Optional restock items to include.

        Returns:
            CartSummary with selected products and pricing.

        Raises:
            SafewayPipelineError: If authentication fails.
        """
        self._authenticate()
        return self._cart_builder.build_cart(items, restock_items)

    def close(self) -> None:
        """Clean up SafewayClient HTTP resources."""
        self._client.close()

    def _authenticate(self) -> None:
        """Authenticate with Safeway if not already authenticated.

        Raises:
            SafewayPipelineError: If authentication fails.
        """
        if self._client.is_authenticated:
            return
        try:
            self._client.authenticate()
        except Exception as exc:
            raise SafewayPipelineError(f"Safeway authentication failed: {exc}") from exc
