"""Tests for grocery_butler.safeway_pipeline module."""

from __future__ import annotations

import threading
import uuid
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

from grocery_butler.config import Config
from grocery_butler.models import (
    CartItem,
    CartSummary,
    FulfillmentType,
    IngredientCategory,
    SafewayProduct,
    ShoppingListItem,
)
from grocery_butler.order_service import OrderConfirmation, OrderResult
from grocery_butler.safeway_pipeline import SafewayPipeline, SafewayPipelineError

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def safeway_config() -> Config:
    """Return a Config with Safeway credentials set.

    Issue #60: order submission is opted in here (``True``) so the
    pre-existing run/submit_cart tests below keep exercising the real
    submission path — the ``Config`` default is ``False``.
    """
    return Config(
        anthropic_api_key="sk-test",
        safeway_username="user@example.com",
        safeway_password="secret",
        safeway_store_id="1234",
        database_path=":memory:",
        safeway_order_submission_enabled=True,
    )


@pytest.fixture()
def disabled_safeway_config() -> Config:
    """Return a Config with Safeway credentials set but submission disabled."""
    return Config(
        anthropic_api_key="sk-test",
        safeway_username="user@example.com",
        safeway_password="secret",
        safeway_store_id="1234",
        database_path=":memory:",
        safeway_order_submission_enabled=False,
    )


@pytest.fixture()
def incomplete_config() -> Config:
    """Return a Config missing Safeway credentials."""
    return Config(
        anthropic_api_key="sk-test",
        safeway_username="",
        safeway_password="",
        safeway_store_id="",
    )


@pytest.fixture()
def sample_items() -> list[ShoppingListItem]:
    """Return sample shopping list items."""
    return [
        ShoppingListItem(
            ingredient="milk",
            quantity=1.0,
            unit="gal",
            category=IngredientCategory.DAIRY,
            search_term="milk",
            from_meals=["manual"],
        ),
        ShoppingListItem(
            ingredient="eggs",
            quantity=1.0,
            unit="dozen",
            category=IngredientCategory.DAIRY,
            search_term="eggs",
            from_meals=["manual"],
        ),
    ]


@pytest.fixture()
def mock_cart_summary() -> CartSummary:
    """Return a mock CartSummary for testing."""
    product = SafewayProduct(
        product_id="P001",
        name="Whole Milk 1 gal",
        price=4.99,
        size="1 gal",
    )
    cart_item = CartItem(
        shopping_list_item=ShoppingListItem(
            ingredient="milk",
            quantity=1.0,
            unit="gal",
            category=IngredientCategory.DAIRY,
            search_term="milk",
            from_meals=["manual"],
        ),
        safeway_product=product,
        quantity_to_order=1,
        estimated_cost=4.99,
    )
    return CartSummary(
        items=[cart_item],
        failed_items=[],
        substituted_items=[],
        skipped_items=[],
        restock_items=[],
        subtotal=4.99,
        fulfillment_options=[],
        recommended_fulfillment=FulfillmentType.PICKUP,
        estimated_total=4.99,
    )


# ---------------------------------------------------------------------------
# Constructor tests
# ---------------------------------------------------------------------------


class TestSafewayPipelineInit:
    """Tests for SafewayPipeline constructor."""

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    def test_bootstrap_services(
        self,
        mock_pantry: MagicMock,
        mock_client: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        safeway_config: Config,
    ):
        """Test that all services are bootstrapped from config."""
        pipeline = SafewayPipeline(safeway_config, ":memory:")

        mock_client.assert_called_once_with(
            username="user@example.com",
            password="secret",
            store_id="1234",
        )
        mock_store.assert_called_once_with(":memory:")
        mock_search.assert_called_once()
        mock_selector.assert_called_once()
        mock_sub.assert_called_once()
        mock_pantry.assert_called_once()
        assert pipeline is not None

    def test_missing_credentials_raises(self, incomplete_config: Config):
        """Test that missing Safeway creds raises error."""
        with pytest.raises(SafewayPipelineError, match="credentials"):
            SafewayPipeline(incomplete_config, ":memory:")

    def test_missing_store_id_raises(self):
        """Test that missing store ID raises error."""
        cfg = Config(
            anthropic_api_key="sk-test",
            safeway_username="user@example.com",
            safeway_password="secret",
            safeway_store_id="",
        )
        with pytest.raises(SafewayPipelineError, match="store ID"):
            SafewayPipeline(cfg, ":memory:")


