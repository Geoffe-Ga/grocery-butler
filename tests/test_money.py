"""Tests for money-as-Decimal handling across grocery_butler (issue #81).

Today every monetary field (``SafewayProduct.price``, ``CartItem.
estimated_cost``, ``FulfillmentOption.fee``, ``CartSummary.subtotal``/
``estimated_total``, ``OrderConfirmation.total``) is a plain ``float``,
and arithmetic on those floats uses binary rounding (Python's
banker's-rounding ``round()``) instead of decimal cents math. This file
is Gate 1 RED for the fix: a new ``Money`` type
(``Annotated[Decimal, BeforeValidator(_coerce_money), PlainSerializer(
float, ...)]``) applied to every monetary field, with arithmetic
quantized to cents (``ROUND_HALF_UP``) at each producing site.

These tests are written against that not-yet-implemented design, so
most of them are expected to fail today. Names that don't exist yet
(``grocery_butler.order_service._safe_money``) are imported locally
inside the test body that needs them, matching this repo's existing
convention (see ``tests/test_order_service.py``'s ``compute_cart_total``
tests) for not breaking collection of the rest of the file with an
``ImportError``. Tests that already pass today are explicitly labeled
"pin" in their docstring -- they exist to catch a regression during the
Money migration (e.g. Pydantic's default JSON serialization of a
``Decimal`` is a *string*, which would silently break the wire
contract without the custom ``PlainSerializer``).
"""

from __future__ import annotations

import json
from dataclasses import dataclass
from decimal import Decimal
from typing import Any
from unittest.mock import MagicMock

import httpx
import pytest
from pydantic import ValidationError

from grocery_butler.cart_builder import (
    CartBuilder,
    _calculate_subtotal,
    _substitution_to_cart_item,
)
from grocery_butler.models import (
    CartItem,
    CartSummary,
    FulfillmentOption,
    FulfillmentType,
    IngredientCategory,
    SafewayProduct,
    ShoppingListItem,
    SubstitutionOption,
    SubstitutionResult,
    SubstitutionSuitability,
)
from grocery_butler.order_service import _build_order_payload, _parse_order_response
from grocery_butler.product_search import ProductSearchService
from grocery_butler.safeway_client import SafewayClient

# ------------------------------------------------------------------
# Fixtures / builders
# ------------------------------------------------------------------


def _make_shopping_item(
    ingredient: str = "milk",
    quantity: float = 1.0,
    unit: str = "lb",
    search_term: str = "whole milk",
    estimated_price: float | None = None,
) -> ShoppingListItem:
    """Create a test ShoppingListItem.

    Args:
        ingredient: Ingredient name.
        quantity: Desired quantity.
        unit: Unit of measurement.
        search_term: Search term.
        estimated_price: Optional estimated price.

    Returns:
        ShoppingListItem for testing.
    """
    return ShoppingListItem(
        ingredient=ingredient,
        quantity=quantity,
        unit=unit,
        category=IngredientCategory.DAIRY,
        search_term=search_term,
        from_meals=["Test Meal"],
        estimated_price=estimated_price,
    )


def _make_product(
    product_id: str = "P001",
    name: str = "Test Product",
    price: float | str = 5.99,
    unit_price: float | None = None,
    size: str = "1 lb",
    in_stock: bool = True,
) -> SafewayProduct:
    """Create a test SafewayProduct.

    Args:
        product_id: Product ID.
        name: Product name.
        price: Product price (float or string, to exercise coercion).
        unit_price: Optional unit price.
        size: Product size.
        in_stock: Whether in stock.

    Returns:
        SafewayProduct for testing.
    """
    return SafewayProduct(
        product_id=product_id,
        name=name,
        price=price,
        unit_price=unit_price,
        size=size,
        in_stock=in_stock,
    )


def _make_cart_item(price: float = 5.99, quantity: int = 1) -> CartItem:
    """Create a test CartItem priced at *price* for *quantity* units.

    Args:
        price: Product/item price.
        quantity: Order quantity.

    Returns:
        CartItem for testing.
    """
    return CartItem(
        shopping_list_item=_make_shopping_item(),
        safeway_product=_make_product(price=price),
        quantity_to_order=quantity,
        estimated_cost=price,
    )


