"""Tests for the new grocery_butler.order_submissions module (Issue #61).

Covers ``cart_fingerprint`` determinism and the ``OrderSubmissionStore``
duplicate-order ledger that backs the pipeline's duplicate guard. This
module does not exist yet, so the whole file is expected to fail to
collect (ModuleNotFoundError/ImportError) until it is implemented —
every test in this file is new, so there is no pre-existing coverage to
protect from that collection failure.
"""

from __future__ import annotations

from datetime import timedelta
from typing import TYPE_CHECKING

import pytest

from grocery_butler.models import (
    CartItem,
    CartSummary,
    FulfillmentOption,
    FulfillmentType,
    IngredientCategory,
    SafewayProduct,
    ShoppingListItem,
)
from grocery_butler.order_submissions import OrderSubmissionStore, cart_fingerprint

if TYPE_CHECKING:
    from pathlib import Path

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_item(
    ingredient: str = "chicken thighs",
    search_term: str = "boneless chicken thighs",
) -> ShoppingListItem:
    """Create a test ShoppingListItem.

    Args:
        ingredient: Ingredient name.
        search_term: Search term.

    Returns:
        ShoppingListItem for testing.
    """
    return ShoppingListItem(
        ingredient=ingredient,
        quantity=2.0,
        unit="lb",
        category=IngredientCategory.MEAT,
        search_term=search_term,
        from_meals=["Test Meal"],
    )


def _make_product(
    product_id: str = "P001",
    name: str = "Boneless Chicken Thighs",
    price: float = 8.99,
) -> SafewayProduct:
    """Create a test SafewayProduct.

    Args:
        product_id: Product ID.
        name: Product name.
        price: Product price.

    Returns:
        SafewayProduct for testing.
    """
    return SafewayProduct(
        product_id=product_id,
        name=name,
        price=price,
        size="2 lb",
        in_stock=True,
    )


def _make_cart_item(
    ingredient: str = "chicken thighs",
    product_id: str = "P001",
    price: float = 8.99,
    quantity_to_order: int = 1,
) -> CartItem:
    """Create a test CartItem.

    Args:
        ingredient: Ingredient name.
        product_id: Product ID.
        price: Product price.
        quantity_to_order: Quantity ordered.

    Returns:
        CartItem for testing.
    """
    return CartItem(
        shopping_list_item=_make_item(ingredient=ingredient),
        safeway_product=_make_product(product_id=product_id, price=price),
        quantity_to_order=quantity_to_order,
        estimated_cost=price * quantity_to_order,
    )


def _make_cart(
    items: list[CartItem] | None = None,
    restock_items: list[CartItem] | None = None,
    fulfillment: FulfillmentType = FulfillmentType.PICKUP,
    estimated_total: float | None = None,
) -> CartSummary:
    """Create a test CartSummary.

    Args:
        items: Regular cart items.
        restock_items: Restock queue items.
        fulfillment: Recommended fulfillment type.
        estimated_total: Explicit estimated total (defaults to item sum).

    Returns:
        CartSummary for testing.
    """
    cart_items = [_make_cart_item()] if items is None else items
    restock = [] if restock_items is None else restock_items
    subtotal = sum(i.estimated_cost for i in cart_items + restock)
    total = subtotal if estimated_total is None else estimated_total
    return CartSummary(
        items=cart_items,
        failed_items=[],
        substituted_items=[],
        skipped_items=[],
        restock_items=restock,
        subtotal=subtotal,
        fulfillment_options=[
            FulfillmentOption(
                type=FulfillmentType.PICKUP,
                available=True,
                fee=0.0,
                windows=[],
            ),
        ],
        recommended_fulfillment=fulfillment,
        estimated_total=total,
    )


# ---------------------------------------------------------------------------
# cart_fingerprint tests
# ---------------------------------------------------------------------------