# ---------------------------------------------------------------------------
# Pipeline execution tests
# ---------------------------------------------------------------------------


class TestSafewayPipelineRun:
    """Tests for SafewayPipeline.run method."""

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    @patch("grocery_butler.safeway_pipeline.CartBuilder")
    @patch("grocery_butler.safeway_pipeline.OrderService")
    def test_run_success(
        self,
        mock_order_cls: MagicMock,
        mock_cart_cls: MagicMock,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        safeway_config: Config,
        sample_items: list[ShoppingListItem],
        mock_cart_summary: CartSummary,
    ):
        """Test successful full pipeline run."""
        mock_client = mock_client_cls.return_value
        mock_client.is_authenticated = False

        mock_cart_builder = mock_cart_cls.return_value
        mock_cart_builder.build_cart.return_value = mock_cart_summary

        expected_result = OrderResult(
            success=True,
            confirmation=OrderConfirmation(
                order_id="ORD-001",
                status="confirmed",
                estimated_time="2h",
                total=4.99,
                fulfillment_type=FulfillmentType.PICKUP,
                item_count=1,
            ),
            items_restocked=0,
        )
        mock_order_cls.return_value.submit_order.return_value = expected_result

        pipeline = SafewayPipeline(safeway_config, ":memory:")
        result = pipeline.run(sample_items)

        mock_client.authenticate.assert_called_once()
        mock_cart_builder.build_cart.assert_called_once_with(sample_items, None)
        assert result.success is True
        assert result.confirmation is not None
        assert result.confirmation.order_id == "ORD-001"

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    def test_auth_failure_raises(
        self,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        safeway_config: Config,
        sample_items: list[ShoppingListItem],
    ):
        """Test that auth failure raises SafewayPipelineError."""
        mock_client = mock_client_cls.return_value
        mock_client.is_authenticated = False
        mock_client.authenticate.side_effect = RuntimeError("auth failed")

        pipeline = SafewayPipeline(safeway_config, ":memory:")

        with pytest.raises(SafewayPipelineError, match="authentication failed"):
            pipeline.run(sample_items)


# ---------------------------------------------------------------------------
# Build cart only tests
# ---------------------------------------------------------------------------


class TestBuildCartOnly:
    """Tests for SafewayPipeline.build_cart_only method."""

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    @patch("grocery_butler.safeway_pipeline.CartBuilder")
    def test_build_cart_only_returns_summary(
        self,
        mock_cart_cls: MagicMock,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        safeway_config: Config,
        sample_items: list[ShoppingListItem],
        mock_cart_summary: CartSummary,
    ):
        """Test that build_cart_only returns CartSummary."""
        mock_client = mock_client_cls.return_value
        mock_client.is_authenticated = True

        mock_cart_cls.return_value.build_cart.return_value = mock_cart_summary

        pipeline = SafewayPipeline(safeway_config, ":memory:")
        cart = pipeline.build_cart_only(sample_items)

        assert cart is mock_cart_summary
        mock_client.authenticate.assert_not_called()

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    @patch("grocery_butler.safeway_pipeline.CartBuilder")
    def test_build_cart_with_restock(
        self,
        mock_cart_cls: MagicMock,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        safeway_config: Config,
        sample_items: list[ShoppingListItem],
        mock_cart_summary: CartSummary,
    ):
        """Test build_cart_only passes restock items."""
        mock_client_cls.return_value.is_authenticated = True
        mock_cart_cls.return_value.build_cart.return_value = mock_cart_summary

        restock = [
            ShoppingListItem(
                ingredient="butter",
                quantity=1.0,
                unit="lb",
                category=IngredientCategory.DAIRY,
                search_term="butter",
                from_meals=["restock"],
            )
        ]

        pipeline = SafewayPipeline(safeway_config, ":memory:")
        pipeline.build_cart_only(sample_items, restock_items=restock)

        mock_cart_cls.return_value.build_cart.assert_called_once_with(
            sample_items, restock
        )


# ---------------------------------------------------------------------------
# Close tests
# ---------------------------------------------------------------------------


class TestSafewayPipelineClose:
    """Tests for SafewayPipeline.close method."""

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    def test_close_calls_client_close(
        self,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        safeway_config: Config,
    ):
        """Test that close cleans up client resources."""
        pipeline = SafewayPipeline(safeway_config, ":memory:")
        pipeline.close()

        mock_client_cls.return_value.close.assert_called_once()