def _make_fulfillment_option(
    fee: float = 0.0,
    ftype: FulfillmentType = FulfillmentType.PICKUP,
    available: bool = True,
) -> FulfillmentOption:
    """Create a test FulfillmentOption.

    Args:
        fee: Fulfillment fee.
        ftype: Fulfillment type.
        available: Whether the option is available.

    Returns:
        FulfillmentOption for testing.
    """
    return FulfillmentOption(
        type=ftype,
        available=available,
        fee=fee,
        windows=[],
    )


def _make_cart_summary(
    subtotal: float = 10.0,
    estimated_total: float = 10.0,
    items: list[CartItem] | None = None,
    fulfillment_options: list[FulfillmentOption] | None = None,
) -> CartSummary:
    """Create a test CartSummary.

    Args:
        subtotal: Cart subtotal.
        estimated_total: Cart estimated total.
        items: Regular cart items (defaults to a single $5.99 item).
        fulfillment_options: Fulfillment options (defaults to a single
            free pickup option).

    Returns:
        CartSummary for testing.
    """
    return CartSummary(
        items=items if items is not None else [_make_cart_item()],
        failed_items=[],
        substituted_items=[],
        skipped_items=[],
        restock_items=[],
        subtotal=subtotal,
        fulfillment_options=(
            fulfillment_options
            if fulfillment_options is not None
            else [_make_fulfillment_option(fee=0.0)]
        ),
        recommended_fulfillment=FulfillmentType.PICKUP,
        estimated_total=estimated_total,
    )


@dataclass
class _MockSelectionResult:
    """Mock selection result matching ProductSelector output."""

    item: ShoppingListItem
    product: SafewayProduct | None
    reasoning: str


def _make_builder(
    search_results: list[SafewayProduct] | None = None,
    selection_product: SafewayProduct | None = None,
    fulfillment_response: dict[str, Any] | None = None,
) -> CartBuilder:
    """Create a CartBuilder with mock dependencies.

    Args:
        search_results: Products returned by search.
        selection_product: Product returned by the selector.
        fulfillment_response: Fulfillment API response.

    Returns:
        CartBuilder with mocked services.
    """
    mock_search = MagicMock()
    mock_search.get_cached_product.return_value = None
    mock_search.search_products.return_value = search_results or []

    mock_selector = MagicMock()
    mock_selector.select_product.return_value = _MockSelectionResult(
        item=_make_shopping_item(),
        product=selection_product,
        reasoning="Test selection",
    )

    mock_substitution = MagicMock()

    mock_client = MagicMock()
    mock_client.store_id = "1234"
    mock_client.get.return_value = fulfillment_response or {}

    return CartBuilder(
        search_service=mock_search,
        product_selector=mock_selector,
        substitution_service=mock_substitution,
        safeway_client=mock_client,
    )


@pytest.fixture
def db_path(tmp_path: Any) -> str:
    """Provide a temporary database path.

    Args:
        tmp_path: Pytest tmp_path fixture.

    Returns:
        Path string for the test database.
    """
    return str(tmp_path / "money_test.db")


# ------------------------------------------------------------------
# 1. Float-drift regression (headline)
# ------------------------------------------------------------------


