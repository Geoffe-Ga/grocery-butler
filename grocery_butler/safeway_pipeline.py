"""End-to-end Safeway ordering pipeline.

Bootstraps all Safeway services from a :class:`Config`, wires them
together, and exposes a two-step flow: build cart → submit order.
"""

from __future__ import annotations

import logging
import uuid
from typing import TYPE_CHECKING, Any

from grocery_butler.cart_builder import CartBuilder
from grocery_butler.order_service import (
    ORDER_SUBMISSION_DISABLED_MESSAGE,
    OrderOutcome,
    OrderResult,
    OrderService,
)
from grocery_butler.order_submissions import (
    DUPLICATE_WINDOW,
    OrderSubmissionStore,
    cart_fingerprint,
)
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

_DUPLICATE_ERROR_MESSAGE = (
    "Duplicate order blocked — a matching cart was submitted recently. "
    "Verify your recent Safeway orders before retrying."
)


class SafewayPipelineError(Exception):
    """Raised when the pipeline encounters an unrecoverable error."""


class OrderSubmissionDisabledError(SafewayPipelineError):
    """Raised when order submission is attempted while descoped (Issue #60).

    Real order submission is disabled by default because the Safeway
    checkout API surface is unverified. This is raised as the first
    action of :meth:`SafewayPipeline.run` and
    :meth:`SafewayPipeline.submit_cart` when the gate is off, before any
    authentication or cart-building I/O occurs.
    """


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

        # Issue #60: fail-safe order-submission gate. Defense in depth —
        # OrderService also defaults to disabled, and this pipeline
        # additionally short-circuits run()/submit_cart() before any I/O.
        self._submission_enabled = config.safeway_order_submission_enabled

        pantry_manager = PantryManager(db_path, anthropic_client)
        self._order_service = OrderService(
            self._client,
            pantry_manager,
            submission_enabled=self._submission_enabled,
        )
        self._order_submissions = OrderSubmissionStore(db_path)

    @property
    def order_submission_enabled(self) -> bool:
        """Whether real order submission is enabled (Issue #60 gate).

        Returns:
            True if ``SAFEWAY_ORDER_SUBMISSION_ENABLED`` was set truthy
            when this pipeline's config was loaded, False otherwise.
        """
        return self._submission_enabled

    def run(
        self,
        items: list[ShoppingListItem],
        restock_items: list[ShoppingListItem] | None = None,
        idempotency_key: str | None = None,
    ) -> OrderResult:
        """Execute the full pipeline: build cart then submit order.

        Args:
            items: Shopping list items to order.
            restock_items: Optional restock items to include.
            idempotency_key: Optional client order id forwarded to the
                duplicate-order guard and Safeway (Issue #61).

        Returns:
            OrderResult with confirmation or error details.

        Raises:
            OrderSubmissionDisabledError: If order submission is disabled
                (Issue #60). Raised before authentication or cart
                building.
            SafewayPipelineError: If authentication fails.
        """
        if not self._submission_enabled:
            raise OrderSubmissionDisabledError(ORDER_SUBMISSION_DISABLED_MESSAGE)
        self._authenticate()
        cart = self._cart_builder.build_cart(items, restock_items)
        return self._submit_guarded(cart, idempotency_key)

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

    def submit_cart(
        self,
        cart: CartSummary,
        idempotency_key: str | None = None,
    ) -> OrderResult:
        """Submit a pre-built cart to Safeway.

        Use this when the cart has already been built via
        :meth:`build_cart_only` and the user has confirmed.

        Args:
            cart: Pre-built cart summary to submit.
            idempotency_key: Optional client order id forwarded to the
                duplicate-order guard and Safeway (Issue #61).

        Returns:
            OrderResult with confirmation or error details.

        Raises:
            OrderSubmissionDisabledError: If order submission is disabled
                (Issue #60). Raised before authentication.
            SafewayPipelineError: If authentication fails.
        """
        if not self._submission_enabled:
            raise OrderSubmissionDisabledError(ORDER_SUBMISSION_DISABLED_MESSAGE)
        self._authenticate()
        return self._submit_guarded(cart, idempotency_key)

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

    def _submit_guarded(
        self,
        cart: CartSummary,
        idempotency_key: str | None,
    ) -> OrderResult:
        """Submit a cart through the duplicate-order guard (Issue #61).

        An empty cart bypasses the guard entirely and goes straight to
        :class:`~grocery_butler.order_service.OrderService` (which
        rejects it with its existing "cart is empty" error). Otherwise,
        a recent submission of an identical cart (by content, not price)
        blocks this attempt outright; else the attempt is recorded in
        the ledger *before* the outbound Safeway call (fail-closed) and
        finalized once the result is known.

        Args:
            cart: Cart summary to submit.
            idempotency_key: Client order id, or None to generate one.

        Returns:
            OrderResult with confirmation, error, or DUPLICATE details.
        """
        if not cart.items and not cart.restock_items:
            return self._order_service.submit_order(cart)

        fingerprint = cart_fingerprint(cart)
        if self._order_submissions.find_recent_blocking(fingerprint, DUPLICATE_WINDOW):
            return OrderResult(
                success=False,
                outcome=OrderOutcome.DUPLICATE,
                error_message=_DUPLICATE_ERROR_MESSAGE,
            )

        key = idempotency_key or str(uuid.uuid4())
        submission_id = self._order_submissions.record_attempt(key, fingerprint)

        # Always forward the exact key recorded in the ledger so the
        # clientOrderId sent to Safeway and our ledger row correlate,
        # even when the key was generated here rather than supplied.
        result = self._order_service.submit_order(cart, idempotency_key=key)

        self._finalize_submission(submission_id, result)
        return result

    def _finalize_submission(self, submission_id: int, result: OrderResult) -> None:
        """Record a submission attempt's final status in the ledger.

        Args:
            submission_id: Row id returned by
                :meth:`OrderSubmissionStore.record_attempt`.
            result: The result returned by the order service.
        """
        if result.outcome is OrderOutcome.SUCCESS:
            order_id = result.confirmation.order_id if result.confirmation else None
            self._order_submissions.mark(submission_id, "confirmed", order_id=order_id)
        elif result.outcome is OrderOutcome.UNKNOWN:
            self._order_submissions.mark(submission_id, "unknown")
        else:
            self._order_submissions.mark(submission_id, "failed")
