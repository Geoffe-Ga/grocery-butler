"""Tests for the /api/v1 destructive endpoints with staged confirmation.

Covers the highest-stakes flow in the stack: ``/order/submit``,
``/brands/set``, and ``/preferences/set`` stage into ``pending_actions``
and execute only through ``/actions/confirm`` (chat-based draft →
confirm → execute per PIVOT.md). No test may ever reach a real Safeway
client — the pipeline factory is mocked everywhere it could execute.

Issue #74: every bearer token minted in this module is now bound to the
exact (method, path, body) of the request it authorizes, via the
shared ``tests.conftest.bearer_header`` helper. A single static header
cannot cover the many different requests exercised here, so the
``auth_headers`` fixture is a per-request minting factory rather than a
static header dict.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import uuid
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient

    AuthHeaderFactory = Callable[[str, str, bytes], dict[str, str]]

from grocery_butler.api import api_v1
from grocery_butler.app import create_app
from grocery_butler.auth_middleware import SECRET_ENV_VAR
from grocery_butler.config import ConfigError
from grocery_butler.db import get_connection
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
from grocery_butler.order_service import (
    OrderConfirmation,
    OrderResult,
    compute_cart_total,
)
from grocery_butler.pending_actions import PendingActionsStore
from grocery_butler.recipe_store import RecipeStore
from grocery_butler.safeway_pipeline import SafewayPipelineError
from tests.conftest import bearer_header

TEST_SECRET = "test-shared-secret"
SECRET_ENV = {SECRET_ENV_VAR: TEST_SECRET}

DESTRUCTIVE_ENDPOINTS = [
    "/api/v1/order/submit",
    "/api/v1/brands/set",
    "/api/v1/preferences/set",
    "/api/v1/actions/confirm",
    "/api/v1/actions/deny",
]

#: Issue #74 AC#2: a view that never calls require_bearer() itself, used
#: to prove the api_v1 blueprint's before_request hook enforces auth
#: even when a route forgets the per-route check. Registered once, at
#: import time -- Flask blueprints only accept new routes before
#: they've been applied to an app via register_blueprint, and api_v1 is
#: a process-wide singleton reused by every create_app() call in this
#: suite, so this must be added before any test in the suite calls
#: create_app().
UNAUTHENTICATED_THROWAWAY_PATH = "/api/v1/_test_unauthenticated_view_issue_74"


@api_v1.get("/_test_unauthenticated_view_issue_74")
def _unauthenticated_throwaway_view() -> dict[str, bool]:
    """Return a fixed payload without ever calling require_bearer().

    Exists solely so ``TestAuthByDefault`` can prove that unauthenticated
    requests are still rejected -- by the blueprint's before_request
    hook, not by this view.
    """
    return {"ok": True}


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
def auth_headers() -> AuthHeaderFactory:
    """Return a factory that mints a bearer header for one exact request.

    A single static header cannot authorize every request exercised in
    this module -- each destructive-endpoint call has its own method,
    path, and body, and the request-bound token contract (issue #74)
    signs all three together. Callers invoke the returned factory once
    per request, e.g.
    ``auth_headers("POST", "/api/v1/order/submit", serialized_body)``.

    Returns:
        A callable ``(method, path, body=b"") -> {"Authorization": ...}``
        that mints tokens under the test shared secret.
    """
    return _auth_factory_for("rubotpaul")


def _post(
    client: FlaskClient,
    auth_headers: AuthHeaderFactory,
    path: str,
    body: Any,
) -> Any:
    """POST a JSON-serializable body with a token bound to the exact bytes sent.

    Serializes ``body`` exactly once so the identical bytes are both
    sent as the request payload and hashed into the bearer token's
    binding -- the two must match byte-for-byte for the server's HMAC
    verification to succeed (issue #74).

    Args:
        client: Flask test client.
        auth_headers: Factory fixture minting a bearer header for one
            exact (method, path, body) triple.
        path: Request path to POST to.
        body: JSON-serializable request payload.

    Returns:
        The Flask test client's response.
    """
    serialized = json.dumps(body).encode()
    headers = auth_headers("POST", path, serialized)
    return client.post(
        path, data=serialized, content_type="application/json", headers=headers
    )


def _auth_factory_for(caller_id: str) -> AuthHeaderFactory:
    """Return an auth-header factory minting request-bound tokens for one caller.

    Used to distinguish the staging caller (requester) from the
    confirming/denying caller (resolver) in W1 audit-trail tests
    (issue #75), while still honoring the request-bound token contract
    (issue #74): every minted header is valid only for the exact
    (method, path, body) it was created for.

    Args:
        caller_id: The caller identity to embed in every minted token.

    Returns:
        A callable ``(method, path, body=b"") -> {"Authorization": ...}``
        that mints bound tokens for ``caller_id`` under the test secret.
    """

    def _make(method: str, path: str, body: bytes = b"") -> dict[str, str]:
        """Mint a bearer header for this caller bound to method/path/body."""
        with patch.dict(os.environ, SECRET_ENV):
            return bearer_header(caller_id, method, path, body)

    return _make


def _get(
    client: FlaskClient,
    auth_headers: AuthHeaderFactory,
    path: str,
    query: str = "",
) -> Any:
    """GET a path with a token bound to the exact request.

    The server binds tokens to ``request.path`` (query string excluded),
    so the token is minted for ``path`` alone while the request URL may
    carry a query string (issue #74).

    Args:
        client: Flask test client.
        auth_headers: Factory fixture minting a bearer header for one
            exact (method, path, body) triple.
        path: Request path (no query string) the token is bound to.
        query: Optional query string (including ``?``) appended to the
            request URL only.

    Returns:
        The Flask test client's response.
    """
    headers = auth_headers("GET", path, b"")
    return client.get(f"{path}{query}", headers=headers)


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
    """Return a valid /order/submit request body.

    The client ``total`` is derived with the same server-side
    :func:`grocery_butler.order_service.compute_cart_total` the endpoint
    uses, because /order/submit rejects any client total that does not
    match the server-computed, fee-inclusive total (Issue #73).
    """
    cart = _make_cart(costs or {"pasta": 3.50, "sauce": 4.25})
    return {
        "cart": cart.model_dump(mode="json"),
        "total": str(compute_cart_total(cart)),
    }


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
    auth_headers: AuthHeaderFactory,
    path: str,
    body: dict[str, Any],
) -> str:
    """Stage an action through the API and return its action_id."""
    response = _post(client, auth_headers, path, body)
    assert response.status_code == 200
    action_id = response.get_json()["action_id"]
    assert isinstance(action_id, str)
    return action_id


# ---------------------------------------------------------------------------
# W1 (issue #75): requester/resolver recorded from the real caller_id
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestRequesterAndResolverAtRoutes:
    """Every staged/resolved action records the real caller (W1, issue #75).

    Two distinct callers are minted so requester (the staging caller) and
    resolver (the confirming/denying caller) can be told apart in the
    audit trail -- neither should ever fall back to the old
    "rubotpaul"-only default.
    """

    def test_order_submit_records_requester_as_caller_id(
        self, client: FlaskClient, pending_store: PendingActionsStore
    ) -> None:
        """Staging an order records the token's caller_id as requester."""
        response = _post(
            client, _auth_factory_for("alice"), "/api/v1/order/submit", _order_body()
        )
        assert response.status_code == 200
        action = pending_store.get_pending_action(response.get_json()["action_id"])
        assert action is not None
        assert action.requester == "alice"

    def test_brands_set_records_requester_as_caller_id(
        self, client: FlaskClient, pending_store: PendingActionsStore
    ) -> None:
        """Staging a brand rule records the token's caller_id as requester."""
        response = _post(
            client, _auth_factory_for("bob"), "/api/v1/brands/set", _brand_body()
        )
        assert response.status_code == 200
        action = pending_store.get_pending_action(response.get_json()["action_id"])
        assert action is not None
        assert action.requester == "bob"

    def test_preferences_set_records_requester_as_caller_id(
        self, client: FlaskClient, pending_store: PendingActionsStore
    ) -> None:
        """Staging preferences records the token's caller_id as requester."""
        body = {"preferences": {"fulfillment": "pickup"}}
        response = _post(
            client, _auth_factory_for("carol"), "/api/v1/preferences/set", body
        )
        assert response.status_code == 200
        action = pending_store.get_pending_action(response.get_json()["action_id"])
        assert action is not None
        assert action.requester == "carol"

    def test_confirm_records_resolver_as_confirming_caller_id(
        self,
        client: FlaskClient,
        pending_store: PendingActionsStore,
    ) -> None:
        """Confirming stamps resolver with the CONFIRMING caller, not requester."""
        action_id = _stage_via_api(
            client, _auth_factory_for("alice"), "/api/v1/brands/set", _brand_body()
        )
        response = _post(
            client,
            _auth_factory_for("bob"),
            "/api/v1/actions/confirm",
            {"action_id": action_id},
        )
        assert response.status_code == 200
        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.requester == "alice"
        assert action.resolver == "bob"

    def test_deny_records_resolver_as_denying_caller_id(
        self,
        client: FlaskClient,
        pending_store: PendingActionsStore,
    ) -> None:
        """Denying stamps the resolver with the DENYING caller."""
        action_id = _stage_via_api(
            client, _auth_factory_for("alice"), "/api/v1/brands/set", _brand_body()
        )
        response = _post(
            client,
            _auth_factory_for("dave"),
            "/api/v1/actions/deny",
            {"action_id": action_id},
        )
        assert response.status_code == 200
        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.resolver == "dave"

    def test_system_expiry_via_confirm_leaves_resolver_none(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
    ) -> None:
        """Confirming an expired action resolves it as expired, no resolver.

        Expiry is a system-initiated resolution (the human never actually
        confirmed anything -- the TTL did), so the resolver must stay
        NULL even though a caller made the doomed confirm request.
        """
        action_id = str(uuid.uuid4())
        pending_store.insert_pending_action(
            action_id=action_id,
            kind="preferences_set",
            payload={"preferences": {"fulfillment": "pickup"}},
            expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
        )
        response = _post(
            client, auth_headers, "/api/v1/actions/confirm", {"action_id": action_id}
        )
        assert response.status_code == 410
        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.EXPIRED
        assert action.resolver is None


# ---------------------------------------------------------------------------
# W2 (issue #75): every transition logs INFO with action_id/kind, never
# payload contents
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestActionAuditLogging:
    """Every staged-action transition is logged at INFO (W2, issue #75).

    Uses a distinctive ingredient name as a payload-content marker: none
    of the captured log messages may ever contain it, proving the audit
    trail logs metadata (action_id, kind) and never the payload itself.
    """

    _MARKER_INGREDIENT = "unobtainium-marker-item"

    def _order_body_with_marker(self) -> dict[str, Any]:
        """Return an /order/submit body containing the payload marker."""
        return _order_body({self._MARKER_INGREDIENT: 9.99})

    def test_staging_order_logs_info_with_action_id_and_total(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Staging an order logs an INFO line naming the action and kind."""
        with caplog.at_level(logging.INFO, logger="api"):
            response = _post(
                client,
                auth_headers,
                "/api/v1/order/submit",
                self._order_body_with_marker(),
            )
        assert response.status_code == 200
        action_id = response.get_json()["action_id"]
        messages = [r.getMessage() for r in caplog.records if r.name == "api"]
        assert any(action_id in m for m in messages)
        assert any("safeway_order_submit" in m for m in messages)
        assert not any(self._MARKER_INGREDIENT in m for m in messages)

    def test_staging_brands_logs_info_with_action_id_and_kind(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Staging a brand rule logs an INFO line, never the brand name."""
        with caplog.at_level(logging.INFO, logger="api"):
            response = _post(client, auth_headers, "/api/v1/brands/set", _brand_body())
        assert response.status_code == 200
        action_id = response.get_json()["action_id"]
        messages = [r.getMessage() for r in caplog.records if r.name == "api"]
        assert any(action_id in m and "brands_set" in m for m in messages)
        assert not any("Clover" in m for m in messages)

    def test_confirm_logs_info_with_action_id_and_kind(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Confirming a staged action logs a transition INFO line."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="api"):
            response = _post(
                client,
                auth_headers,
                "/api/v1/actions/confirm",
                {"action_id": action_id},
            )
        assert response.status_code == 200
        messages = [r.getMessage() for r in caplog.records if r.name == "api"]
        assert any(action_id in m and "brands_set" in m for m in messages)
        assert not any("Clover" in m for m in messages)

    def test_confirm_failure_logs_failed_transition(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A failed order submission logs a 'failed' transition line."""
        action_id = _stage_via_api(
            client,
            auth_headers,
            "/api/v1/order/submit",
            self._order_body_with_marker(),
        )
        pipeline = MagicMock()
        pipeline.submit_cart.return_value = OrderResult(
            success=False, error_message="Safeway is down"
        )
        caplog.clear()
        with (
            patch("grocery_butler.api._safeway_pipeline", return_value=pipeline),
            caplog.at_level(logging.INFO, logger="api"),
        ):
            response = _post(
                client,
                auth_headers,
                "/api/v1/actions/confirm",
                {"action_id": action_id},
            )
        assert response.status_code == 502
        messages = [r.getMessage() for r in caplog.records if r.name == "api"]
        assert any(action_id in m and "failed" in m.lower() for m in messages)
        assert not any(self._MARKER_INGREDIENT in m for m in messages)

    def test_deny_logs_info_with_action_id_and_kind(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Denying a staged action logs a 'denied' transition line."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="api"):
            response = _post(
                client, auth_headers, "/api/v1/actions/deny", {"action_id": action_id}
            )
        assert response.status_code == 200
        messages = [r.getMessage() for r in caplog.records if r.name == "api"]
        assert any(action_id in m and "denied" in m.lower() for m in messages)
        assert not any("Clover" in m for m in messages)

    def test_expire_logs_info_with_action_id_and_kind(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Confirming an expired action logs an 'expired' transition line."""
        action_id = str(uuid.uuid4())
        pending_store.insert_pending_action(
            action_id=action_id,
            kind="preferences_set",
            payload={"preferences": {"fulfillment": "pickup"}},
            expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
        )
        with caplog.at_level(logging.INFO, logger="api"):
            response = _post(
                client,
                auth_headers,
                "/api/v1/actions/confirm",
                {"action_id": action_id},
            )
        assert response.status_code == 410
        messages = [r.getMessage() for r in caplog.records if r.name == "api"]
        assert any(action_id in m and "expired" in m.lower() for m in messages)


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
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """A body without a cart is rejected."""
        response = _post(
            client, auth_headers, "/api/v1/order/submit", {"total": "9.99"}
        )
        assert response.status_code == 400
        assert "cart" in response.get_json()["error"]

    def test_invalid_cart_returns_400(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """A cart that fails CartSummary validation is rejected."""
        response = _post(
            client,
            auth_headers,
            "/api/v1/order/submit",
            {"cart": {"items": "nope"}},
        )
        assert response.status_code == 400
        assert "invalid cart" in response.get_json()["error"]

    def test_empty_cart_returns_400(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """A structurally valid but empty cart is rejected."""
        empty = _make_cart({})
        response = _post(
            client,
            auth_headers,
            "/api/v1/order/submit",
            {"cart": empty.model_dump(mode="json")},
        )
        assert response.status_code == 400
        assert "empty" in response.get_json()["error"]

    def test_non_object_body_returns_400(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """A non-object JSON body is rejected."""
        response = _post(
            client, auth_headers, "/api/v1/order/submit", ["not", "a", "dict"]
        )
        assert response.status_code == 400

    def test_stages_pending_action_and_returns_confirmation_prompt(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
    ) -> None:
        """Submitting a cart stages a pending_actions row with the exact cart."""
        body = _order_body()
        response = _post(client, auth_headers, "/api/v1/order/submit", body)
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
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
    ) -> None:
        """Without an explicit total, the staged total is computed from the cart."""
        cart = _make_cart({"pasta": 3.50, "sauce": 4.25})
        response = _post(
            client,
            auth_headers,
            "/api/v1/order/submit",
            {"cart": cart.model_dump(mode="json")},
        )
        assert response.status_code == 200
        action = pending_store.get_pending_action(response.get_json()["action_id"])
        assert action is not None
        assert action.payload["total"] == "7.75"

    def test_non_numeric_total_returns_400(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """A total that isn't a numeric amount is rejected, not staged."""
        cart = _make_cart({"pasta": 3.50, "sauce": 4.25})
        response = _post(
            client,
            auth_headers,
            "/api/v1/order/submit",
            {"cart": cart.model_dump(mode="json"), "total": "not-a-number"},
        )
        assert response.status_code == 400
        assert "total" in response.get_json()["error"]

    def test_total_with_embedded_newline_returns_400_and_is_not_logged(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A total crafted to forge a fake log line is rejected before staging.

        Regression test (issue #75, log hygiene): an unvalidated client
        ``total`` used to flow verbatim into the ``staged action_id=...``
        audit-log line, letting a caller embed a newline and forge a fake
        transition line. It must now be rejected outright rather than
        staged or logged.
        """
        cart = _make_cart({"pasta": 3.50, "sauce": 4.25})
        forged = "7.75\nfailed action_id=forged-marker kind=safeway_order_submit"
        with caplog.at_level(logging.INFO, logger="api"):
            response = _post(
                client,
                auth_headers,
                "/api/v1/order/submit",
                {"cart": cart.model_dump(mode="json"), "total": forged},
            )
        assert response.status_code == 400
        messages = [r.getMessage() for r in caplog.records if r.name == "api"]
        assert not any("forged-marker" in m for m in messages)

    def test_staging_never_touches_the_safeway_pipeline(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """Staging an order must not construct or call the Safeway pipeline."""
        factory = MagicMock()
        with patch("grocery_butler.api._safeway_pipeline", factory):
            response = _post(
                client, auth_headers, "/api/v1/order/submit", _order_body()
            )
        assert response.status_code == 200
        factory.assert_not_called()

    def test_staged_message_lists_flagged_items_and_reasons(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """The staged message must surface flagged ingredients and reasons.

        Per the chief-architect's ruling: a human confirm only counts as
        review approval if flagged items AND their reason codes were
        rendered to that human first. The staging response is what
        RubotPaul posts to chat before asking for confirmation, so it
        must name every flagged ingredient and its reason code.
        """
        cart = _make_cart_with_flagged_item()
        response = _post(
            client,
            auth_headers,
            "/api/v1/order/submit",
            {"cart": cart.model_dump(mode="json")},
        )
        assert response.status_code == 200
        message = response.get_json()["message"]
        assert "spaghetti" in message
        assert "incomparable_units" in message

    def test_staged_message_has_no_review_section_for_clean_cart(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """A cart with no flagged items keeps the plain staging message."""
        response = _post(client, auth_headers, "/api/v1/order/submit", _order_body())
        assert response.status_code == 200
        message = response.get_json()["message"]
        assert "review" not in message.lower()

    def test_staged_message_includes_unverified_fulfillment_clause(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """The staged message must warn about unverified fulfillment (issue #72).

        Mirrors the needs-review clause: the human must see that
        fulfillment was never actually confirmed with Safeway before
        replying "confirm", since that confirm is the explicit override
        of the fulfillment gate (issue #59 precedent).
        """
        cart = _make_cart_with_unverified_fulfillment()
        response = _post(
            client,
            auth_headers,
            "/api/v1/order/submit",
            {"cart": cart.model_dump(mode="json")},
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
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """Test a client total that mismatches the server total is rejected.

        A $200 cart staged with a lying client total of "7.75" must be
        rejected with 400. Today it is staged as-is: the client's lie
        becomes the audited total.
        """
        cart = _make_cart({"steak": 200.00})
        response = _post(
            client,
            auth_headers,
            "/api/v1/order/submit",
            {"cart": cart.model_dump(mode="json"), "total": "7.75"},
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
        auth_headers: AuthHeaderFactory,
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
        response = _post(client, auth_headers, "/api/v1/order/submit", body)
        assert response.status_code == 400
        assert "total" in response.get_json()["error"].lower()

    def test_order_submit_rejects_non_finite_cart_costs_returns_400(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
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
        response = _post(client, auth_headers, "/api/v1/order/submit", body)
        assert response.status_code == 400
        assert "cart" in response.get_json()["error"].lower()

    def test_order_submit_invalid_cap_config_returns_503(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
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
            response = _post(client, auth_headers, "/api/v1/order/submit", body)
        assert response.status_code == 503
        assert response.get_json()["error"] == "order configuration invalid"
        text = response.get_data(as_text=True)
        assert "SAFEWAY_ORDER_VALUE_CAP_USD" not in text
        assert "garbage" not in text

    def test_order_submit_accepts_matching_client_total(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
    ) -> None:
        """Test a client total exactly equal to the server total is accepted.

        Once total validation exists, a correct client total must still
        stage normally (200) with the server-computed total persisted
        in the pending payload.
        """
        cart = _make_cart({"pasta": 3.50, "sauce": 4.25})
        response = _post(
            client,
            auth_headers,
            "/api/v1/order/submit",
            {"cart": cart.model_dump(mode="json"), "total": "7.75"},
        )
        assert response.status_code == 200
        action = pending_store.get_pending_action(response.get_json()["action_id"])
        assert action is not None
        assert action.payload["total"] == "7.75"

    def test_order_submit_message_uses_fee_inclusive_server_total(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
    ) -> None:
        """Test the staged total/message include the recommended fulfillment fee.

        ``_cart_total`` in api.py sums only item/restock costs and
        omits ``cart.fulfillment_options[].fee`` for the recommended
        option. With no client total supplied, the staged total must be
        item-plus-fee inclusive: $3.50 + $4.25 + $2.50 fee = $10.25.
        """
        cart = _make_cart_with_fee({"pasta": 3.50, "sauce": 4.25}, fee=2.50)
        response = _post(
            client,
            auth_headers,
            "/api/v1/order/submit",
            {"cart": cart.model_dump(mode="json")},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert "$10.25" in data["message"]
        action = pending_store.get_pending_action(data["action_id"])
        assert action is not None
        assert action.payload["total"] == "10.25"

    def test_order_submit_over_cap_without_override_returns_400(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """Test a cart whose server total exceeds the cap is rejected with 400.

        There is no order-value cap anywhere today, so a cart totalling
        well over $300 stages successfully. The fixed behavior must
        reject staging with 400 mentioning the cap unless overridden.
        """
        cart = _make_cart({"prime rib": 350.00})
        response = _post(
            client,
            auth_headers,
            "/api/v1/order/submit",
            {"cart": cart.model_dump(mode="json")},
        )
        assert response.status_code == 400
        assert "cap" in response.get_json()["error"].lower()

    def test_order_submit_over_cap_with_override_stages_with_flag(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
    ) -> None:
        """Test override_cap=true stages an over-cap order with the flag persisted.

        When a human explicitly overrides the cap, staging must succeed
        and record ``allow_over_cap=True`` in the pending payload so the
        confirm path can thread it through to OrderService's cap gate.
        """
        cart = _make_cart({"prime rib": 350.00})
        response = _post(
            client,
            auth_headers,
            "/api/v1/order/submit",
            {"cart": cart.model_dump(mode="json"), "override_cap": True},
        )
        assert response.status_code == 200
        action = pending_store.get_pending_action(response.get_json()["action_id"])
        assert action is not None
        assert action.payload["allow_over_cap"] is True

    def test_confirm_order_submit_passes_allow_over_cap_to_pipeline(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
    ) -> None:
        """Test confirm forwards allow_over_cap from the staged payload.

        ``_confirm_order_submit`` must read
        ``action.payload.get("allow_over_cap", False)`` and forward it
        to ``pipeline.submit_cart`` so the OrderService cap gate can be
        overridden by a human who already approved the over-cap total
        at staging time.
        """
        cart = _make_cart({"prime rib": 350.00})
        stage_response = _post(
            client,
            auth_headers,
            "/api/v1/order/submit",
            {"cart": cart.model_dump(mode="json"), "override_cap": True},
        )
        assert stage_response.status_code == 200
        action_id = stage_response.get_json()["action_id"]

        pipeline = MagicMock()
        pipeline.submit_cart.return_value = _successful_order_result()
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = _post(
                client,
                auth_headers,
                "/api/v1/actions/confirm",
                {"action_id": action_id},
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
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
        recipe_store: RecipeStore,
    ) -> None:
        """A valid brand rule is staged, not written to brand preferences."""
        response = _post(client, auth_headers, "/api/v1/brands/set", _brand_body())
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
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """A payload missing required brand fields is rejected."""
        response = _post(
            client, auth_headers, "/api/v1/brands/set", {"brand": "Clover"}
        )
        assert response.status_code == 400
        assert "invalid brand" in response.get_json()["error"]


@patch.dict(os.environ, SECRET_ENV)
class TestPreferencesSetStaging:
    """/preferences/set stages key/value settings without applying them."""

    def test_stages_pending_action(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
        recipe_store: RecipeStore,
    ) -> None:
        """Valid preferences are staged, not written to the store."""
        body = {"preferences": {"fulfillment": "pickup", "store_id": "1234"}}
        response = _post(client, auth_headers, "/api/v1/preferences/set", body)
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
        auth_headers: AuthHeaderFactory,
        body: dict[str, Any],
    ) -> None:
        """Missing, empty, or non-string preference payloads are rejected."""
        response = _post(client, auth_headers, "/api/v1/preferences/set", body)
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# POST /actions/confirm — executes exactly once
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestActionsConfirm:
    """/actions/confirm resolves staged actions with strict semantics."""

    def test_missing_action_id_returns_400(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """A body without action_id is rejected."""
        response = _post(client, auth_headers, "/api/v1/actions/confirm", {})
        assert response.status_code == 400

    def test_unknown_action_id_returns_404(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """An unknown action_id gets a JSON 404."""
        response = _post(
            client,
            auth_headers,
            "/api/v1/actions/confirm",
            {"action_id": str(uuid.uuid4())},
        )
        assert response.status_code == 404
        assert "error" in response.get_json()

    def test_expired_action_returns_410_and_marks_expired(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
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
        response = _post(
            client,
            auth_headers,
            "/api/v1/actions/confirm",
            {"action_id": action_id},
        )
        assert response.status_code == 410
        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.EXPIRED
        assert action.resolved_at is not None

    def test_confirm_order_calls_submit_with_exact_staged_cart(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
    ) -> None:
        """Confirming an order submits the exact staged cart and approves the row."""
        body = _order_body()
        action_id = _stage_via_api(client, auth_headers, "/api/v1/order/submit", body)

        pipeline = MagicMock()
        pipeline.submit_cart.return_value = _successful_order_result()
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = _post(
                client,
                auth_headers,
                "/api/v1/actions/confirm",
                {"action_id": action_id},
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
        auth_headers: AuthHeaderFactory,
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
            response = _post(
                client,
                auth_headers,
                "/api/v1/actions/confirm",
                {"action_id": action_id},
            )

        assert response.status_code == 200
        pipeline.submit_cart.assert_called_once()
        _, kwargs = pipeline.submit_cart.call_args
        assert kwargs.get("allow_review_items") is True

    def test_confirm_order_forwards_allow_unverified_fulfillment_override(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
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
            response = _post(
                client,
                auth_headers,
                "/api/v1/actions/confirm",
                {"action_id": action_id},
            )

        assert response.status_code == 200
        pipeline.submit_cart.assert_called_once()
        _, kwargs = pipeline.submit_cart.call_args
        assert kwargs.get("allow_unverified_fulfillment") is True

    def test_failed_order_submission_returns_502(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
    ) -> None:
        """A failed Safeway submission reports a terse 502 and resolves as failed.

        Issue #75 (W3): a post-claim failure (``result.success is False``)
        must resolve the row as ``failed``, not leave it stuck
        ``approved`` with no record that the submission actually failed.

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
            response = _post(
                client,
                auth_headers,
                "/api/v1/actions/confirm",
                {"action_id": action_id},
            )
        assert response.status_code == 502
        assert response.get_json()["error"] == "order submission failed"
        assert "Safeway is down" not in response.get_data(as_text=True)
        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.FAILED
        assert action.resolved_at is not None

    def test_pipeline_exception_after_claim_marks_action_failed(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
    ) -> None:
        """A SafewayPipelineError raised by submit_cart marks the row failed.

        Distinct from the pre-claim 503/501 paths (which never claim the
        row): once the claim succeeds, ANY post-claim failure -- an
        exception from ``submit_cart`` or a False ``result.success`` --
        must resolve the row as failed, never leave it stuck approved
        (issue #75, W3).
        """
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/order/submit", _order_body()
        )
        pipeline = MagicMock()
        pipeline.submit_cart.side_effect = SafewayPipelineError("network blip")
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = _post(
                client,
                auth_headers,
                "/api/v1/actions/confirm",
                {"action_id": action_id},
            )
        assert response.status_code == 502
        pipeline.close.assert_called_once()
        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.FAILED

    def test_unavailable_pipeline_returns_503_and_keeps_action_pending(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
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
            response = _post(
                client,
                auth_headers,
                "/api/v1/actions/confirm",
                {"action_id": action_id},
            )
        assert response.status_code == 503
        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.PENDING

    def test_disabled_submission_returns_501_and_keeps_action_pending(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
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
            response = _post(
                client,
                auth_headers,
                "/api/v1/actions/confirm",
                {"action_id": action_id},
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
        auth_headers: AuthHeaderFactory,
    ) -> None:
        """Confirming an already-approved action returns 409."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        first = _post(
            client,
            auth_headers,
            "/api/v1/actions/confirm",
            {"action_id": action_id},
        )
        assert first.status_code == 200
        second = _post(
            client,
            auth_headers,
            "/api/v1/actions/confirm",
            {"action_id": action_id},
        )
        assert second.status_code == 409
        assert "error" in second.get_json()

    def test_confirm_after_deny_returns_409(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
    ) -> None:
        """Confirming a denied action returns 409."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        denied = _post(
            client,
            auth_headers,
            "/api/v1/actions/deny",
            {"action_id": action_id},
        )
        assert denied.status_code == 200
        response = _post(
            client,
            auth_headers,
            "/api/v1/actions/confirm",
            {"action_id": action_id},
        )
        assert response.status_code == 409

    def test_confirm_brands_applies_rule(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        recipe_store: RecipeStore,
        pending_store: PendingActionsStore,
    ) -> None:
        """Confirming a brands_set action writes the brand rule."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        response = _post(
            client,
            auth_headers,
            "/api/v1/actions/confirm",
            {"action_id": action_id},
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
        auth_headers: AuthHeaderFactory,
        recipe_store: RecipeStore,
    ) -> None:
        """Confirming a preferences_set action writes every key/value pair."""
        body = {"preferences": {"fulfillment": "pickup", "store_id": "1234"}}
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/preferences/set", body
        )
        response = _post(
            client,
            auth_headers,
            "/api/v1/actions/confirm",
            {"action_id": action_id},
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
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """A body without action_id is rejected."""
        response = _post(client, auth_headers, "/api/v1/actions/deny", {})
        assert response.status_code == 400

    def test_unknown_action_id_returns_404(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """An unknown action_id gets 404."""
        response = _post(
            client,
            auth_headers,
            "/api/v1/actions/deny",
            {"action_id": str(uuid.uuid4())},
        )
        assert response.status_code == 404

    def test_denies_pending_action(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
        recipe_store: RecipeStore,
    ) -> None:
        """Denying marks the row denied and never applies the change."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        response = _post(
            client,
            auth_headers,
            "/api/v1/actions/deny",
            {"action_id": action_id},
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
        auth_headers: AuthHeaderFactory,
    ) -> None:
        """Denying an already-denied action returns 409."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        first = _post(
            client,
            auth_headers,
            "/api/v1/actions/deny",
            {"action_id": action_id},
        )
        assert first.status_code == 200
        second = _post(
            client,
            auth_headers,
            "/api/v1/actions/deny",
            {"action_id": action_id},
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
        auth_headers: AuthHeaderFactory,
    ) -> None:
        """Test the staged action_id is forwarded as the idempotency key."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/order/submit", _order_body()
        )
        pipeline = MagicMock()
        pipeline.submit_cart.return_value = _successful_order_result()
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = _post(
                client,
                auth_headers,
                "/api/v1/actions/confirm",
                {"action_id": action_id},
            )

        assert response.status_code == 200
        pipeline.submit_cart.assert_called_once()
        _args, kwargs = pipeline.submit_cart.call_args
        assert kwargs.get("idempotency_key") == action_id

    def test_unknown_outcome_returns_504(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
    ) -> None:
        """Test an UNKNOWN order outcome surfaces as HTTP 504 with status unknown.

        Issue #75 (W3): UNKNOWN is a ``result.success is False`` outcome,
        so it's a post-claim failure like any other and must resolve the
        row as ``failed``, not leave it stuck ``approved``.

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
            response = _post(
                client,
                auth_headers,
                "/api/v1/actions/confirm",
                {"action_id": action_id},
            )

        assert response.status_code == 504
        data = response.get_json()
        assert data["status"] == "unknown"
        assert data["action_id"] == action_id
        assert "error" in data
        assert "request timed out" not in data["error"]

        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.FAILED

    def test_duplicate_outcome_returns_409(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
    ) -> None:
        """Test a DUPLICATE order outcome surfaces as HTTP 409 duplicate_prevented.

        Issue #75 (W3): DUPLICATE is also a ``result.success is False``
        outcome, so it too must resolve the row as ``failed``.

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
            response = _post(
                client,
                auth_headers,
                "/api/v1/actions/confirm",
                {"action_id": action_id},
            )

        assert response.status_code == 409
        data = response.get_json()
        assert data["status"] == "duplicate_prevented"
        assert data["action_id"] == action_id
        assert "error" in data
        assert "Duplicate order blocked" not in data["error"]

        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.FAILED


# ---------------------------------------------------------------------------
# W6 (issue #75): a raced/duplicate confirm must still close the pipeline
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestPipelineLeakOnRace:
    """A raced/duplicate confirm must still close the Safeway pipeline (W6).

    ``post_actions_confirm`` reads the action once, then the executor
    builds a Safeway pipeline before atomically claiming the row. If a
    concurrent request already resolved the row by the time the claim
    runs, the claim fails (409) -- but the pipeline that was already
    constructed for THIS request must still be closed, or every raced
    confirm leaks a client/session (issue #75).
    """

    def test_claim_race_still_closes_pipeline_before_409(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
    ) -> None:
        """A claim that loses the race closes the already-built pipeline."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/order/submit", _order_body()
        )
        pipeline = MagicMock()
        with (
            patch("grocery_butler.api._safeway_pipeline", return_value=pipeline),
            patch.object(
                PendingActionsStore, "mark_pending_approved", return_value=False
            ),
        ):
            response = _post(
                client,
                auth_headers,
                "/api/v1/actions/confirm",
                {"action_id": action_id},
            )
        assert response.status_code == 409
        pipeline.close.assert_called_once()


# ---------------------------------------------------------------------------
# W5 (issue #75): preferences confirmation must be all-or-nothing
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestPreferencesAtomicity:
    """Confirming preferences_set must be all-or-nothing (W5, issue #75).

    ``_confirm_preferences_set`` must use a single connection / one
    commit for every staged key (``RecipeStore.set_preferences``)
    instead of the old per-key loop, so a mid-way failure leaves the
    store completely untouched rather than half-applied.
    """

    def test_mid_write_failure_leaves_no_keys_applied(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        recipe_store: RecipeStore,
    ) -> None:
        """A failure applying the second key rolls back the first too."""
        body = {"preferences": {"fulfillment": "pickup", "store_id": "1234"}}
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/preferences/set", body
        )

        class _FailSecondWrite:
            """Connection proxy that fails starting on its second execute()."""

            def __init__(self, real_conn: Any) -> None:
                self._real = real_conn
                self._calls = 0

            def execute(self, sql: str, params: Any = ()) -> Any:
                self._calls += 1
                if self._calls > 1:
                    raise RuntimeError("simulated mid-transaction failure")
                return self._real.execute(sql, params)

            def executescript(self, sql: str) -> None:
                self._real.executescript(sql)

            def commit(self) -> None:
                self._real.commit()

            def close(self) -> None:
                self._real.close()

        def _fake_get_connection(path: str) -> _FailSecondWrite:
            return _FailSecondWrite(get_connection(path))

        with (
            patch(
                "grocery_butler.recipe_store.get_connection",
                side_effect=_fake_get_connection,
            ),
            pytest.raises(RuntimeError, match="simulated mid-transaction failure"),
        ):
            _post(
                client,
                auth_headers,
                "/api/v1/actions/confirm",
                {"action_id": action_id},
            )

        stored = recipe_store.get_all_preferences()
        assert "fulfillment" not in stored
        assert "store_id" not in stored


# ---------------------------------------------------------------------------
# W4 (issue #75): GET /api/v1/actions and GET /api/v1/actions/<action_id>
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestListActionsEndpoint:
    """GET /api/v1/actions -- paginated, filterable audit-trail read (W4).

    Issue #75: read access to the pending_actions audit log so an
    operator (or RubotPaul) can see what's staged, resolved, or expired
    without querying the database directly.
    """

    def test_missing_bearer_returns_401(self, client: FlaskClient) -> None:
        """Unauthenticated requests are rejected."""
        response = client.get("/api/v1/actions")
        assert response.status_code == 401

    def test_returns_staged_actions_with_count(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """Staged actions show up in the list with a matching count."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        response = _get(client, auth_headers, "/api/v1/actions")
        assert response.status_code == 200
        data = response.get_json()
        assert "actions" in data
        assert "count" in data
        assert data["count"] == len(data["actions"])
        ids = [a["action_id"] for a in data["actions"]]
        assert action_id in ids

    def test_sweeps_expired_actions_before_listing(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
    ) -> None:
        """A past-due pending row shows up as expired, not pending."""
        action_id = str(uuid.uuid4())
        pending_store.insert_pending_action(
            action_id=action_id,
            kind="brands_set",
            payload={},
            expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
        )
        response = _get(client, auth_headers, "/api/v1/actions")
        assert response.status_code == 200
        matching = [
            a for a in response.get_json()["actions"] if a["action_id"] == action_id
        ]
        assert matching
        assert matching[0]["status"] == "expired"

    def test_default_limit_is_50(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """Omitting ?limit uses a default of 50."""
        with patch.object(
            PendingActionsStore, "list_pending_actions", return_value=[]
        ) as mock_list:
            response = _get(client, auth_headers, "/api/v1/actions")
        assert response.status_code == 200
        assert mock_list.call_args.kwargs.get("limit") == 50

    def test_limit_over_max_is_clamped_not_rejected(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """A ?limit above the hard cap is clamped to 200, not a 400."""
        with patch.object(
            PendingActionsStore, "list_pending_actions", return_value=[]
        ) as mock_list:
            response = _get(client, auth_headers, "/api/v1/actions", "?limit=99999")
        assert response.status_code == 200
        assert mock_list.call_args.kwargs.get("limit") == 200

    def test_status_filter_returns_only_matching_actions(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
    ) -> None:
        """?status= filters the returned actions by that status."""
        denied_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        _post(client, auth_headers, "/api/v1/actions/deny", {"action_id": denied_id})
        _stage_via_api(client, auth_headers, "/api/v1/brands/set", _brand_body())

        response = _get(client, auth_headers, "/api/v1/actions", "?status=denied")
        assert response.status_code == 200
        data = response.get_json()
        assert all(a["status"] == "denied" for a in data["actions"])
        assert denied_id in [a["action_id"] for a in data["actions"]]

    def test_unknown_status_value_returns_400(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """An unrecognized ?status value is rejected, not silently ignored."""
        response = _get(
            client, auth_headers, "/api/v1/actions", "?status=not_a_real_status"
        )
        assert response.status_code == 400


@patch.dict(os.environ, SECRET_ENV)
class TestGetActionEndpoint:
    """GET /api/v1/actions/<action_id> -- single-row audit-trail read (W4)."""

    def test_missing_bearer_returns_401(self, client: FlaskClient) -> None:
        """Unauthenticated requests are rejected."""
        response = client.get(f"/api/v1/actions/{uuid.uuid4()}")
        assert response.status_code == 401

    def test_returns_serialized_action(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """A known action_id returns its full serialized row."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        response = _get(client, auth_headers, f"/api/v1/actions/{action_id}")
        assert response.status_code == 200
        data = response.get_json()
        assert data["action_id"] == action_id
        assert data["kind"] == "brands_set"
        assert data["status"] == "pending"

    def test_unknown_action_id_returns_404(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """An unknown action_id gets a JSON 404."""
        response = _get(client, auth_headers, f"/api/v1/actions/{uuid.uuid4()}")
        assert response.status_code == 404
        assert "error" in response.get_json()


# ---------------------------------------------------------------------------
# W4 (issue #75): staging endpoints also sweep past-due pending rows first
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestStagingSweepsExpiredActions:
    """Staging a new action sweeps expired rows too, not just GET /actions.

    Issue #75 (W4) requires the lazy sweep to run wherever a caller
    touches the pending_actions table, not only on read: otherwise a
    past-due row can sit ``pending`` forever if the audit log is never
    listed. Each of the three staging routes must trigger the sweep as
    a side effect of inserting its own new row.
    """

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("/api/v1/order/submit", _order_body()),
            ("/api/v1/brands/set", _brand_body()),
            ("/api/v1/preferences/set", {"preferences": {"fulfillment": "pickup"}}),
        ],
        ids=["order_submit", "brands_set", "preferences_set"],
    )
    def test_staging_sweeps_unrelated_past_due_row(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
        path: str,
        body: dict[str, Any],
    ) -> None:
        """An unrelated past-due pending row flips to expired on staging."""
        stale_id = str(uuid.uuid4())
        pending_store.insert_pending_action(
            action_id=stale_id,
            kind="brands_set",
            payload={},
            expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
        )
        response = _post(client, auth_headers, path, body)
        assert response.status_code == 200

        stale = pending_store.get_pending_action(stale_id)
        assert stale is not None
        assert stale.status is PendingActionStatus.EXPIRED
        assert stale.resolved_at is not None
        assert stale.resolver is None


# ---------------------------------------------------------------------------
# Issue #74 AC#1: a token bound to one endpoint cannot be replayed on another
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestCrossEndpointTokenReplayRejected:
    """A token minted for a harmless read endpoint cannot confirm actions."""

    def test_inventory_token_replayed_against_confirm_returns_401(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
    ) -> None:
        """AC#1: a GET /inventory token posted to /actions/confirm is rejected.

        Stages a real action with a correctly-bound token, then attempts
        to confirm it with a token minted for an unrelated read
        endpoint. The replay must be rejected, and the staged action
        must remain untouched -- no action may execute as a side effect
        of a mismatched token.
        """
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        stolen_headers = auth_headers("GET", "/api/v1/inventory", b"")

        response = client.post(
            "/api/v1/actions/confirm",
            data=json.dumps({"action_id": action_id}).encode(),
            content_type="application/json",
            headers=stolen_headers,
        )

        assert response.status_code == 401
        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.PENDING


# ---------------------------------------------------------------------------
# Issue #74 AC#2: auth-by-default via the blueprint's before_request hook
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestAuthByDefault:
    """The api_v1 blueprint enforces auth even without a per-route call.

    ``_unauthenticated_throwaway_view`` (module scope, above) never
    calls ``require_bearer()`` itself. If the blueprint's
    ``before_request`` hook is doing its job, unauthenticated requests
    to it must still 401, and correctly-bound requests must still 200.
    """

    def test_unauthenticated_request_to_unguarded_view_returns_401(
        self, db_path: str
    ) -> None:
        """An unauthenticated request to the unguarded view is rejected.

        Args:
            db_path: Temporary database path fixture for test isolation.
        """
        application = create_app(db_path=db_path)
        application.config["TESTING"] = True
        test_client = application.test_client()

        response = test_client.get(UNAUTHENTICATED_THROWAWAY_PATH)

        assert response.status_code == 401

    def test_bound_token_for_unguarded_view_returns_200(self, db_path: str) -> None:
        """A correctly bound token still reaches the unguarded view.

        Args:
            db_path: Temporary database path fixture for test isolation.
        """
        application = create_app(db_path=db_path)
        application.config["TESTING"] = True
        test_client = application.test_client()
        headers = bearer_header("rubotpaul", "GET", UNAUTHENTICATED_THROWAWAY_PATH)

        response = test_client.get(UNAUTHENTICATED_THROWAWAY_PATH, headers=headers)

        assert response.status_code == 200
        assert response.get_json() == {"ok": True}

    def test_caller_id_helper_without_auth_context_aborts_401(self, app: Flask) -> None:
        """_request_caller_id aborts 401 when the hook never bound a caller.

        Guards the defensive branch for a future auth-exempt endpoint
        mistakenly asking for a caller identity it never authenticated:
        outside the hook, no caller_id is bound to ``flask.g``, so the
        helper must refuse rather than fabricate an identity.

        Args:
            app: Flask test app fixture (provides a request context).
        """
        from werkzeug.exceptions import Unauthorized

        from grocery_butler.api import _request_caller_id

        with (
            app.test_request_context("/api/v1/actions"),
            pytest.raises(Unauthorized),
        ):
            _request_caller_id()