class TestFloatDriftRegression:
    """Regression guards for float binary-rounding drift (issue #81)."""

    def test_half_up_rounding_via_cart_builder_pricing_path(self) -> None:
        """Test a $1.005 item rounds to $1.01 (half-up), not $1.00.

        ``round(1.005, 2)`` in Python returns ``1.0`` -- the float
        1.005 is actually stored as 1.00499999999999989..., so binary
        rounding rounds it *down*. Decimal cents math on the string
        representation ("1.005") rounds up correctly per ROUND_HALF_UP.
        This drives the real ``CartBuilder.build_cart`` pricing path
        (``_build_cart_result``'s ``cost = (product.price *
        decision.quantity).quantize(CENTS, rounding=ROUND_HALF_UP)``),
        not a synthetic helper call.
        """
        item = _make_shopping_item(quantity=1.0, unit="lb")
        product = _make_product(price=1.005, size="1 lb")
        builder = _make_builder(search_results=[product], selection_product=product)

        cart = builder.build_cart([item])

        assert len(cart.items) == 1
        cost = cart.items[0].estimated_cost
        assert cost == Decimal("1.01")
        assert isinstance(cost, Decimal)

    def test_half_up_rounding_via_substitution_pricing_path(self) -> None:
        """Test a $1.005 substitute also rounds to $1.01 (half-up).

        ``_substitution_to_cart_item`` is the fourth money-producing
        site in ``cart_builder`` (after ``_build_cart_result``,
        ``_calculate_subtotal``, and the ``estimated_total`` quantize in
        ``build_cart``). With Decimal prices, a leftover
        ``round(Decimal, 2)`` would silently use the decimal context's
        default ROUND_HALF_EVEN (banker's rounding: $1.005 -> $1.00),
        so this pins the substitution path to the same
        ROUND_HALF_UP cents math as the primary pricing path.
        """
        substitute = _make_product(product_id="ALT1", price=1.005, size="1 lb")
        option = SubstitutionOption(
            product=substitute,
            suitability=SubstitutionSuitability.GOOD,
            reasoning="Similar product",
        )
        result = SubstitutionResult(
            status="alternatives_found",
            original_item=_make_shopping_item(quantity=1.0, unit="lb"),
            alternatives=[option],
            selected=option,
            message="Found 1 alternative(s)",
        )

        cart_item = _substitution_to_cart_item(result, cap=10)

        assert cart_item is not None
        assert cart_item.estimated_cost == Decimal("1.01")
        assert isinstance(cart_item.estimated_cost, Decimal)

    def test_penny_drift_sum_subtotal_exact_decimal(self) -> None:
        """Test ten $0.10 items plus one $0.20 item sum to exactly $1.20.

        Drives ``cart_builder._calculate_subtotal`` -- the same pure
        helper ``CartBuilder.build_cart`` calls to compute
        ``CartSummary.subtotal`` -- directly. Float summation of
        eleven cent-level values is a classic source of penny drift;
        the fix must aggregate in cents-quantized Decimal so the sum
        is exact, not merely "close enough" once rounded for display.
        """
        items = [_make_cart_item(price=0.10) for _ in range(10)]
        items.append(_make_cart_item(price=0.20))

        subtotal = _calculate_subtotal(items, [])

        assert isinstance(subtotal, Decimal)
        assert subtotal == Decimal("1.20")


# ------------------------------------------------------------------
# 2. Type assertions: money fields are Decimal after validation
# ------------------------------------------------------------------


class TestMoneyFieldTypesAreDecimal:
    """Money-bearing fields must be ``Decimal`` after model validation."""

    def test_safeway_product_price_is_decimal(self) -> None:
        """Test SafewayProduct.price is a Decimal instance."""
        product = _make_product(price=5.99)
        assert isinstance(product.price, Decimal)

    def test_cart_item_estimated_cost_is_decimal(self) -> None:
        """Test CartItem.estimated_cost is a Decimal instance."""
        item = _make_cart_item(price=8.99)
        assert isinstance(item.estimated_cost, Decimal)

    def test_fulfillment_option_fee_is_decimal(self) -> None:
        """Test FulfillmentOption.fee is a Decimal instance."""
        option = _make_fulfillment_option(fee=9.95)
        assert isinstance(option.fee, Decimal)

    def test_cart_summary_subtotal_and_estimated_total_are_decimal(self) -> None:
        """Test CartSummary.subtotal and estimated_total are Decimal instances."""
        cart = _make_cart_summary(subtotal=10.0, estimated_total=10.0)
        assert isinstance(cart.subtotal, Decimal)
        assert isinstance(cart.estimated_total, Decimal)


# ------------------------------------------------------------------
# 3. Wire-contract stability: JSON dumps stay numbers, not strings
# ------------------------------------------------------------------