class TestCartFingerprint:
    """Tests for cart_fingerprint determinism and sensitivity to content."""

    def test_returns_deterministic_sha256_hex(self) -> None:
        """Test the fingerprint is a 64-char lowercase-hex SHA-256 digest."""
        fingerprint = cart_fingerprint(_make_cart())

        assert isinstance(fingerprint, str)
        assert len(fingerprint) == 64
        assert all(c in "0123456789abcdef" for c in fingerprint)

    def test_same_cart_contents_same_fingerprint(self) -> None:
        """Test two carts with identical contents fingerprint identically."""
        cart1 = _make_cart()
        cart2 = _make_cart()

        assert cart_fingerprint(cart1) == cart_fingerprint(cart2)

    def test_item_order_does_not_affect_fingerprint(self) -> None:
        """Test item ordering within the cart does not change the fingerprint."""
        item_a = _make_cart_item(ingredient="milk", product_id="A")
        item_b = _make_cart_item(ingredient="eggs", product_id="B")
        cart1 = _make_cart(items=[item_a, item_b])
        cart2 = _make_cart(items=[item_b, item_a])

        assert cart_fingerprint(cart1) == cart_fingerprint(cart2)

    def test_price_and_total_changes_do_not_affect_fingerprint(self) -> None:
        """Test estimated_total/price differences don't change the fingerprint."""
        cart1 = _make_cart(estimated_total=8.99)
        item = _make_cart_item(price=99.99)
        cart2 = _make_cart(items=[item], estimated_total=250.00)

        assert cart_fingerprint(cart1) == cart_fingerprint(cart2)

    def test_different_fulfillment_different_fingerprint(self) -> None:
        """Test a different fulfillment type changes the fingerprint."""
        cart1 = _make_cart(fulfillment=FulfillmentType.PICKUP)
        cart2 = _make_cart(fulfillment=FulfillmentType.DELIVERY)

        assert cart_fingerprint(cart1) != cart_fingerprint(cart2)

    def test_different_items_different_fingerprint(self) -> None:
        """Test a different set of items changes the fingerprint."""
        cart1 = _make_cart()
        cart2 = _make_cart(items=[_make_cart_item(product_id="ZZZ")])

        assert cart_fingerprint(cart1) != cart_fingerprint(cart2)

    def test_different_quantity_different_fingerprint(self) -> None:
        """Test a different ordered quantity changes the fingerprint."""
        cart1 = _make_cart(items=[_make_cart_item(quantity_to_order=1)])
        cart2 = _make_cart(items=[_make_cart_item(quantity_to_order=5)])

        assert cart_fingerprint(cart1) != cart_fingerprint(cart2)

    def test_split_line_items_for_same_product_coalesce(self) -> None:
        """Test a product split across two line items fingerprints like one.

        Issue #61: the cart submitted to ``/order/submit`` is client-supplied
        JSON, not rebuilt server-side, so a client that splits one product's
        quantity across two line items (accidentally or otherwise) must not
        be able to produce a different fingerprint for an economically
        identical cart and slip past the duplicate-order guard.
        """
        combined = _make_cart(items=[_make_cart_item(quantity_to_order=3)])
        split = _make_cart(
            items=[
                _make_cart_item(quantity_to_order=1),
                _make_cart_item(quantity_to_order=2),
            ]
        )

        assert cart_fingerprint(combined) == cart_fingerprint(split)

    def test_different_total_quantity_across_split_items_differs(self) -> None:
        """Test coalescing sums quantities rather than ignoring them."""
        cart1 = _make_cart(
            items=[
                _make_cart_item(quantity_to_order=1),
                _make_cart_item(quantity_to_order=2),
            ]
        )
        cart2 = _make_cart(
            items=[
                _make_cart_item(quantity_to_order=1),
                _make_cart_item(quantity_to_order=3),
            ]
        )

        assert cart_fingerprint(cart1) != cart_fingerprint(cart2)

    def test_restock_items_affect_fingerprint(self) -> None:
        """Test restock items are included in the fingerprint computation."""
        cart1 = _make_cart(restock_items=[])
        cart2 = _make_cart(
            restock_items=[_make_cart_item(ingredient="butter", product_id="R1")]
        )

        assert cart_fingerprint(cart1) != cart_fingerprint(cart2)


# ---------------------------------------------------------------------------
# OrderSubmissionStore tests
# ---------------------------------------------------------------------------


