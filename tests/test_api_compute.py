"""Tests for the /api/v1 compute endpoints (grocery_butler.api blueprint).

Covers POST /api/v1/meals/parse, /api/v1/shopping-list/preview, and
/api/v1/order/preview. The Claude / Safeway pipelines are mocked — no
live API calls happen here.

Issue #74: every bearer token minted in this module is now bound to the
exact (method, path, body) of the request it authorizes, via the shared
``tests.conftest.bearer_header`` helper and the ``_post`` helper below,
which serializes each JSON body exactly once and binds the token to
those same bytes.
"""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient

    AuthHeaderFactory = Callable[[str, str, bytes], dict[str, str]]

from grocery_butler.app import create_app
from grocery_butler.auth_middleware import SECRET_ENV_VAR
from grocery_butler.models import (
    CartItem,
    CartSummary,
    FulfillmentOption,
    FulfillmentType,
    Ingredient,
    IngredientCategory,
    InventoryItem,
    InventoryStatus,
    ParsedMeal,
    SafewayProduct,
    ShoppingListItem,
    Unit,
)
from grocery_butler.pantry_manager import PantryManager
from grocery_butler.recipe_store import RecipeStore
from grocery_butler.safeway_pipeline import SafewayPipelineError
from tests.conftest import bearer_header

TEST_SECRET = "test-shared-secret"
SECRET_ENV = {SECRET_ENV_VAR: TEST_SECRET}