class TestWireContractStabilityJsonNumbers:
    """Money fields must dump as JSON numbers (floats), never strings.

    These currently PASS -- the fields are still plain ``float`` --
    which is exactly why they're pinned here. Pydantic's default JSON
    serialization of a ``Decimal`` is a *string* (to avoid float
    precision loss), so once the fields become ``Decimal``, the
    ``Money`` type's ``PlainSerializer(float, ..., when_used="json")``
    is required to keep the wire contract unchanged. Without it, these
    tests would start failing the moment the field type flips.
    """

    def test_cart_dump_money_fields_are_json_numbers(self) -> None:
        """Test cart.model_dump(mode="json") money fields are floats (pin)."""
        cart = _make_cart_summary(
            subtotal=8.99,
            estimated_total=8.99,
            fulfillment_options=[_make_fulfillment_option(fee=0.0)],
        )

        dumped = cart.model_dump(mode="json")

        assert isinstance(dumped["subtotal"], float)
        assert isinstance(dumped["estimated_total"], float)
        assert isinstance(dumped["items"][0]["estimated_cost"], float)
        assert isinstance(dumped["items"][0]["safeway_product"]["price"], float)
        assert isinstance(dumped["fulfillment_options"][0]["fee"], float)
        json.dumps(dumped)  # must not raise

    def test_build_order_payload_estimated_total_is_json_number(self) -> None:
        """Test _build_order_payload's estimatedTotal is a float (pin)."""
        cart = _make_cart_summary(subtotal=8.99, estimated_total=8.99)

        payload = _build_order_payload(cart)

        assert isinstance(payload["estimatedTotal"], float)
        json.dumps(payload)  # must not raise


# ------------------------------------------------------------------
# 4. Issue #73 semantics preserved: non-finite rejection, bool rejection
# ------------------------------------------------------------------


class TestIssue73SemanticsPreservedForMoney:
    """Non-finite money is still rejected; bool must also be rejected.

    Issue #73 (``allow_inf_nan=False``) already guards ``CartItem.
    estimated_cost``, ``FulfillmentOption.fee``, and ``CartSummary.
    subtotal``/``estimated_total`` -- those parametrized cases below
    are pins. ``SafewayProduct.price``/``unit_price`` have no such
    guard today, so the non-finite cases for those fields are genuine
    RED. Bool rejection is also genuine RED: nothing today stops
    ``price=True`` from validating as a truthy "1.0".
    """

    @pytest.mark.parametrize("bad_value", [float("inf"), float("-inf"), float("nan")])
    def test_cart_item_estimated_cost_rejects_non_finite_pin(
        self, bad_value: float
    ) -> None:
        """Test CartItem.estimated_cost still rejects inf/-inf/nan (pin)."""
        with pytest.raises(ValidationError):
            CartItem(
                shopping_list_item=_make_shopping_item(),
                safeway_product=_make_product(),
                quantity_to_order=1,
                estimated_cost=bad_value,
            )

    @pytest.mark.parametrize("bad_value", [float("inf"), float("-inf"), float("nan")])
    def test_fulfillment_option_fee_rejects_non_finite_pin(
        self, bad_value: float
    ) -> None:
        """Test FulfillmentOption.fee still rejects inf/-inf/nan (pin)."""
        with pytest.raises(ValidationError):
            _make_fulfillment_option(fee=bad_value)

    @pytest.mark.parametrize("field", ["subtotal", "estimated_total"])
    def test_cart_summary_totals_reject_non_finite_pin(self, field: str) -> None:
        """Test CartSummary subtotal/estimated_total still reject non-finite (pin)."""
        kwargs: dict[str, float] = {"subtotal": 0.0, "estimated_total": 0.0}
        kwargs[field] = float("inf")
        with pytest.raises(ValidationError):
            _make_cart_summary(**kwargs)

    @pytest.mark.parametrize("bad_value", [float("inf"), float("-inf"), float("nan")])
    def test_safeway_product_price_rejects_non_finite_new_guard(
        self, bad_value: float
    ) -> None:
        """Test SafewayProduct.price rejects inf/-inf/nan (RED: no guard today)."""
        with pytest.raises(ValidationError):
            SafewayProduct(
                product_id="P1", name="Bad Price", price=bad_value, size="1 lb"
            )

    @pytest.mark.parametrize("bad_value", [float("inf"), float("-inf"), float("nan")])
    def test_safeway_product_unit_price_rejects_non_finite_new_guard(
        self, bad_value: float
    ) -> None:
        """Test SafewayProduct.unit_price rejects inf/-inf/nan (RED: no guard today)."""
        with pytest.raises(ValidationError):
            SafewayProduct(
                product_id="P1",
                name="Bad Unit Price",
                price=1.0,
                unit_price=bad_value,
                size="1 lb",
            )

    def test_model_validate_json_infinity_literal_rejected_pin(self) -> None:
        """Test a raw JSON "Infinity" literal for estimated_cost is rejected (pin).

        Python's json parser accepts the non-standard ``Infinity``
        literal even though it isn't valid JSON, so a hand-built
        payload is used here rather than ``json.dumps`` (which would
        never emit an un-quoted bare literal from a normal float in a
        way that reproduces this).
        """
        item = _make_shopping_item()
        product = _make_product()
        json_str = (
            '{"shopping_list_item": '
            + item.model_dump_json()
            + ', "safeway_product": '
            + product.model_dump_json()
            + ', "quantity_to_order": 1, "estimated_cost": Infinity}'
        )
        with pytest.raises(ValidationError):
            CartItem.model_validate_json(json_str)

    def test_safeway_product_price_rejects_bool(self) -> None:
        """Test SafewayProduct.price rejects a bare bool (RED: not enforced today).

        ``bool`` is a subclass of ``int`` and would otherwise silently
        coerce to a nonsensical ``1.0``/``0.0`` price -- the ``Money``
        type's ``_coerce_money`` must explicitly reject it.
        """
        with pytest.raises(ValidationError):
            SafewayProduct(product_id="P1", name="Bool Price", price=True, size="1 lb")