# ---------------------------------------------------------------------------
# Submit cart tests
# ---------------------------------------------------------------------------


class TestSubmitCart:
    """Tests for SafewayPipeline.submit_cart method."""

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    @patch("grocery_butler.safeway_pipeline.CartBuilder")
    @patch("grocery_butler.safeway_pipeline.OrderService")
    def test_submit_cart_calls_order_service(
        self,
        mock_order_cls: MagicMock,
        mock_cart_cls: MagicMock,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        safeway_config: Config,
        mock_cart_summary: CartSummary,
    ):
        """Test submit_cart delegates to order service without rebuilding.

        When no idempotency key is supplied, the pipeline generates a
        UUID4 key and forwards it to the order service so the ledger row
        and the ``clientOrderId`` sent to Safeway always correlate
        (Issue #61).
        """
        mock_client = mock_client_cls.return_value
        mock_client.is_authenticated = True

        expected = OrderResult(success=True)
        mock_order_cls.return_value.submit_order.return_value = expected

        pipeline = SafewayPipeline(safeway_config, ":memory:")
        result = pipeline.submit_cart(mock_cart_summary)

        assert result is expected
        mock_submit = mock_order_cls.return_value.submit_order
        mock_submit.assert_called_once()
        args, kwargs = mock_submit.call_args
        assert args == (mock_cart_summary,)
        assert kwargs["allow_review_items"] is False
        generated_key = kwargs["idempotency_key"]
        assert uuid.UUID(generated_key).version == 4
        mock_cart_cls.return_value.build_cart.assert_not_called()


# ---------------------------------------------------------------------------
# Submit cart review-gate forwarding tests (issue #59)
# ---------------------------------------------------------------------------
#
# Gate 2.5 blocker: SafewayPipeline.submit_cart must accept and forward
# allow_review_items to OrderService.submit_order so bot/api/cli callers
# can pass the human override through, while defaulting to the safe
# (blocked) behavior when not supplied.


class TestSubmitCartReviewGate:
    """Tests for SafewayPipeline.submit_cart forwarding allow_review_items."""

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    @patch("grocery_butler.safeway_pipeline.CartBuilder")
    @patch("grocery_butler.safeway_pipeline.OrderService")
    def test_submit_cart_blocks_flagged_cart_by_default(
        self,
        mock_order_cls: MagicMock,
        mock_cart_cls: MagicMock,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        safeway_config: Config,
        mock_cart_summary: CartSummary,
    ):
        """Test submit_cart forwards the default (blocking) review gate.

        Without an explicit override, submit_cart must call
        OrderService.submit_order with allow_review_items=False so a
        flagged cart is blocked pending review.
        """
        mock_client_cls.return_value.is_authenticated = True
        blocked = OrderResult(
            success=False,
            error_message="Order blocked pending review: flour (incomparable_units)",
        )
        mock_order_cls.return_value.submit_order.return_value = blocked

        pipeline = SafewayPipeline(safeway_config, ":memory:")
        result = pipeline.submit_cart(mock_cart_summary)

        assert result is blocked
        mock_submit = mock_order_cls.return_value.submit_order
        mock_submit.assert_called_once()
        args, kwargs = mock_submit.call_args
        assert args == (mock_cart_summary,)
        assert kwargs["allow_review_items"] is False

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    @patch("grocery_butler.safeway_pipeline.CartBuilder")
    @patch("grocery_butler.safeway_pipeline.OrderService")
    def test_submit_cart_forwards_allow_review_items_true(
        self,
        mock_order_cls: MagicMock,
        mock_cart_cls: MagicMock,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        safeway_config: Config,
        mock_cart_summary: CartSummary,
    ):
        """Test submit_cart forwards an explicit human override.

        Callers (bot confirm view, API confirm endpoint) must be able to
        pass allow_review_items=True through submit_cart to proceed with
        a flagged cart after explicit human confirmation.
        """
        mock_client_cls.return_value.is_authenticated = True
        expected = OrderResult(success=True)
        mock_order_cls.return_value.submit_order.return_value = expected

        pipeline = SafewayPipeline(safeway_config, ":memory:")
        result = pipeline.submit_cart(mock_cart_summary, allow_review_items=True)

        assert result is expected
        mock_submit = mock_order_cls.return_value.submit_order
        mock_submit.assert_called_once()
        args, kwargs = mock_submit.call_args
        assert args == (mock_cart_summary,)
        assert kwargs["allow_review_items"] is True

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    @patch("grocery_butler.safeway_pipeline.CartBuilder")
    @patch("grocery_butler.safeway_pipeline.OrderService")
    def test_submit_cart_flagged_cart_blocked_before_ledger_write(
        self,
        mock_order_cls: MagicMock,
        mock_cart_cls: MagicMock,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        safeway_config: Config,
        mock_cart_summary: CartSummary,
        tmp_path: Path,
    ) -> None:
        """Test a flagged cart is review-blocked before any ledger write.

        The review gate (issue #59) must short-circuit in the pipeline
        itself, ahead of the duplicate-order ledger (Issue #61): a
        review-blocked attempt never reaches Safeway, so it must not
        record a ledger row that would spuriously mark the post-review
        override resubmission of the same cart a duplicate.
        """
        from grocery_butler.order_submissions import DUPLICATE_WINDOW, cart_fingerprint

        mock_client_cls.return_value.is_authenticated = True

        flagged_item = mock_cart_summary.items[0].model_copy(
            update={"needs_review": True, "review_reason": "incomparable_units"}
        )
        flagged_cart = mock_cart_summary.model_copy(update={"items": [flagged_item]})

        pipeline = SafewayPipeline(safeway_config, str(tmp_path / "orders.db"))
        result = pipeline.submit_cart(flagged_cart, idempotency_key="key-review")

        assert result.success is False
        assert "review" in result.error_message.lower()
        assert "milk (incomparable_units)" in result.error_message
        mock_order_cls.return_value.submit_order.assert_not_called()
        row = pipeline._order_submissions.find_recent_blocking(
            cart_fingerprint(flagged_cart), DUPLICATE_WINDOW
        )
        assert row is None


