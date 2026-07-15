"""Tests for the /api/v1 destructive endpoints with staged confirmation.

Covers the highest-stakes flow in the stack: ``/order/submit``,
``/brands/set``, and ``/preferences/set`` stage into ``pending_actions``
and execute only through ``/actions/confirm`` (chat-based draft →
confirm → execute per PIVOT.md). No test may ever reach a real Safeway
client — the pipeline factory is mocked everywhere it could execute.
"""

from __future__ import annotations

import datetime as dt
import os
import uuid
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient

from grocery_butler.app import create_app
from grocery_butler.auth_middleware import SECRET_ENV_VAR, mint_token
from grocery_butler.config import ConfigError
from grocery_butler.models import (
    BrandMatchType,
    BrandPreference,
    BrandPreferenceType,
    CartItem,
    CartSummary,
    FulfillmentOption,
    FulfillmentType,
    IngredientCategory,
    PendingActionStatus,
    SafewayProduct,
    ShoppingListItem,
    Unit,
)
from grocery_butler.order_service import OrderConfirmation, OrderResult
from grocery_butler.pending_actions import PendingActionsStore
from grocery_butler.recipe_store import RecipeStore

TEST_SECRET = "test-shared-secret"
SECRET_ENV = {SECRET_ENV_VAR: TEST_SECRET}

DESTRUCTIVE_ENDPOINTS = [
    "/api/v1/order/submit",
    "/api/v1/brands/set",
    "/api/v1/preferences/set",
    "/api/v1/actions/confirm",
    "/api/v1/actions/deny",
]


# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a temporary database path for test isolation."""
    return str(tmp_path / "test_api_destructive.db")


@pytest.fixture()
def app(db_path: str) -> Flask:
    """Create a Flask test app with a temporary database."""
    application = create_app(db_path=db_path)
    application.config["TESTING"] = True
    return application


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    """Return a Flask test client."""
    return app.test_client()


@pytest.fixture()
def pending_store(db_path: str) -> PendingActionsStore:
    """Return a PendingActionsStore bound to the test database."""
    return PendingActionsStore(db_path)


@pytest.fixture()
def recipe_store(db_path: str) -> RecipeStore:
    """Return a RecipeStore bound to the test database."""
    return RecipeStore(db_path)


@pytest.fixture()
def auth_headers() -> dict[str, str]:
    """Return a valid Authorization header minted with the test secret."""
    with patch.dict(os.environ, SECRET_ENV):
        token = mint_token("rubotpaul")
    return {"Authorization": f"Bearer {token}"}


def _make_shopping_item(ingredient: str = "pasta") -> ShoppingListItem:
    """Return a ShoppingListItem for cart payloads."""
    return ShoppingListItem(
        ingredient=ingredient,
        quantity=1.0,
        unit=Unit.LB,
        category=IngredientCategory.PANTRY_DRY,
        search_term=ingredient,
        from_meals=["test pasta"],
    )


def _make_cart_item(
    ingredient: str,
    cost: float,
    *,
    needs_review: bool = False,
    review_reason: str = "",
) -> CartItem:
    """Return a CartItem with the given estimated cost.

    Args:
        ingredient: Ingredient name for the underlying shopping list item.
        cost: Estimated cost assigned to the item.
        needs_review: Whether to flag the item as needing human review.
        review_reason: Machine-readable reason code when flagged.

    Returns:
        A CartItem, optionally flagged for review (issue #59).
    """
    return CartItem(
        shopping_list_item=_make_shopping_item(ingredient),
        safeway_product=SafewayProduct(
            product_id=f"prod-{ingredient}",
            name=f"Brand {ingredient}",
            price=cost,
            size="1 lb",
        ),
        quantity_to_order=1,
        estimated_cost=cost,
        needs_review=needs_review,
        review_reason=review_reason,
    )


def _make_cart(costs: dict[str, float]) -> CartSummary:
    """Return a CartSummary with one item per (ingredient, cost) pair."""
    items = [_make_cart_item(name, cost) for name, cost in costs.items()]
    return CartSummary(
        items=items,
        failed_items=[],
        substituted_items=[],
        restock_items=[],
        subtotal=sum(costs.values()),
        fulfillment_options=[],
        recommended_fulfillment=FulfillmentType.PICKUP,
        estimated_total=sum(costs.values()),
    )


