"""Tests for the /api/v1 error contract (Issue #77).

Pins down that ``/api/v1`` never leaks an HTML error page or internal
exception detail to a JSON client: unknown paths, wrong HTTP methods,
and uncaught exceptions must all render terse JSON, and the
sanitized-but-mapped error sites in ``grocery_butler.api`` (Safeway
pipeline / order-value cap / order submission failures) must never
echo the underlying exception text or config value into the response
body. Every scenario here is RED against today's implementation and
is expected to go GREEN once the error contract is fixed.
"""

from __future__ import annotations

import os
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
    CartItem,
    CartSummary,
    FulfillmentType,
    IngredientCategory,
    SafewayProduct,
    ShoppingListItem,
    Unit,
)
from grocery_butler.order_service import OrderOutcome, OrderResult
from grocery_butler.safeway_pipeline import SafewayPipelineError

TEST_SECRET = "test-shared-secret"
SECRET_ENV = {SECRET_ENV_VAR: TEST_SECRET}

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a temporary database path for test isolation."""
    return str(tmp_path / "test_api_errors.db")


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
def auth_headers() -> dict[str, str]:
    """Return a valid Authorization header minted with the test secret."""
    with patch.dict(os.environ, SECRET_ENV):
        token = mint_token("rubotpaul")
    return {"Authorization": f"Bearer {token}"}


def _make_shopping_item(ingredient: str = "pasta") -> ShoppingListItem:
    """Return a ShoppingListItem for order preview/submit payloads."""
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


def _order_body() -> dict[str, Any]:
    """Return a valid /order/submit request body totalling $7.75."""
    cart = _make_cart({"pasta": 3.50, "sauce": 4.25})
    return {"cart": cart.model_dump(mode="json"), "total": "7.75"}


def _stage_via_api(
    client: FlaskClient, auth_headers: dict[str, str], body: dict[str, Any]
) -> str:
    """Stage an order through /order/submit and return its action_id."""
    response = client.post("/api/v1/order/submit", json=body, headers=auth_headers)
    assert response.status_code == 200
    action_id = response.get_json()["action_id"]
    assert isinstance(action_id, str)
    return action_id


# ---------------------------------------------------------------------------
# Unknown paths under /api/v1 must render JSON, not Flask's HTML 404 page
# ---------------------------------------------------------------------------


class TestUnknownPathContract:
    """GET on an unmatched /api/v1 path must return a terse JSON 404."""

    def test_unknown_path_returns_json_404_not_html(self, client: FlaskClient) -> None:
        """An unmatched /api/v1 path returns JSON, never Flask's HTML page.

        Today Flask cannot associate an unmatched URL with any
        blueprint's error handlers, so it falls through to the app-level
        404 handler in grocery_butler.app, which renders error.html —
        an HTML page to a JSON-only API client.
        """
        response = client.get("/api/v1/nonexistent")
        assert response.status_code == 404
        assert response.is_json
        assert response.get_json() == {"error": "not found"}
        assert response.content_type.startswith("application/json")
        assert "<!doctype html" not in response.get_data(as_text=True).lower()


# ---------------------------------------------------------------------------
# Wrong HTTP method on a known /api/v1 route must render JSON, not HTML
# ---------------------------------------------------------------------------


class TestMethodNotAllowedContract:
    """A wrong HTTP method on a known /api/v1 route must return JSON 405."""

    def test_wrong_method_returns_json_405_not_html(self, client: FlaskClient) -> None:
        """DELETE on the GET-only /api/v1/inventory route returns JSON 405.

        No 405 handler is registered on the blueprint today (only 400,
        401, 404, 409, 410 are), so werkzeug's default HTML method-not-
        allowed page leaks through instead.
        """
        response = client.delete("/api/v1/inventory")
        assert response.status_code == 405
        assert response.is_json
        assert response.get_json() == {"error": "method not allowed"}
        assert "<!doctype html" not in response.get_data(as_text=True).lower()


# ---------------------------------------------------------------------------
# Uncaught exceptions inside a route must render JSON, not HTML, and must
# never leak the exception text
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestInternalServerErrorContract:
    """Uncaught exceptions inside /api/v1 routes render a terse JSON 500."""

    def test_uncaught_exception_returns_json_500_without_leaking_text(
        self,
        client: FlaskClient,
        app: Flask,
        auth_headers: dict[str, str],
    ) -> None:
        """A RuntimeError inside the route body becomes a generic JSON 500.

        TESTING=True makes Flask propagate exceptions by default, which
        would bypass any 500 handler and hide this bug entirely — so
        PROPAGATE_EXCEPTIONS is explicitly disabled here to exercise the
        request pipeline the way it behaves in production. The injected
        RuntimeError carries a secret-shaped marker to prove the
        eventual fix's generic body doesn't echo it back.
        """
        app.config["PROPAGATE_EXCEPTIONS"] = False
        with patch(
            "grocery_butler.api.PantryManager.get_inventory",
            side_effect=RuntimeError("ANTHROPIC_API_KEY=sk-secret-marker"),
        ):
            response = client.get("/api/v1/inventory", headers=auth_headers)
        assert response.status_code == 500
        assert response.is_json
        assert response.get_json() == {"error": "internal server error"}
        body_text = response.get_data(as_text=True)
        assert "sk-secret" not in body_text
        assert "ANTHROPIC_API_KEY" not in body_text
        assert "<!doctype html" not in body_text.lower()


# ---------------------------------------------------------------------------
# Leak-scrub: the sanitized 503/502/504 sites in grocery_butler/api.py must
# use terse, stable messages and must never echo exception/config detail
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestLeakScrubbedErrorBodies:
    """503/502/504 responses must be terse and free of internal detail."""

    def test_order_preview_pipeline_configerror_is_scrubbed(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """A ConfigError building the pipeline for /order/preview is scrubbed.

        Today's handler interpolates the raw exception into the 503
        body (``f"safeway pipeline unavailable: {exc}"``), so
        ``ConfigError``'s message — which can name missing env var
        details — reaches the client verbatim.
        """
        with patch(
            "grocery_butler.api._safeway_pipeline",
            side_effect=ConfigError("SECRET_MARKER_ENV_DETAIL"),
        ):
            response = client.post(
                "/api/v1/order/preview",
                json={"shopping_list": [_make_shopping_item().model_dump(mode="json")]},
                headers=auth_headers,
            )
        assert response.status_code == 503
        assert response.get_json()["error"] == "safeway pipeline unavailable"
        assert "SECRET_MARKER_ENV_DETAIL" not in response.get_data(as_text=True)

    def test_order_preview_cart_build_error_is_scrubbed(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """A SafewayPipelineError building the cart for /order/preview is scrubbed.

        Today's handler interpolates the raw exception into the 503
        body (``f"cart build failed: {exc}"``).
        """
        pipeline = MagicMock()
        pipeline.build_cart_only.side_effect = SafewayPipelineError(
            "SECRET_MARKER_AUTH"
        )
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = client.post(
                "/api/v1/order/preview",
                json={"shopping_list": [_make_shopping_item().model_dump(mode="json")]},
                headers=auth_headers,
            )
        assert response.status_code == 503
        assert response.get_json()["error"] == "cart build failed"
        assert "SECRET_MARKER_AUTH" not in response.get_data(as_text=True)

    def test_order_submit_invalid_cap_config_is_scrubbed(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """An unparseable order-value cap env var yields a terse 503.

        Today's handler interpolates the raw ``ConfigError`` into the
        503 body (``f"order-value cap configuration invalid: {exc}"``),
        which names the offending env var and its invalid raw value.
        """
        with patch.dict(os.environ, {"SAFEWAY_ORDER_VALUE_CAP_USD": "not-a-number"}):
            response = client.post(
                "/api/v1/order/submit", json=_order_body(), headers=auth_headers
            )
        assert response.status_code == 503
        assert response.get_json()["error"] == "order configuration invalid"
        text = response.get_data(as_text=True)
        assert "SAFEWAY_ORDER_VALUE_CAP_USD" not in text
        assert "not-a-number" not in text

    def test_confirm_pipeline_configerror_is_scrubbed(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """ConfigError building the pipeline at confirm time is scrubbed."""
        action_id = _stage_via_api(client, auth_headers, _order_body())
        with patch(
            "grocery_butler.api._safeway_pipeline",
            side_effect=ConfigError("SECRET_MARKER_CONF"),
        ):
            response = client.post(
                "/api/v1/actions/confirm",
                json={"action_id": action_id},
                headers=auth_headers,
            )
        assert response.status_code == 503
        assert response.get_json()["error"] == "safeway pipeline unavailable"
        assert "SECRET_MARKER_CONF" not in response.get_data(as_text=True)

    def test_confirm_submit_cart_raises_is_scrubbed(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """A SafewayPipelineError raised by submit_cart is scrubbed.

        Today's handler interpolates the raw exception into the 502
        body (``f"order submission failed: {exc}"``).
        """
        action_id = _stage_via_api(client, auth_headers, _order_body())
        pipeline = MagicMock()
        pipeline.submit_cart.side_effect = SafewayPipelineError("SECRET_MARKER_SUBMIT")
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = client.post(
                "/api/v1/actions/confirm",
                json={"action_id": action_id},
                headers=auth_headers,
            )
        assert response.status_code == 502
        assert response.get_json()["error"] == "order submission failed"
        assert "SECRET_MARKER_SUBMIT" not in response.get_data(as_text=True)

    def test_confirm_unmapped_failure_result_is_scrubbed(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """An unmapped-outcome OrderResult failure (no ``outcome``) is scrubbed.

        ``_order_failure_response`` relays ``result.error_message``
        verbatim into the 502 body for any outcome not in
        ``_OUTCOME_RESPONSES`` — which can include raw text surfaced
        from deep inside the Safeway client.
        """
        action_id = _stage_via_api(client, auth_headers, _order_body())
        pipeline = MagicMock()
        pipeline.submit_cart.return_value = OrderResult(
            success=False, error_message="SECRET_MARKER_RAW_SAFEWAY"
        )
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = client.post(
                "/api/v1/actions/confirm",
                json={"action_id": action_id},
                headers=auth_headers,
            )
        assert response.status_code == 502
        assert response.get_json()["error"] == "order submission failed"
        assert "SECRET_MARKER_RAW_SAFEWAY" not in response.get_data(as_text=True)

    def test_confirm_mapped_outcome_preserves_status_but_scrubs_text(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """A mapped OrderOutcome keeps status/action_id but scrubs free text.

        ``_order_failure_response`` maps ``OrderOutcome.UNKNOWN`` to a
        504 with ``status="unknown"``, but today still relays
        ``result.error_message`` verbatim as the ``error`` field. The
        fixed contract must keep the mapped status code and the
        ``status``/``action_id`` keys (RubotPaul needs those to route
        the reply) while replacing the free text with something terse.
        """
        action_id = _stage_via_api(client, auth_headers, _order_body())
        pipeline = MagicMock()
        pipeline.submit_cart.return_value = OrderResult(
            success=False,
            outcome=OrderOutcome.UNKNOWN,
            error_message="SECRET_MARKER_TIMEOUT_DETAIL",
        )
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = client.post(
                "/api/v1/actions/confirm",
                json={"action_id": action_id},
                headers=auth_headers,
            )
        assert response.status_code == 504
        body = response.get_json()
        assert body["status"] == "unknown"
        assert body["action_id"] == action_id
        assert "SECRET_MARKER_TIMEOUT_DETAIL" not in response.get_data(as_text=True)


# ---------------------------------------------------------------------------
# Regression guards: behavior that is already correct and must not regress
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestRegressionGuards:
    """Already-correct /api/v1 behavior that the #77 fix must not break."""

    def test_recipe_not_found_still_uses_blueprint_json_404(
        self, client: FlaskClient, auth_headers: dict[str, str]
    ) -> None:
        """An in-route abort(404) still renders the blueprint's descriptive JSON.

        Distinct from the unmatched-path case above: this hits a real
        route whose handler explicitly aborts 404 for an unknown id, so
        the blueprint's existing ``@api_v1.errorhandler(404)`` already
        renders JSON correctly and must keep doing so.
        """
        response = client.get("/api/v1/recipes/999999", headers=auth_headers)
        assert response.status_code == 404
        assert response.get_json() == {"error": "recipe not found"}

    def test_missing_bearer_on_inventory_still_returns_json_401(
        self, client: FlaskClient
    ) -> None:
        """No Authorization header on a real route still returns JSON 401."""
        response = client.get("/api/v1/inventory")
        assert response.status_code == 401
        assert response.is_json