# ---------------------------------------------------------------------------
# Empty shopping list tests
# ---------------------------------------------------------------------------


class TestEmptyShoppingList:
    """Tests for handling empty shopping lists."""

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    @patch("grocery_butler.safeway_pipeline.CartBuilder")
    @patch("grocery_butler.safeway_pipeline.OrderService")
    def test_empty_list_still_calls_pipeline(
        self,
        mock_order_cls: MagicMock,
        mock_cart_cls: MagicMock,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        safeway_config: Config,
    ):
        """Test that empty list flows through to order service."""
        mock_client_cls.return_value.is_authenticated = True

        empty_cart = CartSummary(
            items=[],
            failed_items=[],
            substituted_items=[],
            skipped_items=[],
            restock_items=[],
            subtotal=0.0,
            fulfillment_options=[],
            recommended_fulfillment=FulfillmentType.PICKUP,
            estimated_total=0.0,
        )
        mock_cart_cls.return_value.build_cart.return_value = empty_cart
        mock_order_cls.return_value.submit_order.return_value = OrderResult(
            success=False,
            error_message="Cart is empty — nothing to order",
        )

        pipeline = SafewayPipeline(safeway_config, ":memory:")
        result = pipeline.run([])

        assert result.success is False
        assert "empty" in result.error_message.lower()


# ---------------------------------------------------------------------------
# Issue #61: duplicate-order guard
#
# These tests exercise the duplicate-submission ledger and the
# ``idempotency_key`` parameter now accepted by ``submit_cart``/``run``.
# They were written test-first, before that support existed, which is why
# OrderOutcome is still imported locally rather than at module scope.
# Each test uses a real tmp_path SQLite db so the ledger is real while
# every bootstrapped service (SafewayClient, OrderService, etc.) stays
# mocked, matching the pattern used throughout this file.
# ---------------------------------------------------------------------------