def _make_cart_with_fee(costs: dict[str, float], fee: float) -> CartSummary:
    """Return a CartSummary whose recommended fulfillment option has a fee.

    Unlike :func:`_make_cart` (empty ``fulfillment_options``), this pins
    a non-zero fee on the recommended (pickup) fulfillment option so
    tests can verify the staged total includes it (issue #73).
    ``estimated_total`` deliberately omits the fee, matching the shape
    of a cart built by today's (buggy) pipeline.

    Args:
        costs: Mapping of ingredient name to estimated cost.
        fee: Fee for the recommended pickup fulfillment option.

    Returns:
        A CartSummary with a non-zero recommended-fulfillment fee.
    """
    items = [_make_cart_item(name, cost) for name, cost in costs.items()]
    subtotal = sum(costs.values())
    return CartSummary(
        items=items,
        failed_items=[],
        substituted_items=[],
        restock_items=[],
        subtotal=subtotal,
        fulfillment_options=[
            FulfillmentOption(
                type=FulfillmentType.PICKUP,
                available=True,
                fee=fee,
                windows=[],
            ),
        ],
        recommended_fulfillment=FulfillmentType.PICKUP,
        estimated_total=subtotal,
    )


def _make_cart_with_flagged_item() -> CartSummary:
    """Return a CartSummary with one flagged item and one clean item.

    The flagged item ("spaghetti") carries ``needs_review=True`` with
    reason code ``"incomparable_units"``, matching the real quantity
    calculator's flag for a fixture-style unit mismatch (issue #59).
    """
    flagged = _make_cart_item(
        "spaghetti",
        2.50,
        needs_review=True,
        review_reason="incomparable_units",
    )
    clean = _make_cart_item("ground beef", 5.00)
    items = [flagged, clean]
    return CartSummary(
        items=items,
        failed_items=[],
        substituted_items=[],
        restock_items=[],
        subtotal=7.50,
        fulfillment_options=[],
        recommended_fulfillment=FulfillmentType.PICKUP,
        estimated_total=7.50,
    )


def _make_cart_with_unverified_fulfillment() -> CartSummary:
    """Return a CartSummary flagged fulfillment_unverified=True (issue #72).

    Mirrors ``_make_cart_with_flagged_item`` but for the fulfillment
    gate: exercises the staged-message warning and the confirm-time
    override for a cart whose fulfillment options could not be confirmed
    with Safeway.
    """
    cart = _make_cart({"pasta": 3.50, "sauce": 4.25})
    return cart.model_copy(update={"fulfillment_unverified": True})


def _order_body(costs: dict[str, float] | None = None) -> dict[str, Any]:
    """Return a valid /order/submit request body."""
    cart = _make_cart(costs or {"pasta": 3.50, "sauce": 4.25})
    return {"cart": cart.model_dump(mode="json"), "total": "7.75"}


def _brand_body() -> dict[str, Any]:
    """Return a valid /brands/set request body."""
    return BrandPreference(
        match_target="milk",
        match_type=BrandMatchType.INGREDIENT,
        brand="Clover",
        preference_type=BrandPreferenceType.PREFERRED,
        notes="organic when available",
    ).model_dump(mode="json")


def _successful_order_result() -> OrderResult:
    """Return an OrderResult for a successfully submitted order."""
    return OrderResult(
        success=True,
        confirmation=OrderConfirmation(
            order_id="SW-12345",
            status="confirmed",
            estimated_time="Tomorrow 10am",
            total=7.75,
            fulfillment_type=FulfillmentType.PICKUP,
            item_count=2,
        ),
        items_restocked=1,
    )


def _stage_via_api(
    client: FlaskClient,
    auth_headers: dict[str, str],
    path: str,
    body: dict[str, Any],
) -> str:
    """Stage an action through the API and return its action_id."""
    response = client.post(path, json=body, headers=auth_headers)
    assert response.status_code == 200
    action_id = response.get_json()["action_id"]
    assert isinstance(action_id, str)
    return action_id