COMPUTE_ENDPOINTS = [
    "/api/v1/meals/parse",
    "/api/v1/shopping-list/preview",
    "/api/v1/order/preview",
]

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a temporary database path for test isolation."""
    return str(tmp_path / "test_api_compute.db")


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
def pantry_mgr(db_path: str) -> PantryManager:
    """Return a PantryManager bound to the test database."""
    return PantryManager(db_path)


@pytest.fixture()
def recipe_store(db_path: str) -> RecipeStore:
    """Return a RecipeStore bound to the test database."""
    return RecipeStore(db_path)


@pytest.fixture()
def auth_headers() -> AuthHeaderFactory:
    """Return a factory that mints a bearer header for one exact request.

    A single static header cannot authorize every request exercised in
    this module -- each compute endpoint call has its own body, and the
    request-bound token contract (issue #74) signs method, path, and
    body together. Callers invoke the returned factory once per
    request, typically via the ``_post`` helper below.

    Returns:
        A callable ``(method, path, body=b"") -> {"Authorization": ...}``
        that mints tokens under the test shared secret.
    """

    def _make(method: str, path: str, body: bytes = b"") -> dict[str, str]:
        """Mint a bearer header bound to the given method/path/body."""
        with patch.dict(os.environ, SECRET_ENV):
            return bearer_header("rubotpaul", method, path, body)

    return _make


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


def _make_meal(name: str = "test pasta") -> ParsedMeal:
    """Return a ParsedMeal with one purchase item."""
    return ParsedMeal(
        name=name,
        servings=4,
        known_recipe=True,
        needs_confirmation=False,
        purchase_items=[
            Ingredient(
                ingredient="pasta",
                quantity=1.0,
                unit="lb",
                category=IngredientCategory.PANTRY_DRY,
            ),
        ],
        pantry_items=[],
    )


def _make_shopping_item(ingredient: str = "pasta") -> ShoppingListItem:
    """Return a ShoppingListItem for order previews."""
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


# ---------------------------------------------------------------------------
# Auth: every compute endpoint rejects unauthenticated requests, with JSON
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestAuthRequired:
    """All compute endpoints must reject requests without a valid bearer."""

    @pytest.mark.parametrize("path", COMPUTE_ENDPOINTS)
    def test_missing_bearer_returns_401(self, client: FlaskClient, path: str) -> None:
        """Requests without an Authorization header get 401."""
        response = client.post(path, json={})
        assert response.status_code == 401

    @pytest.mark.parametrize("path", COMPUTE_ENDPOINTS)
    def test_garbage_bearer_returns_401(self, client: FlaskClient, path: str) -> None:
        """Requests with an invalid token get 401."""
        response = client.post(
            path, json={}, headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert response.status_code == 401

    @pytest.mark.parametrize("path", COMPUTE_ENDPOINTS)
    def test_401_body_is_json(self, client: FlaskClient, path: str) -> None:
        """API auth failures return a JSON error body, not HTML."""
        response = client.post(path, json={})
        assert response.content_type.startswith("application/json")
        assert "error" in response.get_json()


# ---------------------------------------------------------------------------
# POST /api/v1/meals/parse
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestMealsParse:
    """POST /api/v1/meals/parse."""

    @pytest.mark.parametrize("body", [{}, {"text": ""}, {"text": "   "}])
    def test_empty_text_returns_400(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory, body: dict[str, str]
    ) -> None:
        """Missing or blank text is a 400 with a JSON error."""
        response = _post(client, auth_headers, "/api/v1/meals/parse", body)
        assert response.status_code == 400
        assert "error" in response.get_json()

    def test_non_object_body_returns_400(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """A non-object JSON body is a 400."""
        response = _post(client, auth_headers, "/api/v1/meals/parse", ["tacos"])
        assert response.status_code == 400

    def test_parses_comma_and_newline_separated_meals(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """Meal names split on commas and newlines reach parse_meals."""
        parser = MagicMock()
        parser.parse_meals.return_value = [_make_meal("tacos"), _make_meal("pasta")]
        with patch("grocery_butler.api._meal_parser", return_value=parser):
            response = _post(
                client,
                auth_headers,
                "/api/v1/meals/parse",
                {"text": "tacos, pasta\nchili"},
            )
        assert response.status_code == 200
        parser.parse_meals.assert_called_once_with(["tacos", "pasta", "chili"])

    def test_serializes_parsed_meals(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """The parsed meals come back JSON-serialized."""
        parser = MagicMock()
        parser.parse_meals.return_value = [_make_meal("tacos")]
        with patch("grocery_butler.api._meal_parser", return_value=parser):
            response = _post(
                client, auth_headers, "/api/v1/meals/parse", {"text": "tacos"}
            )
        assert response.status_code == 200
        meals = response.get_json()["meals"]
        assert len(meals) == 1
        assert meals[0]["name"] == "tacos"
        assert meals[0]["purchase_items"][0]["ingredient"] == "pasta"


# ---------------------------------------------------------------------------
# POST /api/v1/shopping-list/preview
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestShoppingListPreview:
    """POST /api/v1/shopping-list/preview."""

    def test_invalid_meals_returns_400(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """Meals that fail ParsedMeal validation are a 400."""
        response = _post(
            client,
            auth_headers,
            "/api/v1/shopping-list/preview",
            {"meals": [{"name": "incomplete"}]},
        )
        assert response.status_code == 400
        assert "error" in response.get_json()

    def test_meals_must_be_a_list(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """A non-list meals field is a 400."""
        response = _post(
            client, auth_headers, "/api/v1/shopping-list/preview", {"meals": "tacos"}
        )
        assert response.status_code == 400

    def test_consolidates_with_restock_and_staples(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pantry_mgr: PantryManager,
        recipe_store: RecipeStore,
    ) -> None:
        """The consolidator gets parsed meals, restock queue, and staples."""
        pantry_mgr.add_item(
            InventoryItem(
                ingredient="milk",
                display_name="Milk",
                category=IngredientCategory.DAIRY,
                status=InventoryStatus.ON_HAND,
            )
        )
        pantry_mgr.update_status("milk", InventoryStatus.OUT)
        recipe_store.add_pantry_staple("saffron", IngredientCategory.PANTRY_DRY.value)

        consolidator = MagicMock()
        consolidator.consolidate.return_value = [_make_shopping_item()]
        meal = _make_meal("tacos")
        with patch("grocery_butler.api._consolidator", return_value=consolidator):
            response = _post(
                client,
                auth_headers,
                "/api/v1/shopping-list/preview",
                {"meals": [meal.model_dump(mode="json")]},
            )

        assert response.status_code == 200
        kwargs = consolidator.consolidate.call_args.kwargs
        assert kwargs["meals"] == [meal]
        assert [i.ingredient for i in kwargs["restock_queue"]] == ["milk"]
        assert "saffron" in kwargs["pantry_staples"]
        items = response.get_json()["items"]
        assert len(items) == 1
        assert items[0]["ingredient"] == "pasta"

    def test_include_restock_false_skips_restock_queue(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pantry_mgr: PantryManager,
    ) -> None:
        """include_restock=false keeps the restock queue out of the call."""
        pantry_mgr.add_item(
            InventoryItem(
                ingredient="milk",
                display_name="Milk",
                category=IngredientCategory.DAIRY,
                status=InventoryStatus.ON_HAND,
            )
        )
        pantry_mgr.update_status("milk", InventoryStatus.OUT)

        consolidator = MagicMock()
        consolidator.consolidate.return_value = []
        with patch("grocery_butler.api._consolidator", return_value=consolidator):
            response = _post(
                client,
                auth_headers,
                "/api/v1/shopping-list/preview",
                {
                    "meals": [_make_meal().model_dump(mode="json")],
                    "include_restock": False,
                },
            )

        assert response.status_code == 200
        assert consolidator.consolidate.call_args.kwargs["restock_queue"] == []


# ---------------------------------------------------------------------------
# POST /api/v1/order/preview
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestOrderPreview:
    """POST /api/v1/order/preview."""

    @pytest.mark.parametrize("body", [{}, {"shopping_list": []}])
    def test_empty_shopping_list_returns_400(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        body: dict[str, object],
    ) -> None:
        """A missing or empty shopping_list is a 400."""
        response = _post(client, auth_headers, "/api/v1/order/preview", body)
        assert response.status_code == 400
        assert "error" in response.get_json()

    def test_invalid_shopping_list_item_returns_400(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """Items that fail ShoppingListItem validation are a 400."""
        response = _post(
            client,
            auth_headers,
            "/api/v1/order/preview",
            {"shopping_list": [{"ingredient": "pasta"}]},
        )
        assert response.status_code == 400

    def test_builds_cart_and_returns_decimal_safe_total(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """0.1 + 0.2 comes back as exactly "0.30", not a float artifact.

        Issue #73: totals are now server-computed via
        ``order_service.compute_cart_total``, which quantizes monetary
        values to cents — hence "0.30" rather than the bare "0.3".
        """
        pipeline = MagicMock()
        pipeline.build_cart_only.return_value = _make_cart({"pasta": 0.1, "beans": 0.2})
        item = _make_shopping_item()
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = _post(
                client,
                auth_headers,
                "/api/v1/order/preview",
                {"shopping_list": [item.model_dump(mode="json")]},
            )

        assert response.status_code == 200
        pipeline.build_cart_only.assert_called_once_with([item])
        pipeline.close.assert_called_once_with()
        body = response.get_json()
        assert body["total"] == "0.30"
        assert len(body["cart"]["items"]) == 2

    def test_restock_items_count_toward_total(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """Restock cart items are part of the Decimal-safe total."""
        cart = _make_cart({"pasta": 1.25})
        cart = cart.model_copy(
            update={"restock_items": [_make_cart_item("milk", 2.50)]}
        )
        pipeline = MagicMock()
        pipeline.build_cart_only.return_value = cart
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = _post(
                client,
                auth_headers,
                "/api/v1/order/preview",
                {"shopping_list": [_make_shopping_item().model_dump(mode="json")]},
            )

        assert response.status_code == 200
        assert response.get_json()["total"] == "3.75"

    def test_failed_items_are_serialized_in_cart_response(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """A cart with failed_items still serializes as JSON (issue #76).

        Serialization guard: when CartBuilder routes an item to
        ``failed_items`` (e.g. after a per-item ``ProductSearchError``),
        the ``/order/preview`` response must still come back as a clean
        200 with the failed item present in ``cart.failed_items`` --
        not blow up or silently drop it.
        """
        cart = _make_cart({"pasta": 1.0})
        failed_item = _make_shopping_item("eggs")
        cart = cart.model_copy(update={"failed_items": [failed_item]})
        pipeline = MagicMock()
        pipeline.build_cart_only.return_value = cart
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = _post(
                client,
                auth_headers,
                "/api/v1/order/preview",
                {"shopping_list": [_make_shopping_item().model_dump(mode="json")]},
            )

        assert response.status_code == 200
        failed_items = response.get_json()["cart"]["failed_items"]
        assert len(failed_items) == 1
        assert failed_items[0]["ingredient"] == "eggs"

    def test_order_preview_total_includes_fulfillment_fee(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """Test the preview total includes the recommended fulfillment fee.

        Issue #73: /order/preview computes its total via the same
        fee-omitting ``_cart_total`` helper as /order/submit. A cart
        with a $4.00 recommended-fulfillment fee on top of a $1.25 item
        must preview a $5.25 total, not $1.25.
        """
        cart = _make_cart({"pasta": 1.25})
        cart = cart.model_copy(
            update={
                "fulfillment_options": [
                    FulfillmentOption(
                        type=FulfillmentType.PICKUP,
                        available=True,
                        fee=4.00,
                        windows=[],
                    ),
                ],
                "recommended_fulfillment": FulfillmentType.PICKUP,
            }
        )
        pipeline = MagicMock()
        pipeline.build_cart_only.return_value = cart
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = _post(
                client,
                auth_headers,
                "/api/v1/order/preview",
                {"shopping_list": [_make_shopping_item().model_dump(mode="json")]},
            )

        assert response.status_code == 200
        assert response.get_json()["total"] == "5.25"

    def test_pipeline_error_returns_503(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """Safeway pipeline failures surface as a JSON 503."""
        with patch(
            "grocery_butler.api._safeway_pipeline",
            side_effect=SafewayPipelineError("no credentials"),
        ):
            response = _post(
                client,
                auth_headers,
                "/api/v1/order/preview",
                {"shopping_list": [_make_shopping_item().model_dump(mode="json")]},
            )
        assert response.status_code == 503
        assert "error" in response.get_json()

    def test_pipeline_closed_even_when_build_fails(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """The Safeway client is cleaned up when cart building raises."""
        pipeline = MagicMock()
        pipeline.build_cart_only.side_effect = SafewayPipelineError("auth failed")
        with patch("grocery_butler.api._safeway_pipeline", return_value=pipeline):
            response = _post(
                client,
                auth_headers,
                "/api/v1/order/preview",
                {"shopping_list": [_make_shopping_item().model_dump(mode="json")]},
            )
        assert response.status_code == 503
        pipeline.close.assert_called_once_with()


# ---------------------------------------------------------------------------
# Anthropic client helper
# ---------------------------------------------------------------------------


class TestAnthropicClientHelper:
    """grocery_butler.api._anthropic_client."""

    def test_returns_none_without_api_key(self) -> None:
        """No ANTHROPIC_API_KEY means no client (pure-Python fallbacks)."""
        from grocery_butler.api import _anthropic_client

        with patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}):
            assert _anthropic_client() is None

    def test_builds_client_from_env_key(self) -> None:
        """A configured key is passed through to make_anthropic_client."""
        from grocery_butler import api

        with (
            patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test"}),
            patch.object(api, "make_anthropic_client") as factory,
        ):
            factory.return_value = object()
            assert api._anthropic_client() is factory.return_value
        factory.assert_called_once_with("sk-test")