class TestSubmitCartDuplicateGuard:
    """Tests for duplicate-order prevention in SafewayPipeline.submit_cart."""

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    @patch("grocery_butler.safeway_pipeline.CartBuilder")
    @patch("grocery_butler.safeway_pipeline.OrderService")
    def test_unknown_outcome_blocks_resubmission_with_different_key(
        self,
        mock_order_cls: MagicMock,
        mock_cart_cls: MagicMock,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        safeway_config: Config,
        mock_cart_summary: CartSummary,
        tmp_path: Path,
    ) -> None:
        """Test an UNKNOWN-outcome submission blocks a same-cart resubmission."""
        from grocery_butler.order_service import OrderOutcome

        mock_client_cls.return_value.is_authenticated = True
        mock_order_cls.return_value.submit_order.side_effect = [
            OrderResult(
                success=False,
                outcome=OrderOutcome.UNKNOWN,
                error_message="Order outcome unknown — request timed out",
            ),
            OrderResult(success=True, outcome=OrderOutcome.SUCCESS),
        ]

        pipeline = SafewayPipeline(safeway_config, str(tmp_path / "orders.db"))

        first = pipeline.submit_cart(mock_cart_summary, idempotency_key="key-1")
        second = pipeline.submit_cart(mock_cart_summary, idempotency_key="key-2")

        assert first.outcome is OrderOutcome.UNKNOWN
        assert second.success is False
        assert second.outcome is OrderOutcome.DUPLICATE
        assert mock_order_cls.return_value.submit_order.call_count == 1

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    @patch("grocery_butler.safeway_pipeline.CartBuilder")
    @patch("grocery_butler.safeway_pipeline.OrderService")
    def test_success_then_immediate_resubmission_blocked(
        self,
        mock_order_cls: MagicMock,
        mock_cart_cls: MagicMock,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        safeway_config: Config,
        mock_cart_summary: CartSummary,
        tmp_path: Path,
    ) -> None:
        """Test a successful submission also blocks an immediate resubmission."""
        from grocery_butler.order_service import OrderOutcome

        mock_client_cls.return_value.is_authenticated = True
        mock_order_cls.return_value.submit_order.side_effect = [
            OrderResult(
                success=True,
                outcome=OrderOutcome.SUCCESS,
                confirmation=OrderConfirmation(
                    order_id="ORD-1",
                    status="confirmed",
                    estimated_time="2h",
                    total=4.99,
                    fulfillment_type=FulfillmentType.PICKUP,
                    item_count=1,
                ),
            ),
            OrderResult(success=True, outcome=OrderOutcome.SUCCESS),
        ]

        pipeline = SafewayPipeline(safeway_config, str(tmp_path / "orders.db"))

        first = pipeline.submit_cart(mock_cart_summary, idempotency_key="key-a")
        second = pipeline.submit_cart(mock_cart_summary, idempotency_key="key-b")

        assert first.success is True
        assert second.success is False
        assert second.outcome is OrderOutcome.DUPLICATE
        assert mock_order_cls.return_value.submit_order.call_count == 1

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    @patch("grocery_butler.safeway_pipeline.CartBuilder")
    @patch("grocery_butler.safeway_pipeline.OrderService")
    def test_failed_first_submission_does_not_block_retry(
        self,
        mock_order_cls: MagicMock,
        mock_cart_cls: MagicMock,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        safeway_config: Config,
        mock_cart_summary: CartSummary,
        tmp_path: Path,
    ) -> None:
        """Test a definitive FAILED first submission does not block a retry."""
        from grocery_butler.order_service import OrderOutcome

        mock_client_cls.return_value.is_authenticated = True
        mock_order_cls.return_value.submit_order.side_effect = [
            OrderResult(
                success=False,
                outcome=OrderOutcome.FAILED,
                error_message="Safeway rejected the order",
            ),
            OrderResult(success=True, outcome=OrderOutcome.SUCCESS),
        ]

        pipeline = SafewayPipeline(safeway_config, str(tmp_path / "orders.db"))

        first = pipeline.submit_cart(mock_cart_summary, idempotency_key="key-1")
        second = pipeline.submit_cart(mock_cart_summary, idempotency_key="key-2")

        assert first.outcome is OrderOutcome.FAILED
        assert second.success is True
        assert mock_order_cls.return_value.submit_order.call_count == 2

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    @patch("grocery_butler.safeway_pipeline.CartBuilder")
    @patch("grocery_butler.safeway_pipeline.OrderService")
    def test_duplicate_error_message_mentions_duplicate_or_recent(
        self,
        mock_order_cls: MagicMock,
        mock_cart_cls: MagicMock,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        safeway_config: Config,
        mock_cart_summary: CartSummary,
        tmp_path: Path,
    ) -> None:
        """Test the DUPLICATE error message references duplicate/recent activity."""
        from grocery_butler.order_service import OrderOutcome

        mock_client_cls.return_value.is_authenticated = True
        mock_order_cls.return_value.submit_order.side_effect = [
            OrderResult(success=False, outcome=OrderOutcome.UNKNOWN),
            OrderResult(success=True, outcome=OrderOutcome.SUCCESS),
        ]

        pipeline = SafewayPipeline(safeway_config, str(tmp_path / "orders.db"))

        pipeline.submit_cart(mock_cart_summary, idempotency_key="key-1")
        second = pipeline.submit_cart(mock_cart_summary, idempotency_key="key-2")

        message = second.error_message.lower()
        assert "duplicate" in message or "recent" in message

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    @patch("grocery_butler.safeway_pipeline.CartBuilder")
    @patch("grocery_butler.safeway_pipeline.OrderService")
    def test_ledger_write_failure_does_not_mask_successful_order(
        self,
        mock_order_cls: MagicMock,
        mock_cart_cls: MagicMock,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        safeway_config: Config,
        mock_cart_summary: CartSummary,
        tmp_path: Path,
    ) -> None:
        """Test a ledger write failure never masks a successful order.

        Gate 2.5 review BLOCKER (Issue #61): if
        ``OrderSubmissionStore.mark`` raises after Safeway has already
        confirmed and charged the order, that exception must never
        propagate and hide the real, successful result — the caller
        would otherwise see an unhandled 500 for an order that actually
        succeeded, and the confirmation/order_id would be lost.
        """
        from grocery_butler.order_service import OrderOutcome
        from grocery_butler.order_submissions import DUPLICATE_WINDOW, cart_fingerprint

        mock_client_cls.return_value.is_authenticated = True
        expected = OrderResult(
            success=True,
            outcome=OrderOutcome.SUCCESS,
            confirmation=OrderConfirmation(
                order_id="ORD-LEDGER-FAIL",
                status="confirmed",
                estimated_time="2h",
                total=4.99,
                fulfillment_type=FulfillmentType.PICKUP,
                item_count=1,
            ),
        )
        mock_order_cls.return_value.submit_order.return_value = expected

        pipeline = SafewayPipeline(safeway_config, str(tmp_path / "orders.db"))

        with patch.object(
            pipeline._order_submissions,
            "mark",
            side_effect=RuntimeError("ledger write failed"),
        ):
            result = pipeline.submit_cart(mock_cart_summary, idempotency_key="key-1")

        assert result is expected
        assert result.success is True
        assert result.confirmation is not None
        assert result.confirmation.order_id == "ORD-LEDGER-FAIL"

        row = pipeline._order_submissions.find_recent_blocking(
            cart_fingerprint(mock_cart_summary), DUPLICATE_WINDOW
        )
        assert row is not None
        assert row["status"] == "submitted"


