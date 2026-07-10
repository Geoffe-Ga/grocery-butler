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


def _make_cart_item(ingredient: str, cost: float) -> CartItem:
    """Return a CartItem with the given estimated cost."""
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

    def test_failed_order_submission_returns_502(
        self,
        client: FlaskClient,
        auth_headers: dict[str, str],
        pending_store: PendingActionsStore,
    ) -> None:
        """A failed Safeway submission reports 502; the claim is consumed."""
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
        assert "Safeway is down" in response.get_json()["error"]
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