# ---------------------------------------------------------------------------
# Auth: every destructive endpoint rejects unauthenticated requests
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestAuthRequired:
    """All destructive endpoints must reject requests without a valid bearer."""

    @pytest.mark.parametrize("path", DESTRUCTIVE_ENDPOINTS)
    def test_missing_bearer_returns_401(self, client: FlaskClient, path: str) -> None:
        """Requests without an Authorization header get a JSON 401."""
        response = client.post(path, json={})
        assert response.status_code == 401
        assert "error" in response.get_json()

    @pytest.mark.parametrize("path", DESTRUCTIVE_ENDPOINTS)
    def test_garbage_bearer_returns_401(self, client: FlaskClient, path: str) -> None:
        """Requests with an invalid token get 401."""
        response = client.post(
            path, json={}, headers={"Authorization": "Bearer not.a.token"}
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /order/submit — stages, never submits
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestOrderSubmitStaging:
    """/order/submit stages a pending action and must not touch Safeway."""

    def test_missing_cart_returns_400(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """A body without a cart is rejected."""
        response = client.post(
            "/api/v1/order/submit", json={"total": "9.99"}, headers=auth_headers
        )
        assert response.status_code == 400
        assert "cart" in response.get_json()["error"]

    def test_invalid_cart_returns_400(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """A cart that fails CartSummary validation is rejected."""
        response = client.post(
            "/api/v1/order/submit",
            json={"cart": {"items": "nope"}},
            headers=auth_headers,
        )
        assert response.status_code == 400
        assert "invalid cart" in response.get_json()["error"]

    def test_empty_cart_returns_400(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """A structurally valid but empty cart is rejected."""
        empty = _make_cart({})
        response = client.post(
            "/api/v1/order/submit",
            json={"cart": empty.model_dump(mode="json")},
            headers=auth_headers,
        )
        assert response.status_code == 400
        assert "empty" in response.get_json()["error"]

    def test_non_object_body_returns_400(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """A non-object JSON body is rejected."""
        response = client.post(
            "/api/v1/order/submit", json=["not", "a", "dict"], headers=auth_headers
        )
        assert response.status_code == 400

    def test_stages_pending_action_and_returns_confirmation_prompt(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        pending_store: PendingActionsStore,
    ) -> None:
        """Submitting a cart stages a pending_actions row with the exact cart."""
        body = _order_body()
        response = client.post("/api/v1/order/submit", json=body, headers=auth_headers)
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "pending_confirmation"
        assert "2 items" in data["message"]
        assert "$7.75" in data["message"]

        action = pending_store.get_pending_action(data["action_id"])
        assert action is not None
        assert action.kind == "safeway_order_submit"
        assert action.status is PendingActionStatus.PENDING
        assert action.payload["cart"] == body["cart"]
        assert action.payload["total"] == "7.75"
        assert not action.is_expired()

    def test_total_defaults_to_cart_total_when_omitted(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        pending_store: PendingActionsStore,
    ) -> None:
        """Without an explicit total, the staged total is computed from the cart."""
        cart = _make_cart({"pasta": 3.50, "sauce": 4.25})
        response = client.post(
            "/api/v1/order/submit",
            json={"cart": cart.model_dump(mode="json")},
            headers=auth_headers,
        )
        assert response.status_code == 200
        action = pending_store.get_pending_action(response.get_json()["action_id"])
        assert action is not None
        assert action.payload["total"] == "7.75"

    def test_staging_never_touches_the_safeway_pipeline(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """Staging an order must not construct or call the Safeway pipeline."""
        factory = MagicMock()
        with patch("grocery_butler.api._safeway_pipeline", factory):
            response = client.post(
                "/api/v1/order/submit", json=_order_body(), headers=auth_headers
            )
        assert response.status_code == 200
        factory.assert_not_called()

    def test_staged_message_lists_flagged_items_and_reasons(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """The staged message must surface flagged ingredients and reasons.

        Per the chief-architect's ruling: a human confirm only counts as
        review approval if flagged items AND their reason codes were
        rendered to that human first. The staging response is what
        RubotPaul posts to chat before asking for confirmation, so it
        must name every flagged ingredient and its reason code.
        """
        cart = _make_cart_with_flagged_item()
        response = client.post(
            "/api/v1/order/submit",
            json={"cart": cart.model_dump(mode="json")},
            headers=auth_headers,
        )
        assert response.status_code == 200
        message = response.get_json()["message"]
        assert "spaghetti" in message
        assert "incomparable_units" in message

    def test_staged_message_has_no_review_section_for_clean_cart(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """A cart with no flagged items keeps the plain staging message."""
        response = client.post(
            "/api/v1/order/submit", json=_order_body(), headers=auth_headers
        )
        assert response.status_code == 200
        message = response.get_json()["message"]
        assert "review" not in message.lower()

    def test_staged_message_includes_unverified_fulfillment_clause(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """The staged message must warn about unverified fulfillment (issue #72).

        Mirrors the needs-review clause: the human must see that
        fulfillment was never actually confirmed with Safeway before
        replying "confirm", since that confirm is the explicit override
        of the fulfillment gate (issue #59 precedent).
        """
        cart = _make_cart_with_unverified_fulfillment()
        response = client.post(
            "/api/v1/order/submit",
            json={"cart": cart.model_dump(mode="json")},
            headers=auth_headers,
        )
        assert response.status_code == 200
        message = response.get_json()["message"].lower()
        assert "fulfillment" in message
        assert "unverified" in message or "unconfirmed" in message


# ---------------------------------------------------------------------------
# POST /order/submit — issue #73: server-trusted total and order-value cap
#
# Today ``post_order_submit`` does
# ``total = str(body.get("total") or _cart_total(cart))``: a client-
# supplied total is trusted verbatim (staged into the confirmation
# message and audit payload), ``_cart_total`` omits the recommended
# fulfillment fee, and there is no order-value cap anywhere. These tests
# pin the fixed behavior: the server always computes the fee-inclusive
# total, a mismatching/non-numeric client total is rejected, and orders
# over a configurable cap are blocked unless explicitly overridden.
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestOrderSubmitTotalValidationAndCap:
    """/order/submit trusts only the server total and enforces a value cap."""

    def test_order_submit_rejects_client_total_mismatch_returns_400(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """Test a client total that mismatches the server total is rejected.

        A $200 cart staged with a lying client total of "7.75" must be
        rejected with 400. Today it is staged as-is: the client's lie
        becomes the audited total.
        """
        cart = _make_cart({"steak": 200.00})
        response = client.post(
            "/api/v1/order/submit",
            json={"cart": cart.model_dump(mode="json"), "total": "7.75"},
            headers=auth_headers,
        )
        assert response.status_code == 400
        assert "total" in response.get_json()["error"].lower()

    @pytest.mark.parametrize(
        "bad_total",
        [
            {"x": 1},
            True,
            [1, 2],
            "not-a-number",
            float("inf"),
            float("nan"),
            "Infinity",
            "NaN",
        ],
        ids=[
            "dict",
            "bool",
            "list",
            "unparseable-str",
            "inf",
            "nan",
            "inf-str",
            "nan-str",
        ],
    )
    def test_order_submit_rejects_non_numeric_total_returns_400(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        bad_total: Any,
    ) -> None:
        """Test non-numeric and non-finite totals are rejected with 400.

        Today's ``body.get("total") or _cart_total(cart)`` accepts any
        truthy value verbatim — a dict or list would even survive
        ``str()`` into the staged message and audit payload. Python's
        json parser also accepts the non-standard ``Infinity``/``NaN``
        literals, and ``"Infinity"``/``"NaN"`` strings are valid Decimal
        literals — quantizing them raises ``InvalidOperation``, so
        without an explicit finiteness check they crash with an
        unhandled 500 instead of this clean 400 (Gate 2.5 review).
        """
        body = _order_body()
        body["total"] = bad_total
        response = client.post("/api/v1/order/submit", json=body, headers=auth_headers)
        assert response.status_code == 400
        assert "total" in response.get_json()["error"].lower()

    def test_order_submit_rejects_non_finite_cart_costs_returns_400(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """Test a cart item with an Infinity estimated_cost is rejected with 400.

        Python's json parser accepts the non-standard ``Infinity``
        literal, and a non-finite item cost reaches
        ``compute_cart_total`` whose cents-quantize step raises an
        uncaught ``decimal.InvalidOperation`` — an unhandled 500 instead
        of a clean 400 (Gate 2.5 review). The model layer must reject
        non-finite monetary fields so ``_parse_cart_payload``'s existing
        ValidationError handler turns this into a 400.
        """
        body = _order_body()
        body["cart"]["items"][0]["estimated_cost"] = float("inf")
        del body["total"]
        response = client.post("/api/v1/order/submit", json=body, headers=auth_headers)
        assert response.status_code == 400
        assert "cart" in response.get_json()["error"].lower()

    def test_order_submit_invalid_cap_config_returns_503(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """Test an unparseable SAFEWAY_ORDER_VALUE_CAP_USD yields a JSON 503.

        The cap gate reads the env var at staging time; if an operator
        has set it to garbage the request must fail with a clean JSON
        503, not an unhandled 500. Issue #77: the body is a terse,
        stable "order configuration invalid" message rather than one
        naming the offending env var or its raw value -- that detail is
        logged server-side instead of relayed to the client.
        """
        body = _order_body()
        del body["total"]
        with patch.dict(os.environ, {"SAFEWAY_ORDER_VALUE_CAP_USD": "garbage"}):
            response = client.post(
                "/api/v1/order/submit", json=body, headers=auth_headers
            )
        assert response.status_code == 503
        assert response.get_json()["error"] == "order configuration invalid"
        text = response.get_data(as_text=True)
        assert "SAFEWAY_ORDER_VALUE_CAP_USD" not in text
        assert "garbage" not in text

    def test_order_submit_accepts_matching_client_total(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        pending_store: PendingActionsStore,
    ) -> None:
        """Test a client total exactly equal to the server total is accepted.

        Once total validation exists, a correct client total must still
        stage normally (200) with the server-computed total persisted
        in the pending payload.
        """
        cart = _make_cart({"pasta": 3.50, "sauce": 4.25})
        response = client.post(
            "/api/v1/order/submit",
            json={"cart": cart.model_dump(mode="json"), "total": "7.75"},
            headers=auth_headers,
        )
        assert response.status_code == 200
        action = pending_store.get_pending_action(response.get_json()["action_id"])
        assert action is not None
        assert action.payload["total"] == "7.75"

    def test_order_submit_message_uses_fee_inclusive_server_total(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        pending_store: PendingActionsStore,
    ) -> None:
        """Test the staged total/message include the recommended fulfillment fee.

        ``_cart_total`` in api.py sums only item/restock costs and
        omits ``cart.fulfillment_options[].fee`` for the recommended
        option. With no client total supplied, the staged total must be
        item-plus-fee inclusive: $3.50 + $4.25 + $2.50 fee = $10.25.
        """
        cart = _make_cart_with_fee({"pasta": 3.50, "sauce": 4.25}, fee=2.50)
        response = client.post(
            "/api/v1/order/submit",
            json={"cart": cart.model_dump(mode="json")},
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.get_json()
        assert "$10.25" in data["message"]
        action = pending_store.get_pending_action(data["action_id"])
        assert action is not None
        assert action.payload["total"] == "10.25"

    def test_order_submit_over_cap_without_override_returns_400(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """Test a cart whose server total exceeds the cap is rejected with 400.

        There is no order-value cap anywhere today, so a cart totalling
        well over $300 stages successfully. The fixed behavior must
        reject staging with 400 mentioning the cap unless overridden.
        """
        cart = _make_cart({"prime rib": 350.00})
        response = client.post(
            "/api/v1/order/submit",
            json={"cart": cart.model_dump(mode="json")},
            headers=auth_headers,
        )
        assert response.status_code == 400
        assert "cap" in response.get_json()["error"].lower()

    def test_order_submit_over_cap_with_override_stages_with_flag(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        pending_store: PendingActionsStore,
    ) -> None:
        """Test override_cap=true stages an over-cap order with the flag persisted.

        When a human explicitly overrides the cap, staging must succeed
        and record ``allow_over_cap=True`` in the pending payload so the
        confirm path can thread it through to OrderService's cap gate.
        """
        cart = _make_cart({"prime rib": 350.00})
        response = client.post(
            "/api/v1/order/submit",
            json={"cart": cart.model_dump(mode="json"), "override_cap": True},
            headers=auth_headers,
        )
        assert response.status_code == 200
        action = pending_store.get_pending_action(response.get_json()["action_id"])
        assert action is not None
        assert action.payload["allow_over_cap"] is True

    def test_confirm_order_submit_passes_allow_over_cap_to_pipeline(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Test confirm forwards allow_over_cap from the staged payload.

        ``_confirm_order_submit`` must read
        ``action.payload.get("allow_over_cap", False)`` and forward it
        to ``pipeline.submit_cart`` so the OrderService cap gate can be
        overridden by a human who already approved the over-cap total
        at staging time.
        """
        cart = _make_cart({"prime rib": 350.00})
        stage_response = client.post(
            "/api/v1/order/submit",
            json={"cart": cart.model_dump(mode="json"), "override_cap": True},
            headers=auth_headers,
        )
        assert stage_response.status_code == 200
        action_id = stage_response.get_json()["action_id"]

        pipeline = MagicMock()
        pipeline.submit_cart.return_value = _successful_order_result()
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = client.post(
                "/api/v1/actions/confirm",
                json={"action_id": action_id},
                headers=auth_headers,
            )

        assert response.status_code == 200
        pipeline.submit_cart.assert_called_once()
        _, kwargs = pipeline.submit_cart.call_args
        assert kwargs.get("allow_over_cap") is True


# ---------------------------------------------------------------------------
# POST /brands/set and /preferences/set — same staging pattern
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestBrandsSetStaging:
    """/brands/set stages a validated brand rule without applying it."""

    def test_stages_pending_action(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        pending_store: PendingActionsStore,
        recipe_store: RecipeStore,
    ) -> None:
        """A valid brand rule is staged, not written to brand preferences."""
        response = client.post(
            "/api/v1/brands/set", json=_brand_body(), headers=auth_headers
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "pending_confirmation"

        action = pending_store.get_pending_action(data["action_id"])
        assert action is not None
        assert action.kind == "brands_set"
        assert action.status is PendingActionStatus.PENDING
        assert action.payload["brand"] == "Clover"
        assert recipe_store.get_brand_preferences() == []

    def test_invalid_brand_payload_returns_400(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """A payload missing required brand fields is rejected."""
        response = client.post(
            "/api/v1/brands/set", json={"brand": "Clover"}, headers=auth_headers
        )
        assert response.status_code == 400
        assert "invalid brand" in response.get_json()["error"]


@patch.dict(os.environ, SECRET_ENV)
class TestPreferencesSetStaging:
    """/preferences/set stages key/value settings without applying them."""

    def test_stages_pending_action(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        pending_store: PendingActionsStore,
        recipe_store: RecipeStore,
    ) -> None:
        """Valid preferences are staged, not written to the store."""
        body = {"preferences": {"fulfillment": "pickup", "store_id": "1234"}}
        response = client.post(
            "/api/v1/preferences/set", json=body, headers=auth_headers
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "pending_confirmation"

        action = pending_store.get_pending_action(data["action_id"])
        assert action is not None
        assert action.kind == "preferences_set"
        assert action.payload == body
        # Staging must not write: neither staged key reaches the store.
        stored = recipe_store.get_all_preferences()
        assert "fulfillment" not in stored
        assert "store_id" not in stored

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"preferences": {}},
            {"preferences": "pickup"},
            {"preferences": {"fulfillment": 7}},
        ],
    )
    def test_invalid_preferences_return_400(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        body: dict[str, Any],
    ) -> None:
        """Missing, empty, or non-string preference payloads are rejected."""
        response = client.post(
            "/api/v1/preferences/set", json=body, headers=auth_headers
        )
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# POST /actions/confirm — executes exactly once
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestActionsConfirm:
    """/actions/confirm resolves staged actions with strict semantics."""

    def test_missing_action_id_returns_400(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """A body without action_id is rejected."""
        response = client.post("/api/v1/actions/confirm", json={}, headers=auth_headers)
        assert response.status_code == 400

    def test_unknown_action_id_returns_404(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """An unknown action_id gets a JSON 404."""
        response = client.post(
            "/api/v1/actions/confirm",
            json={"action_id": str(uuid.uuid4())},
            headers=auth_headers,
        )
        assert response.status_code == 404
        assert "error" in response.get_json()

    def test_expired_action_returns_410_and_marks_expired(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        pending_store: PendingActionsStore,
    ) -> None:
        """Confirming past the TTL returns 410 and resolves the row as expired."""
        action_id = str(uuid.uuid4())
        pending_store.insert_pending_action(
            action_id=action_id,
            kind="preferences_set",
            payload={"preferences": {"fulfillment": "pickup"}},
            expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
        )
        response = client.post(
            "/api/v1/actions/confirm",
            json={"action_id": action_id},
            headers=auth_headers,
        )
        assert response.status_code == 410
        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.EXPIRED
        assert action.resolved_at is not None

    def test_confirm_order_calls_submit_with_exact_staged_cart(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        pending_store: PendingActionsStore,
    ) -> None:
        """Confirming an order submits the exact staged cart and approves the row."""
        body = _order_body()
        action_id = _stage_via_api(client, auth_headers, "/api/v1/order/submit", body)

        pipeline = MagicMock()
        pipeline.submit_cart.return_value = _successful_order_result()
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = client.post(
                "/api/v1/actions/confirm",
                json={"action_id": action_id},
                headers=auth_headers,
            )

        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "approved"
        assert data["result"]["order_id"] == "SW-12345"
        assert data["result"]["items_restocked"] == 1

        pipeline.submit_cart.assert_called_once()
        submitted_cart = pipeline.submit_cart.call_args.args[0]
        assert submitted_cart.model_dump(mode="json") == body["cart"]
        pipeline.close.assert_called_once()

        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.APPROVED
        assert action.resolved_at is not None

    def test_confirm_order_forwards_allow_review_items_override(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Confirming a staged order must override the review gate.

        Per the chief-architect's ruling: the human already saw the
        flagged items and their reason codes in the staged message
        before replying "confirm", so that confirm IS the explicit
        review approval. The confirm executor must therefore call
        ``pipeline.submit_cart`` with ``allow_review_items=True`` --
        otherwise a flagged cart the human already approved would still
        be hard-blocked by ``OrderService.submit_order``.
        """
        body = {"cart": _make_cart_with_flagged_item().model_dump(mode="json")}
        action_id = _stage_via_api(client, auth_headers, "/api/v1/order/submit", body)

        pipeline = MagicMock()
        pipeline.submit_cart.return_value = _successful_order_result()
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = client.post(
                "/api/v1/actions/confirm",
                json={"action_id": action_id},
                headers=auth_headers,
            )

        assert response.status_code == 200
        pipeline.submit_cart.assert_called_once()
        _, kwargs = pipeline.submit_cart.call_args
        assert kwargs.get("allow_review_items") is True

    def test_confirm_order_forwards_allow_unverified_fulfillment_override(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Confirming a staged order must override the fulfillment gate.

        Issue #72: the staged message (post_order_submit) already warned
        that fulfillment was unconfirmed before the human replied
        "confirm", so that confirm IS the explicit human override
        (mirrors the issue #59 review-gate precedent). The confirm
        executor must call ``pipeline.submit_cart`` with
        ``allow_unverified_fulfillment=True``.
        """
        body = {
            "cart": _make_cart_with_unverified_fulfillment().model_dump(mode="json")
        }
        action_id = _stage_via_api(client, auth_headers, "/api/v1/order/submit", body)

        pipeline = MagicMock()
        pipeline.submit_cart.return_value = _successful_order_result()
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = client.post(
                "/api/v1/actions/confirm",
                json={"action_id": action_id},
                headers=auth_headers,
            )

        assert response.status_code == 200
        pipeline.submit_cart.assert_called_once()
        _, kwargs = pipeline.submit_cart.call_args
        assert kwargs.get("allow_unverified_fulfillment") is True

    def test_failed_order_submission_returns_502(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        pending_store: PendingActionsStore,
    ) -> None:
        """A failed Safeway submission reports a terse 502; the claim is consumed.

        Issue #77: the raw ``error_message`` from an unmapped-outcome
        OrderResult failure must not be relayed verbatim into the
        response body — "Safeway is down" is a stand-in for text that,
        in production, could carry internal detail. The body must use
        the fixed, terse "order submission failed" message instead.
        """
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/order/submit", _order_body()
        )
        pipeline = MagicMock()
        pipeline.submit_cart.return_value = OrderResult(
            success=False, error_message="Safeway is down"
        )
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = client.post(
                "/api/v1/actions/confirm",
                json={"action_id": action_id},
                headers=auth_headers,
            )
        assert response.status_code == 502
        assert response.get_json()["error"] == "order submission failed"
        assert "Safeway is down" not in response.get_data(as_text=True)
        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.APPROVED

    def test_unavailable_pipeline_returns_503_and_keeps_action_pending(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        pending_store: PendingActionsStore,
    ) -> None:
        """If the pipeline can't start, the action stays pending for retry."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/order/submit", _order_body()
        )
        with patch(
            "grocery_butler.api._safeway_pipeline",
            side_effect=ConfigError("SAFEWAY_USERNAME missing"),
        ):
            response = client.post(
                "/api/v1/actions/confirm",
                json={"action_id": action_id},
                headers=auth_headers,
            )
        assert response.status_code == 503
        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.PENDING

    def test_disabled_submission_returns_501_and_keeps_action_pending(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        pending_store: PendingActionsStore,
    ) -> None:
        """Issue #60: a disabled pipeline returns 501 and leaves the row pending.

        The pending action must remain retriable (not claimed) so that
        flipping ``SAFEWAY_ORDER_SUBMISSION_ENABLED`` on later lets the
        same confirm request succeed.
        """
        from grocery_butler.order_service import ORDER_SUBMISSION_DISABLED_MESSAGE

        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/order/submit", _order_body()
        )
        pipeline = MagicMock()
        pipeline.order_submission_enabled = False
        pipeline.submit_cart.return_value = _successful_order_result()
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = client.post(
                "/api/v1/actions/confirm",
                json={"action_id": action_id},
                headers=auth_headers,
            )

        assert response.status_code == 501
        assert response.get_json()["error"] == ORDER_SUBMISSION_DISABLED_MESSAGE
        pipeline.submit_cart.assert_not_called()
        pipeline.close.assert_called_once()

        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.PENDING

    def test_double_confirm_returns_409(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Confirming an already-approved action returns 409."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        first = client.post(
            "/api/v1/actions/confirm",
            json={"action_id": action_id},
            headers=auth_headers,
        )
        assert first.status_code == 200
        second = client.post(
            "/api/v1/actions/confirm",
            json={"action_id": action_id},
            headers=auth_headers,
        )
        assert second.status_code == 409
        assert "error" in second.get_json()

    def test_confirm_after_deny_returns_409(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Confirming a denied action returns 409."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        denied = client.post(
            "/api/v1/actions/deny",
            json={"action_id": action_id},
            headers=auth_headers,
        )
        assert denied.status_code == 200
        response = client.post(
            "/api/v1/actions/confirm",
            json={"action_id": action_id},
            headers=auth_headers,
        )
        assert response.status_code == 409

    def test_confirm_brands_applies_rule(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        recipe_store: RecipeStore,
        pending_store: PendingActionsStore,
    ) -> None:
        """Confirming a brands_set action writes the brand rule."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        response = client.post(
            "/api/v1/actions/confirm",
            json={"action_id": action_id},
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "approved"
        assert data["result"]["brand"] == "Clover"

        prefs = recipe_store.get_brand_preferences()
        assert len(prefs) == 1
        assert prefs[0].brand == "Clover"
        assert prefs[0].match_target == "milk"
        # The confirmation payload must carry the real inserted row id, not
        # the BrandPreference model's None default (regression: issue #63).
        assert prefs[0].id is not None
        assert data["result"]["id"] == prefs[0].id

        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.APPROVED

    def test_confirm_preferences_applies_settings(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        recipe_store: RecipeStore,
    ) -> None:
        """Confirming a preferences_set action writes every key/value pair."""
        body = {"preferences": {"fulfillment": "pickup", "store_id": "1234"}}
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/preferences/set", body
        )
        response = client.post(
            "/api/v1/actions/confirm",
            json={"action_id": action_id},
            headers=auth_headers,
        )
        assert response.status_code == 200
        assert response.get_json()["result"]["updated"] == 2
        stored = recipe_store.get_all_preferences()
        assert stored["fulfillment"] == "pickup"
        assert stored["store_id"] == "1234"


# ---------------------------------------------------------------------------
# POST /actions/deny — audit-trail denial
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestActionsDeny:
    """/actions/deny resolves staged actions as denied."""

    def test_missing_action_id_returns_400(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """A body without action_id is rejected."""
        response = client.post("/api/v1/actions/deny", json={}, headers=auth_headers)
        assert response.status_code == 400

    def test_unknown_action_id_returns_404(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """An unknown action_id gets 404."""
        response = client.post(
            "/api/v1/actions/deny",
            json={"action_id": str(uuid.uuid4())},
            headers=auth_headers,
        )
        assert response.status_code == 404

    def test_denies_pending_action(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        pending_store: PendingActionsStore,
        recipe_store: RecipeStore,
    ) -> None:
        """Denying marks the row denied and never applies the change."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        response = client.post(
            "/api/v1/actions/deny",
            json={"action_id": action_id},
            headers=auth_headers,
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "denied"
        assert data["action_id"] == action_id

        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.DENIED
        assert action.resolved_at is not None
        assert recipe_store.get_brand_preferences() == []

    def test_double_deny_returns_409(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Denying an already-denied action returns 409."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        first = client.post(
            "/api/v1/actions/deny",
            json={"action_id": action_id},
            headers=auth_headers,
        )
        assert first.status_code == 200
        second = client.post(
            "/api/v1/actions/deny",
            json={"action_id": action_id},
            headers=auth_headers,
        )
        assert second.status_code == 409


# ---------------------------------------------------------------------------
# Issue #61: idempotency-key forwarding and outcome-aware status codes
#
# ``OrderOutcome`` is imported inside each test body rather than at module
# scope because these tests were written test-first, before the name
# existed; the local imports are kept as a historic artifact of that TDD
# process.
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestConfirmOrderIdempotency:
    """/actions/confirm forwards an idempotency key and reports new outcomes."""

    def test_confirm_order_passes_action_id_as_idempotency_key(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
    ) -> None:
        """Test the staged action_id is forwarded as the idempotency key."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/order/submit", _order_body()
        )
        pipeline = MagicMock()
        pipeline.submit_cart.return_value = _successful_order_result()
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = client.post(
                "/api/v1/actions/confirm",
                json={"action_id": action_id},
                headers=auth_headers,
            )

        assert response.status_code == 200
        pipeline.submit_cart.assert_called_once()
        _args, kwargs = pipeline.submit_cart.call_args
        assert kwargs.get("idempotency_key") == action_id

    def test_unknown_outcome_returns_504(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        pending_store: PendingActionsStore,
    ) -> None:
        """Test an UNKNOWN order outcome surfaces as HTTP 504 with status unknown.

        Issue #77: the mapped-outcome response must keep ``status`` and
        ``action_id`` (RubotPaul needs those to route the reply) but
        must not relay the raw ``error_message`` free text verbatim —
        this OrderResult's message is a stand-in for text that could
        carry internal detail in production.
        """
        from grocery_butler.order_service import OrderOutcome

        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/order/submit", _order_body()
        )
        pipeline = MagicMock()
        pipeline.submit_cart.return_value = OrderResult(
            success=False,
            outcome=OrderOutcome.UNKNOWN,
            error_message="Order outcome unknown — request timed out",
        )
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = client.post(
                "/api/v1/actions/confirm",
                json={"action_id": action_id},
                headers=auth_headers,
            )

        assert response.status_code == 504
        data = response.get_json()
        assert data["status"] == "unknown"
        assert data["action_id"] == action_id
        assert "error" in data
        assert "request timed out" not in data["error"]

        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.APPROVED

    def test_duplicate_outcome_returns_409(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        pending_store: PendingActionsStore,
    ) -> None:
        """Test a DUPLICATE order outcome surfaces as HTTP 409 duplicate_prevented.

        Issue #77: the mapped-outcome response must keep ``status`` and
        ``action_id`` but must not relay the raw ``error_message`` free
        text verbatim — this OrderResult's message is a stand-in for
        text that could carry internal detail in production.
        """
        from grocery_butler.order_service import OrderOutcome

        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/order/submit", _order_body()
        )
        pipeline = MagicMock()
        pipeline.submit_cart.return_value = OrderResult(
            success=False,
            outcome=OrderOutcome.DUPLICATE,
            error_message="Duplicate order blocked — a recent submission is pending",
        )
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = client.post(
                "/api/v1/actions/confirm",
                json={"action_id": action_id},
                headers=auth_headers,
            )

        assert response.status_code == 409
        data = response.get_json()
        assert data["status"] == "duplicate_prevented"
        assert data["action_id"] == action_id
        assert "error" in data
        assert "Duplicate order blocked" not in data["error"]

        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.APPROVED