class TestRunDuplicateGuard:
    """Tests for duplicate-order prevention in SafewayPipeline.run."""

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    @patch("grocery_butler.safeway_pipeline.CartBuilder")
    @patch("grocery_butler.safeway_pipeline.OrderService")
    def test_run_blocks_immediate_duplicate_cart(
        self,
        mock_order_cls: MagicMock,
        mock_cart_cls: MagicMock,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        safeway_config: Config,
        sample_items: list[ShoppingListItem],
        mock_cart_summary: CartSummary,
        tmp_path: Path,
    ) -> None:
        """Test running the identical cart twice in a row blocks the second submit."""
        from grocery_butler.order_service import OrderOutcome

        mock_client = mock_client_cls.return_value
        mock_client.is_authenticated = False

        mock_cart_builder = mock_cart_cls.return_value
        mock_cart_builder.build_cart.return_value = mock_cart_summary

        mock_order_cls.return_value.submit_order.side_effect = [
            OrderResult(success=False, outcome=OrderOutcome.UNKNOWN),
            OrderResult(success=True, outcome=OrderOutcome.SUCCESS),
        ]

        pipeline = SafewayPipeline(safeway_config, str(tmp_path / "orders.db"))

        first = pipeline.run(sample_items)
        second = pipeline.run(sample_items)

        assert first.outcome is OrderOutcome.UNKNOWN
        assert second.outcome is OrderOutcome.DUPLICATE
        assert mock_order_cls.return_value.submit_order.call_count == 1