# ------------------------------------------------------------------
# 5. Money | None: optional monetary fields
# ------------------------------------------------------------------


class TestMoneyOptionalFields:
    """``Money | None`` fields accept and round-trip ``None``."""

    def test_safeway_product_unit_price_none_valid_and_dumps_none(self) -> None:
        """Test SafewayProduct.unit_price=None validates and dumps as null."""
        product = _make_product(unit_price=None)
        assert product.unit_price is None

        dumped = product.model_dump(mode="json")
        assert dumped["unit_price"] is None

    def test_shopping_list_item_estimated_price_none_valid_and_dumps_none(
        self,
    ) -> None:
        """Test ShoppingListItem.estimated_price=None validates and dumps as null."""
        item = _make_shopping_item(estimated_price=None)
        assert item.estimated_price is None

        dumped = item.model_dump(mode="json")
        assert dumped["estimated_price"] is None


# ------------------------------------------------------------------
# 6. Coercion fidelity: float/str inputs coerce to exact Decimal
# ------------------------------------------------------------------


class TestCoercionFidelity:
    """Money coercion must preserve decimal exactness, not binary drift."""

    def test_safeway_product_price_float_coerces_exact_decimal(self) -> None:
        """Test price=4.15 (float) coerces to exactly Decimal("4.15").

        4.15 as a binary float is not exactly 4.15 in decimal, so this
        also guards that coercion goes through ``Decimal(str(v))``
        (exact) rather than ``Decimal(v)`` (binary-exact float
        conversion, which would produce a long non-round value).
        """
        product = SafewayProduct(
            product_id="P1", name="Coerce Float", price=4.15, size="1 lb"
        )
        assert product.price == Decimal("4.15")
        assert isinstance(product.price, Decimal)

    def test_safeway_product_price_string_coerces_to_decimal(self) -> None:
        """Test price="4.15" (string) coerces to Decimal("4.15")."""
        product = SafewayProduct(
            product_id="P1", name="Coerce String", price="4.15", size="1 lb"
        )
        assert product.price == Decimal("4.15")
        assert isinstance(product.price, Decimal)

    def test_safeway_product_price_rejects_unparseable_string(self) -> None:
        """Test an unparseable price string raises ValidationError.

        Exercises ``_coerce_money``'s ``InvalidOperation`` branch: the
        input is a coercible *type* (str) whose value cannot form a
        Decimal.
        """
        with pytest.raises(ValidationError):
            SafewayProduct(
                product_id="P1", name="Bad String", price="not-money", size="1 lb"
            )

    def test_safeway_product_price_rejects_non_numeric_type(self) -> None:
        """Test a non-coercible type (list) is rejected downstream.

        Exercises ``_coerce_money``'s pass-through branch: values that
        aren't Decimal/int/float/str are returned unchanged so
        Pydantic's own type validation rejects them with its usual
        error.
        """
        with pytest.raises(ValidationError):
            SafewayProduct(product_id="P1", name="Bad Type", price=[4, 15], size="1 lb")


# ------------------------------------------------------------------
# 7. sqlite round-trip through product_search's cache
# ------------------------------------------------------------------


