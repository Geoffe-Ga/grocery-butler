"""Tests for grocery_butler.safeway_pipeline module."""

from __future__ import annotations

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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def safeway_config() -> Config:
    """Return a Config with Safeway credentials set."""
    return Config(
        anthropic_api_key="sk-test",
        safeway_username="user@example.com",
        safeway_password="secret",
        safeway_store_id="1234",
        database_path=":memory:",
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
        """Test submit_cart delegates to order service without rebuilding."""
        mock_client = mock_client_cls.return_value
        mock_client.is_authenticated = True

        expected = OrderResult(success=True)
        mock_order_cls.return_value.submit_order.return_value = expected

        pipeline = SafewayPipeline(safeway_config, ":memory:")
        result = pipeline.submit_cart(mock_cart_summary)

        assert result is expected
        mock_order_cls.return_value.submit_order.assert_called_once_with(
            mock_cart_summary
        )
        mock_cart_cls.return_value.build_cart.assert_not_called()


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