# ---------------------------------------------------------------------------
# Issue #61 (security-review BLOCKER): the duplicate-order guard must be
# atomic under real concurrency, not just sequential re-calls.
#
# _submit_guarded previously did find_recent_blocking() (SELECT, one
# connection) then record_attempt() (INSERT, a second connection), so two
# concurrent submissions of an identical cart could both pass the SELECT
# and both reach OrderService.submit_order — the double-charge Issue #61
# exists to prevent. The fix switches _submit_guarded to the atomic
# OrderSubmissionStore.try_record_attempt (added in
# tests/test_order_submissions.py). This test does not patch the store —
# it uses the pipeline's real tmp_path-backed OrderSubmissionStore and
# races two real threads against it via a threading.Barrier placed
# directly on try_record_attempt. It was written test-first, before that
# method existed, so it originally failed for the right reason
# (AttributeError: try_record_attempt did not exist) until both the
# store method and the pipeline's switch to it were implemented.
# ---------------------------------------------------------------------------


class TestSubmitCartDuplicateGuardConcurrency:
    """Regression test: concurrent submit_cart calls must not double-submit."""

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    @patch("grocery_butler.safeway_pipeline.CartBuilder")
    @patch("grocery_butler.safeway_pipeline.OrderService")
    def test_concurrent_submit_cart_only_one_reaches_order_service(
        self,
        mock_order_cls: MagicMock,
        mock_cart_cls: MagicMock,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        safeway_config: Config,
        mock_cart_summary: CartSummary,
        tmp_path: Path,
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """Test two concurrent submit_cart calls for the same cart: one wins.

        A ``threading.Barrier`` lines both threads up immediately before
        the real ``OrderSubmissionStore.try_record_attempt`` call so they
        race for real against the tmp_path-backed SQLite ledger. Exactly
        one thread may proceed to ``OrderService.submit_order``; the
        other must get back a DUPLICATE outcome.
        """
        from grocery_butler.order_service import OrderOutcome

        mock_client_cls.return_value.is_authenticated = True
        mock_order_cls.return_value.submit_order.return_value = OrderResult(
            success=True, outcome=OrderOutcome.SUCCESS
        )

        pipeline = SafewayPipeline(safeway_config, str(tmp_path / "race.db"))

        barrier = threading.Barrier(2)
        order_submissions_store = pipeline._order_submissions
        real_try_record_attempt = order_submissions_store.try_record_attempt

        def _guarded_try_record_attempt(*args: object, **kwargs: object) -> object:
            barrier.wait()
            return real_try_record_attempt(*args, **kwargs)

        monkeypatch.setattr(
            order_submissions_store,
            "try_record_attempt",
            _guarded_try_record_attempt,
        )

        results: list[OrderResult | None] = [None, None]

        def _submit(index: int, key: str) -> None:
            results[index] = pipeline.submit_cart(
                mock_cart_summary, idempotency_key=key
            )

        thread_a = threading.Thread(target=_submit, args=(0, "race-key-1"))
        thread_b = threading.Thread(target=_submit, args=(1, "race-key-2"))
        thread_a.start()
        thread_b.start()
        thread_a.join()
        thread_b.join()

        assert mock_order_cls.return_value.submit_order.call_count == 1
        outcomes = [result.outcome for result in results if result is not None]
        assert outcomes.count(OrderOutcome.DUPLICATE) == 1


# ---------------------------------------------------------------------------
# Issue #60 — order submission descoped for v1.0 behind a fail-safe gate
# ---------------------------------------------------------------------------


class TestOrderSubmissionDisabledError:
    """Tests for the OrderSubmissionDisabledError exception type."""

    def test_is_a_safeway_pipeline_error(self) -> None:
        """Test OrderSubmissionDisabledError subclasses SafewayPipelineError."""
        from grocery_butler.safeway_pipeline import OrderSubmissionDisabledError

        assert issubclass(OrderSubmissionDisabledError, SafewayPipelineError)

    def test_message_matches_the_shared_disabled_message(self) -> None:
        """Test the exception message is the shared actionable message."""
        from grocery_butler.order_service import ORDER_SUBMISSION_DISABLED_MESSAGE
        from grocery_butler.safeway_pipeline import OrderSubmissionDisabledError

        err = OrderSubmissionDisabledError(ORDER_SUBMISSION_DISABLED_MESSAGE)
        assert str(err) == ORDER_SUBMISSION_DISABLED_MESSAGE


class TestOrderSubmissionEnabledProperty:
    """Tests for SafewayPipeline.order_submission_enabled."""

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    def test_reflects_enabled_config(
        self,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        safeway_config: Config,
    ):
        """Test the property is True when the config gate is enabled."""
        pipeline = SafewayPipeline(safeway_config, ":memory:")
        assert pipeline.order_submission_enabled is True

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    def test_reflects_disabled_config(
        self,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        disabled_safeway_config: Config,
    ):
        """Test the property is False when the config gate is disabled."""
        pipeline = SafewayPipeline(disabled_safeway_config, ":memory:")
        assert pipeline.order_submission_enabled is False


class TestSubmissionDisabledBlocksRun:
    """Tests that a disabled config short-circuits run() before any I/O."""

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    @patch("grocery_butler.safeway_pipeline.CartBuilder")
    @patch("grocery_butler.safeway_pipeline.OrderService")
    def test_run_raises_without_authenticating_or_building_cart(
        self,
        mock_order_cls: MagicMock,
        mock_cart_cls: MagicMock,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        disabled_safeway_config: Config,
        sample_items: list[ShoppingListItem],
    ):
        """Test run() raises OrderSubmissionDisabledError before any I/O."""
        from grocery_butler.safeway_pipeline import OrderSubmissionDisabledError

        mock_client = mock_client_cls.return_value
        mock_client.is_authenticated = False

        pipeline = SafewayPipeline(disabled_safeway_config, ":memory:")

        with pytest.raises(OrderSubmissionDisabledError):
            pipeline.run(sample_items)

        mock_client.authenticate.assert_not_called()
        mock_cart_cls.return_value.build_cart.assert_not_called()
        mock_order_cls.return_value.submit_order.assert_not_called()


class TestSubmissionDisabledBlocksSubmitCart:
    """Tests that a disabled config short-circuits submit_cart() before auth."""

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    @patch("grocery_butler.safeway_pipeline.CartBuilder")
    @patch("grocery_butler.safeway_pipeline.OrderService")
    def test_submit_cart_raises_without_authenticating(
        self,
        mock_order_cls: MagicMock,
        mock_cart_cls: MagicMock,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        disabled_safeway_config: Config,
        mock_cart_summary: CartSummary,
    ):
        """Test submit_cart raises OrderSubmissionDisabledError before auth."""
        from grocery_butler.safeway_pipeline import OrderSubmissionDisabledError

        mock_client = mock_client_cls.return_value
        mock_client.is_authenticated = False

        pipeline = SafewayPipeline(disabled_safeway_config, ":memory:")

        with pytest.raises(OrderSubmissionDisabledError):
            pipeline.submit_cart(mock_cart_summary)

        mock_client.authenticate.assert_not_called()
        mock_order_cls.return_value.submit_order.assert_not_called()


class TestSubmissionDisabledDoesNotAffectBuildCartOnly:
    """Tests that build_cart_only is unaffected by the Issue #60 gate."""

    @patch("grocery_butler.safeway_pipeline.RecipeStore")
    @patch("grocery_butler.safeway_pipeline.ProductSearchService")
    @patch("grocery_butler.safeway_pipeline.ProductSelector")
    @patch("grocery_butler.safeway_pipeline.SubstitutionService")
    @patch("grocery_butler.safeway_pipeline.SafewayClient")
    @patch("grocery_butler.safeway_pipeline.PantryManager")
    @patch("grocery_butler.safeway_pipeline.CartBuilder")
    def test_build_cart_only_works_when_disabled(
        self,
        mock_cart_cls: MagicMock,
        mock_pantry: MagicMock,
        mock_client_cls: MagicMock,
        mock_sub: MagicMock,
        mock_selector: MagicMock,
        mock_search: MagicMock,
        mock_store: MagicMock,
        disabled_safeway_config: Config,
        sample_items: list[ShoppingListItem],
        mock_cart_summary: CartSummary,
    ):
        """Test cart building/review keeps working when submission is disabled."""
        mock_client = mock_client_cls.return_value
        mock_client.is_authenticated = True
        mock_cart_cls.return_value.build_cart.return_value = mock_cart_summary

        pipeline = SafewayPipeline(disabled_safeway_config, ":memory:")
        cart = pipeline.build_cart_only(sample_items)

        assert cart is mock_cart_summary