class TestSqliteRoundTripPreservesDecimalPrecision:
    """A cached product's price must round-trip through sqlite as Decimal."""

    def _make_service(self, db_path: str) -> ProductSearchService:
        """Create a ProductSearchService over a real sqlite db, mock HTTP.

        Args:
            db_path: Path to the test database.

        Returns:
            ProductSearchService instance. No live HTTP calls are made
            by these tests, so the transport is never asked to respond.
        """
        transport = httpx.MockTransport(
            lambda request: httpx.Response(500, json={"error": "unused"})
        )
        http = httpx.Client(transport=transport)
        client = SafewayClient("user", "pass", "1234", http_client=http)
        return ProductSearchService(client, db_path)

    def test_product_price_round_trip_preserves_decimal_exactness(
        self, db_path: str
    ) -> None:
        """Test a product priced 4.15 round-trips through the cache as Decimal.

        Regression guard for issue #81: today ``SafewayProduct.price``
        is a plain float, so ``cached.product.price`` comes back as a
        float, not a ``Decimal`` -- this fails on the isinstance check
        even before considering exactness.
        """
        service = self._make_service(db_path)
        product = _make_product(price=4.15)

        service.save_mapping("term", product)
        cached = service.get_cached_mapping("term")

        assert cached is not None
        assert cached.product.price == Decimal("4.15")
        assert isinstance(cached.product.price, Decimal)


# ------------------------------------------------------------------
# 8. order_service._safe_money
# ------------------------------------------------------------------


class TestSafeMoneyOrderService:
    """Tests for the not-yet-existing ``order_service._safe_money``.

    ``_safe_money`` doesn't exist yet, so it is imported locally inside
    each test body (matching this file's existing convention for names
    introduced test-first, e.g. ``compute_cart_total`` in
    ``tests/test_order_service.py``) rather than at module scope, where
    the ``ImportError`` would break collection of every other test in
    this file.
    """

    def test_safe_money_valid_string_becomes_decimal(self) -> None:
        """Test a valid numeric string converts to an exact Decimal."""
        from grocery_butler.order_service import _safe_money

        result = _safe_money("12.34", Decimal("0"))
        assert result == Decimal("12.34")
        assert isinstance(result, Decimal)

    def test_safe_money_none_returns_fallback(self) -> None:
        """Test None returns the given fallback."""
        from grocery_butler.order_service import _safe_money

        assert _safe_money(None, Decimal("9.99")) == Decimal("9.99")

    def test_safe_money_garbage_string_returns_fallback(self) -> None:
        """Test a non-numeric string returns the fallback."""
        from grocery_butler.order_service import _safe_money

        assert _safe_money("not-a-number", Decimal("1.00")) == Decimal("1.00")

    def test_safe_money_nan_string_returns_fallback(self) -> None:
        """Test the string "NaN" (a valid but non-finite Decimal) returns fallback.

        ``Decimal("NaN")`` does not raise ``InvalidOperation`` -- it's a
        legitimate non-finite Decimal value -- so ``_safe_money`` must
        explicitly check finiteness, not merely catch construction
        errors.
        """
        from grocery_butler.order_service import _safe_money

        assert _safe_money("NaN", Decimal("2.50")) == Decimal("2.50")

    def test_safe_money_quantizes_to_cents(self) -> None:
        """Test a three-decimal-place input quantizes to cents, half-up."""
        from grocery_butler.order_service import _safe_money

        assert _safe_money("12.345", Decimal("0")) == Decimal("12.35")


# ------------------------------------------------------------------
# 9. OrderConfirmation.total from _parse_order_response
# ------------------------------------------------------------------


class TestOrderConfirmationTotalIsDecimal:
    """OrderConfirmation.total must be Decimal after response parsing."""

    def test_parse_order_response_total_is_decimal(self) -> None:
        """Test a response total of 25.97 parses into Decimal("25.97")."""
        cart = _make_cart_summary(subtotal=25.97, estimated_total=25.97)
        response = {"orderId": "ORD-1", "status": "confirmed", "total": 25.97}

        confirmation = _parse_order_response(response, cart)

        assert confirmation is not None
        assert confirmation.total == Decimal("25.97")
        assert isinstance(confirmation.total, Decimal)
