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
    fulfillment_block_result,
    review_block_result,
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
    from decimal import Decimal

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
        self._order_value_cap = config.safeway_order_value_cap

        pantry_manager = PantryManager(db_path, anthropic_client)
        self._order_service = OrderService(
            self._client,
            pantry_manager,
            submission_enabled=self._submission_enabled,
            order_value_cap=self._order_value_cap,
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

    @property
    def order_value_cap(self) -> Decimal:
        """The configured Safeway order-value cap (Issue #73 gate).

        Returns:
            The cap threshold read from ``config.safeway_order_value_cap``
            when this pipeline was constructed.
        """
        return self._order_value_cap

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
        *,
        allow_review_items: bool = False,
        allow_unverified_fulfillment: bool = False,
        allow_over_cap: bool = False,
    ) -> OrderResult:
        """Submit a pre-built cart to Safeway.

        Use this when the cart has already been built via
        :meth:`build_cart_only` and the user has confirmed.

        Args:
            cart: Pre-built cart summary to submit.
            idempotency_key: Optional client order id forwarded to the
                duplicate-order guard and Safeway (Issue #61).
            allow_review_items: If True, bypass the review gate for
                items flagged as ``needs_review`` (explicit human
                override). Defaults to False (safe/blocking).
            allow_unverified_fulfillment: If True, bypass the
                fulfillment gate for a cart whose fulfillment options
                could not be confirmed with Safeway (explicit human
                override, Issue #72). Defaults to False (safe/blocking).
            allow_over_cap: If True, bypass the Issue #73 order-value
                cap gate for a total exceeding ``order_value_cap``
                (explicit human override). Defaults to False
                (safe/blocking).

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
        return self._submit_guarded(
            cart,
            idempotency_key,
            allow_review_items=allow_review_items,
            allow_unverified_fulfillment=allow_unverified_fulfillment,
            allow_over_cap=allow_over_cap,
        )

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
        *,
        allow_review_items: bool = False,
        allow_unverified_fulfillment: bool = False,
        allow_over_cap: bool = False,
    ) -> OrderResult:
        """Submit a cart through the duplicate-order guard (Issue #61).

        An empty cart bypasses the guard entirely and goes straight to
        :class:`~grocery_butler.order_service.OrderService` (which
        rejects it with its existing "cart is empty" error). A cart with
        items flagged ``needs_review`` is blocked next (Issue #59) —
        unless ``allow_review_items`` overrides — followed by a cart
        flagged ``fulfillment_unverified`` (Issue #72) — unless
        ``allow_unverified_fulfillment`` overrides — both *before* any
        ledger write, so a gate-blocked attempt (which never reaches
        Safeway) can never leave behind a ledger row that would
        spuriously mark a post-override resubmission a duplicate.
        Otherwise, the attempt is atomically recorded in the ledger — or
        rejected as a duplicate — via a single call to
        ``OrderSubmissionStore.try_record_attempt`` (fail-closed, and
        race-safe: a security review found the prior
        ``find_recent_blocking`` + ``record_attempt`` pair vulnerable
        to a check-then-insert race that let two concurrent
        submissions of an identical cart both reach Safeway).

        Args:
            cart: Cart summary to submit.
            idempotency_key: Client order id, or None to generate one.
            allow_review_items: If True, bypass the review gate for
                flagged items (explicit human override). Defaults to
                False (safe/blocking).
            allow_unverified_fulfillment: If True, bypass the
                fulfillment gate for a cart whose fulfillment options
                could not be confirmed with Safeway (explicit human
                override, Issue #72). Defaults to False (safe/blocking).
            allow_over_cap: If True, bypass the Issue #73 order-value
                cap gate (explicit human override). Defaults to False
                (safe/blocking).

        Returns:
            OrderResult with confirmation, error, or DUPLICATE details.
        """
        if not cart.items and not cart.restock_items:
            return self._order_service.submit_order(cart, allow_over_cap=allow_over_cap)

        if not allow_review_items:
            blocked = review_block_result(cart)
            if blocked is not None:
                return blocked

        if not allow_unverified_fulfillment:
            blocked = fulfillment_block_result(cart)
            if blocked is not None:
                return blocked

        fingerprint = cart_fingerprint(cart)
        key = idempotency_key or str(uuid.uuid4())
        # Deliberately NOT wrapped in try/except, in contrast to
        # _finalize_submission below: no money has moved yet, so if the
        # ledger write itself fails (e.g. a transient DB lock) the only
        # safe response is to fail closed and let the error propagate,
        # aborting the submission. Swallowing it here would send a
        # real-money order to Safeway with no duplicate-guard record of
        # the attempt (PR #107 review feedback, Issue #61).
        submission_id = self._order_submissions.try_record_attempt(
            key, fingerprint, DUPLICATE_WINDOW
        )
        if submission_id is None:
            return OrderResult(
                success=False,
                outcome=OrderOutcome.DUPLICATE,
                error_message=_DUPLICATE_ERROR_MESSAGE,
            )

        # Always forward the exact key recorded in the ledger so the
        # clientOrderId sent to Safeway and our ledger row correlate,
        # even when the key was generated here rather than supplied.
        result = self._order_service.submit_order(
            cart,
            idempotency_key=key,
            allow_review_items=allow_review_items,
            allow_unverified_fulfillment=allow_unverified_fulfillment,
            allow_over_cap=allow_over_cap,
        )

        self._finalize_submission(submission_id, result)
        return result

    def _finalize_submission(self, submission_id: int, result: OrderResult) -> None:
        """Record a submission attempt's final status in the ledger.

        Called only after ``OrderService.submit_order`` has already
        returned — for a successful outcome, that means Safeway has
        already confirmed and charged the order — so any failure of the
        ledger write itself must never propagate: raising at this point
        would surface a successfully-placed, real-money order as an
        unhandled exception to the caller, and the caller would lose
        the confirmation/order_id entirely (Gate 2.5 review BLOCKER,
        Issue #61; mirrors
        :meth:`~grocery_butler.order_service.OrderService._restock_ordered_items`).
        The row is left at ``'submitted'`` in this case, which still
        blocks duplicate resubmissions of the same cart for the
        remainder of :data:`~grocery_butler.order_submissions.DUPLICATE_WINDOW`
        (``'submitted'`` is one of the blocking statuses), so the guard's
        protection is unaffected even though the final status update
        was lost.

        Args:
            submission_id: Row id returned by
                :meth:`OrderSubmissionStore.try_record_attempt`.
            result: The result returned by the order service.
        """
        try:
            if result.outcome is OrderOutcome.SUCCESS:
                order_id = result.confirmation.order_id if result.confirmation else None
                self._order_submissions.mark(
                    submission_id, "confirmed", order_id=order_id
                )
            elif result.outcome is OrderOutcome.UNKNOWN:
                self._order_submissions.mark(submission_id, "unknown")
            else:
                self._order_submissions.mark(submission_id, "failed")
        except Exception:
            logger.exception(
                "Failed to record final submission status in the "
                "duplicate-order ledger (submission_id=%s); the order "
                "result itself is unaffected",
                submission_id,
            )