class TestOrderSubmissionStore:
    """Tests for OrderSubmissionStore's ledger and duplicate-window blocking."""

    def test_record_attempt_returns_row_id(self, tmp_path: Path) -> None:
        """Test record_attempt returns an integer row id."""
        store = OrderSubmissionStore(str(tmp_path / "test.db"))

        submission_id = store.record_attempt("key-1", "fingerprint-abc")

        assert isinstance(submission_id, int)

    def test_auto_creates_schema(self, tmp_path: Path) -> None:
        """Test the store auto-creates its schema on first use (init_db pattern)."""
        db_path = str(tmp_path / "fresh.db")
        store = OrderSubmissionStore(db_path)

        # Should not raise even though nothing has initialized this db yet.
        submission_id = store.record_attempt("key-1", "fp-fresh")

        assert submission_id >= 1

    def test_record_attempt_raises_if_no_row_id(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test record_attempt raises if the insert yields no row id.

        Defensive branch: this should be unreachable in practice (SQLite
        and the PostgreSQL adapter's injected ``RETURNING id`` both
        populate ``lastrowid`` for a successful single-row INSERT), but
        mirrors the same guard used by
        :meth:`grocery_butler.pending_actions.PendingActionsStore.insert_pending_action`.
        """
        store = OrderSubmissionStore(str(tmp_path / "test.db"))

        class _NoIdResult:
            """Stub CursorResult whose lastrowid is always None."""

            lastrowid: int | None = None

        class _FakeConnection:
            """Stub DatabaseConnection whose execute() never yields a row id."""

            def execute(self, sql: str, params: object = ()) -> _NoIdResult:
                """Ignore the statement and return a row-id-less result."""
                return _NoIdResult()

            def commit(self) -> None:
                """No-op commit."""

            def close(self) -> None:
                """No-op close."""

        monkeypatch.setattr(store, "_connect", lambda: _FakeConnection())

        with pytest.raises(RuntimeError, match="row id"):
            store.record_attempt("key-1", "fp-no-id")

    def test_recently_submitted_blocks(self, tmp_path: Path) -> None:
        """Test a just-recorded 'submitted' row blocks within the window."""
        store = OrderSubmissionStore(str(tmp_path / "test.db"))
        store.record_attempt("key-1", "fp-1")

        blocking = store.find_recent_blocking("fp-1", within=timedelta(minutes=30))

        assert blocking is not None

    def test_confirmed_status_blocks(self, tmp_path: Path) -> None:
        """Test a row marked 'confirmed' blocks within the window."""
        store = OrderSubmissionStore(str(tmp_path / "test.db"))
        submission_id = store.record_attempt("key-1", "fp-confirmed")
        store.mark(submission_id, "confirmed", order_id="ORD-1")

        blocking = store.find_recent_blocking(
            "fp-confirmed", within=timedelta(minutes=30)
        )

        assert blocking is not None

    def test_unknown_status_blocks(self, tmp_path: Path) -> None:
        """Test a row marked 'unknown' blocks within the window."""
        store = OrderSubmissionStore(str(tmp_path / "test.db"))
        submission_id = store.record_attempt("key-1", "fp-unknown")
        store.mark(submission_id, "unknown")

        blocking = store.find_recent_blocking(
            "fp-unknown", within=timedelta(minutes=30)
        )

        assert blocking is not None

    def test_failed_status_does_not_block(self, tmp_path: Path) -> None:
        """Test a row marked 'failed' does not block resubmission."""
        store = OrderSubmissionStore(str(tmp_path / "test.db"))
        submission_id = store.record_attempt("key-1", "fp-failed")
        store.mark(submission_id, "failed")

        blocking = store.find_recent_blocking("fp-failed", within=timedelta(minutes=30))

        assert blocking is None

    def test_different_fingerprint_not_blocked(self, tmp_path: Path) -> None:
        """Test a row for a different cart fingerprint never blocks."""
        store = OrderSubmissionStore(str(tmp_path / "test.db"))
        store.record_attempt("key-1", "fp-mine")

        blocking = store.find_recent_blocking(
            "fp-someone-elses", within=timedelta(minutes=30)
        )

        assert blocking is None

    def test_mark_accepts_missing_order_id(self, tmp_path: Path) -> None:
        """Test mark() works without an order_id (e.g. for 'failed' status)."""
        store = OrderSubmissionStore(str(tmp_path / "test.db"))
        submission_id = store.record_attempt("key-1", "fp-no-order-id")

        # Should not raise.
        store.mark(submission_id, "failed")

        assert (
            store.find_recent_blocking("fp-no-order-id", within=timedelta(minutes=30))
            is None
        )
