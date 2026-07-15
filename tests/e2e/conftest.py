"""Shared fixtures for the grocery-butler end-to-end test suite.

Every fixture wires REAL application components together: the Flask app
via ``create_app``, a real temporary SQLite database, and real
stores/services/pipeline objects. Mocking is restricted to external
network boundaries only:

* the Anthropic SDK client object (``make_anthropic_client``), and
* the httpx transport inside :class:`~grocery_butler.safeway_client.SafewayClient`.

All state comes from ``monkeypatch``/``tmp_path`` -- nothing ambient.
``load_dotenv(override=False)`` (see ``grocery_butler.config.load_config``)
means ``monkeypatch.setenv`` always wins over a developer's local
``.env`` file, so these tests are deterministic in CI (which has no
secrets) and locally.
"""

from __future__ import annotations

import json
import os
import time
from dataclasses import dataclass, field
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import httpx
import pytest

from grocery_butler.app import create_app
from grocery_butler.auth_middleware import SECRET_ENV_VAR, RequestBinding, mint_token
from grocery_butler.models import Ingredient, IngredientCategory, ParsedMeal
from grocery_butler.recipe_store import RecipeStore
from grocery_butler.safeway_client import OKTA_CLIENT_ID
from grocery_butler.safeway_client import SafewayClient as _RealSafewayClient

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient

#: Shared HMAC secret every minted token in this suite is signed with.
E2E_SHARED_SECRET = "e2e-test-secret"

#: Deterministic seeded recipe name, resolvable via RecipeStore without Claude.
SEEDED_RECIPE_NAME = "spaghetti bolognese"


# ---------------------------------------------------------------------------
# Database and environment
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a fresh temporary SQLite database path.

    A single seam for the storage backend: every fixture and service in
    this suite reads and writes through this path, so a future
    PostgreSQL variant only needs to change this fixture.

    Args:
        tmp_path: Pytest's per-test temporary directory.

    Returns:
        Absolute path string for a database file that does not yet exist.
    """
    return str(tmp_path / "e2e.db")


@pytest.fixture(autouse=True)
def e2e_env(monkeypatch: pytest.MonkeyPatch, db_path: str) -> None:
    """Set every environment variable the app needs, isolated per test.

    ``load_config`` calls ``load_dotenv`` with the default
    ``override=False``, so these ``monkeypatch.setenv`` calls always win
    over any local ``.env`` file -- CI has no secrets and none are
    needed here.

    Args:
        monkeypatch: Pytest's monkeypatch fixture.
        db_path: The temporary database path for this test.
    """
    monkeypatch.setenv(SECRET_ENV_VAR, E2E_SHARED_SECRET)
    monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-e2e-not-real")
    monkeypatch.setenv("SAFEWAY_USERNAME", "e2e-safeway-user")
    monkeypatch.setenv("SAFEWAY_PASSWORD", "e2e-safeway-pass")
    monkeypatch.setenv("SAFEWAY_STORE_ID", "e2e-store-1")
    monkeypatch.setenv("DATABASE_PATH", db_path)
    # Issue #60: order submission is opted in for this suite's full-chain
    # tests, which exercise real submission against the mocked transport.
    # The production default (env var unset) is False -- see
    # test_api_chain.py::test_disabled_submission_returns_501_default_off
    # for coverage of the fail-safe default itself.
    monkeypatch.setenv("SAFEWAY_ORDER_SUBMISSION_ENABLED", "true")


@pytest.fixture()
def seed_recipe(db_path: str) -> ParsedMeal:
    """Seed a deterministic recipe so ``MealParser.find_recipe`` resolves it.

    Saving through a real :class:`RecipeStore` means the seeded recipe
    round-trips through the exact same lookup path production code
    uses, so no Claude call is ever needed to resolve it by name.

    Args:
        db_path: The temporary database path for this test.

    Returns:
        The ParsedMeal as saved (name, servings, ingredients).
    """
    meal = ParsedMeal(
        name=SEEDED_RECIPE_NAME,
        servings=4,
        known_recipe=True,
        needs_confirmation=False,
        purchase_items=[
            Ingredient(
                ingredient="ground beef",
                quantity=1.0,
                unit="lb",
                category=IngredientCategory.MEAT,
            ),
            Ingredient(
                ingredient="spaghetti",
                quantity=1.0,
                unit="box",
                category=IngredientCategory.PANTRY_DRY,
            ),
            Ingredient(
                ingredient="tomato sauce",
                quantity=2.0,
                unit="can",
                category=IngredientCategory.PANTRY_DRY,
            ),
        ],
        pantry_items=[
            Ingredient(
                ingredient="salt",
                quantity=1.0,
                unit="tsp",
                category=IngredientCategory.PANTRY_DRY,
                is_pantry_item=True,
            ),
        ],
    )
    RecipeStore(db_path).save_recipe(meal)
    return meal


# ---------------------------------------------------------------------------
# Flask app / client
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(db_path: str, e2e_env: None) -> Flask:
    """Create the real Flask app wired to the temporary database.

    Uses an explicit ``db_path`` rather than relying on ``DATABASE_URL``
    pickup: issue #57 tracks an open bug in that alternate path, and
    this suite deliberately sidesteps it instead of asserting around it.

    Args:
        db_path: The temporary database path for this test.
        e2e_env: Ensures environment variables are set before app creation.

    Returns:
        A configured Flask application in ``TESTING`` mode.
    """
    application = create_app(db_path=db_path)
    application.config["TESTING"] = True
    return application


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    """Return a Flask test client bound to the real app.

    Args:
        app: The configured Flask application.

    Returns:
        A Flask test client.
    """
    return app.test_client()


# ---------------------------------------------------------------------------
# Auth
# ---------------------------------------------------------------------------


@pytest.fixture()
def signed_headers() -> Callable[..., dict[str, str]]:
    """Return a factory for minting ``Authorization`` headers.

    Supports building expired, future-dated, and foreign-secret tokens
    for exercising the auth module's rejection paths, in addition to
    plain valid tokens. Issue #74: tokens are now bound to the exact
    request they authorize (method + path + body hash) via
    ``RequestBinding``, so the factory accepts ``method``/``path``/
    ``body`` overrides -- defaulting to a bodyless ``GET
    /api/v1/inventory``, the request this fixture is most commonly used
    against across the e2e suite.

    Returns:
        A callable ``(caller_id="rubotpaul", *, method="GET",
        path="/api/v1/inventory", body=b"", now_offset=0.0,
        secret=None) -> {"Authorization": "Bearer <token>"}``. Positive
        ``now_offset`` mints a future-dated token; a very negative one
        mints an expired token. ``secret`` signs with a different
        shared secret than the one the server validates against.
    """

    def _make(
        caller_id: str = "rubotpaul",
        *,
        method: str = "GET",
        path: str = "/api/v1/inventory",
        body: bytes = b"",
        now_offset: float = 0.0,
        secret: str | None = None,
    ) -> dict[str, str]:
        binding = RequestBinding.of(method, path, body)
        now = time.time() + now_offset
        if secret is None:
            token = mint_token(caller_id, binding, now=now)
        else:
            real_secret = os.environ[SECRET_ENV_VAR]
            os.environ[SECRET_ENV_VAR] = secret
            try:
                token = mint_token(caller_id, binding, now=now)
            finally:
                os.environ[SECRET_ENV_VAR] = real_secret
        return {"Authorization": f"Bearer {token}"}

    return _make


@pytest.fixture()
def signed_request(
    client: FlaskClient, signed_headers: Callable[..., dict[str, str]]
) -> Callable[..., Any]:
    """Return a helper that mints a bound token and sends one request atomically.

    Issue #74 bound every bearer token to the exact ``(method, path,
    body)`` triple it authorizes, so a header dict minted once can no
    longer be reused across requests to different endpoints or with
    different bodies. This fixture closes that gap for callers that
    need to hit several endpoints in one test: it serializes
    ``json_body`` to bytes exactly once and mints the token off those
    same bytes, then sends those same bytes -- never re-serializing
    (which could byte-differ, e.g. on key order) between minting and
    sending.

    Args:
        client: The Flask test client.
        signed_headers: The token-minting header factory fixture.

    Returns:
        A callable ``(method, path, json_body=None, **header_kwargs) ->
        werkzeug.test.TestResponse``. ``json_body`` is any
        JSON-serializable value; omit it (or pass ``None``) to send a
        bodyless request. Extra ``header_kwargs`` (``caller_id``,
        ``now_offset``, ``secret``) are forwarded to ``signed_headers``.
    """

    def _send(
        method: str,
        path: str,
        json_body: Any = None,
        **header_kwargs: Any,
    ) -> Any:
        """Mint a token bound to this exact request, then send it."""
        body = b"" if json_body is None else json.dumps(json_body).encode()
        headers = signed_headers(method=method, path=path, body=body, **header_kwargs)
        call = getattr(client, method.lower())
        if json_body is None:
            return call(path, headers=headers)
        return call(path, data=body, content_type="application/json", headers=headers)

    return _send


# ---------------------------------------------------------------------------
# Claude seam
# ---------------------------------------------------------------------------


@pytest.fixture()
def no_claude(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force API-layer Claude-backed services onto their deterministic fallback.

    Patches the ``make_anthropic_client`` name as imported into
    ``grocery_butler.api`` so ``_anthropic_client()`` returns ``None``
    even though ``ANTHROPIC_API_KEY`` is set. This is the seam that
    keeps API-layer e2e tests offline and deterministic (MealParser,
    Consolidator, ProductSelector, and SubstitutionService all fall
    back to pure-Python heuristics when their client is ``None``).

    Args:
        monkeypatch: Pytest's monkeypatch fixture.
    """
    monkeypatch.setattr(
        "grocery_butler.api.make_anthropic_client",
        lambda api_key: None,
    )


@pytest.fixture()
def no_claude_cli(monkeypatch: pytest.MonkeyPatch) -> None:
    """Force CLI-driven Claude calls onto their deterministic fallback.

    ``grocery_butler.cli`` imports ``make_anthropic_client`` locally
    inside ``_make_anthropic_client`` on every call, so patching the
    source function in ``grocery_butler.claude_utils`` is what actually
    takes effect: the local ``from ... import`` re-resolves the name
    from the module namespace each time it runs.

    Args:
        monkeypatch: Pytest's monkeypatch fixture.
    """
    monkeypatch.setattr(
        "grocery_butler.claude_utils.make_anthropic_client",
        lambda api_key: None,
    )


@pytest.fixture()
def fake_claude(monkeypatch: pytest.MonkeyPatch) -> Callable[[str], None]:
    """Patch the Claude seam to return a scripted mock client.

    Use only when a scenario genuinely needs a Claude "decision"
    instead of a deterministic fallback -- check whether the no-client
    fallback already produces the desired behavior first.

    Args:
        monkeypatch: Pytest's monkeypatch fixture.

    Returns:
        A callable that scripts the mock client's ``messages.create``
        response text for subsequent calls.
    """
    mock_client = MagicMock()

    def _script(response_text: str) -> None:
        """Set the text the mock client's next Claude call will return."""
        response = MagicMock()
        response.content = [MagicMock(text=response_text)]
        mock_client.messages.create.return_value = response

    monkeypatch.setattr(
        "grocery_butler.api.make_anthropic_client",
        lambda api_key: mock_client,
    )
    return _script


# ---------------------------------------------------------------------------
# Safeway mock transport
# ---------------------------------------------------------------------------


@dataclass
class SafewayMockState:
    """Mutable configuration and call recorder for the mocked Safeway API.

    Attributes:
        available_products: Steady-state search results keyed by search
            term; each value is a list of raw Nimbus ``productsInfo``
            entries (dicts with ``upc``/``name``/``price``/``size``/
            ``inStock`` keys).
        oos_once: Search terms whose FIRST search call returns a single
            out-of-stock product (forcing the substitution flow);
            subsequent calls for the same term fall back to
            ``available_products``.
        force_search_empty: If True, every product search returns no
            results, exercising the ``failed_items`` path.
        force_auth_fail: If True, the Okta authn step returns a 500,
            exercising the pipeline's authentication-failure path.
        order_response: The JSON body returned by the order-submit
            endpoint.
        requested_paths: Every request path seen by the mock transport,
            in call order.
    """

    available_products: dict[str, list[dict[str, Any]]] = field(default_factory=dict)
    oos_once: set[str] = field(default_factory=set)
    force_search_empty: bool = False
    force_auth_fail: bool = False
    order_response: dict[str, Any] = field(
        default_factory=lambda: {
            "orderId": "e2e-order-1",
            "status": "confirmed",
            "estimatedTime": "Tomorrow 9am-11am",
            "total": "0",
        }
    )
    requested_paths: list[str] = field(default_factory=list)
    search_call_counts: dict[str, int] = field(default_factory=dict)


def _default_product(
    term: str,
    *,
    in_stock: bool = True,
    price: float = 3.99,
) -> dict[str, Any]:
    """Build a generic Nimbus ``productsInfo`` entry for a search term.

    Args:
        term: The search term the product should match.
        in_stock: Stock flag for the returned product.
        price: Price for the returned product.

    Returns:
        A dict shaped like a Nimbus search result entry.
    """
    slug = term.strip().lower().replace(" ", "-") or "item"
    return {
        "upc": f"upc-{slug}",
        "name": f"Generic {term.title()}",
        "price": price,
        "size": "1 lb",
        "inStock": in_stock,
    }


def _handle_authn(state: SafewayMockState) -> httpx.Response:
    """Return the Okta authn response: a session token, or a failure.

    Args:
        state: The mutable mock state for this test.

    Returns:
        A 200 response with a session token, or a 500 if
        ``state.force_auth_fail`` is set.
    """
    if state.force_auth_fail:
        return httpx.Response(500, json={"error": "internal error"})
    return httpx.Response(
        200,
        json={"status": "SUCCESS", "sessionToken": "e2e-session-token"},
    )


def _handle_authorize() -> httpx.Response:
    """Return the Okta authorize redirect with a bearer token fragment.

    Returns:
        A 302 response whose ``Location`` header fragment carries a
        fake access token.
    """
    location = "https://www.safeway.com#access_token=e2e-access-token&expires_in=3600"
    return httpx.Response(302, headers={"location": location})


def _handle_search(request: httpx.Request, state: SafewayMockState) -> httpx.Response:
    """Return search results for a query, honoring the state's knobs.

    Args:
        request: The incoming search request.
        state: The mutable mock state for this test.

    Returns:
        A 200 response with a ``productsInfo`` list.
    """
    query = request.url.params.get("q", "")
    if state.force_search_empty:
        return httpx.Response(200, json={"productsInfo": []})

    call_index = state.search_call_counts.get(query, 0)
    state.search_call_counts[query] = call_index + 1

    if query in state.oos_once and call_index == 0:
        products = [_default_product(query, in_stock=False, price=1.99)]
    else:
        products = state.available_products.get(query, [_default_product(query)])
    return httpx.Response(200, json={"productsInfo": products})


def _handle_fulfillment() -> httpx.Response:
    """Return a single available pickup fulfillment option.

    Returns:
        A 200 response with one available, fee-free pickup option.
    """
    return httpx.Response(
        200,
        json={
            "fulfillmentOptions": [
                {"type": "pickup", "available": True, "fee": 0.0, "windows": []},
            ]
        },
    )


def _build_safeway_handler(
    state: SafewayMockState,
) -> Callable[[httpx.Request], httpx.Response]:
    """Build the routing handler for the mocked Safeway ``MockTransport``.

    Routes on the request path to the Okta authn/authorize steps and
    the Nimbus search/fulfillment/order endpoints that
    ``grocery_butler.safeway_client`` and ``grocery_butler.cart_builder``
    call.

    Args:
        state: The mutable state driving responses for this test.

    Returns:
        A handler suitable for ``httpx.MockTransport``.
    """

    def handler(request: httpx.Request) -> httpx.Response:
        """Dispatch a mocked Safeway/Okta request to the right response."""
        path = request.url.path
        state.requested_paths.append(path)

        if path == "/api/v1/authn":
            return _handle_authn(state)
        if path == f"/oauth2/{OKTA_CLIENT_ID}/v1/authorize":
            return _handle_authorize()
        if path == "/api/v2/grocerystore/search":
            return _handle_search(request, state)
        if path.startswith("/abs/pub/web/stores/") and path.endswith("/fulfillment"):
            return _handle_fulfillment()
        if path == "/abs/pub/web/orders":
            return httpx.Response(200, json=state.order_response)
        return httpx.Response(404, json={"error": f"unhandled path {path}"})

    return handler


@pytest.fixture()
def safeway_mock(monkeypatch: pytest.MonkeyPatch) -> SafewayMockState:
    """Route ``SafewayPipeline``'s HTTP client through a ``MockTransport``.

    Kills the real rate limiter (no ``time.sleep`` in tests) and wraps
    the real :class:`SafewayClient` class so ``SafewayPipeline`` gets a
    client whose ``http_client`` is backed by an in-process
    ``httpx.MockTransport`` -- no real network traffic, and no
    ``grocery_butler`` service classes are mocked.

    Args:
        monkeypatch: Pytest's monkeypatch fixture.

    Returns:
        The mutable :class:`SafewayMockState` scenarios drive and assert on.
    """
    state = SafewayMockState()
    handler = _build_safeway_handler(state)
    monkeypatch.setattr("grocery_butler.safeway_client._MIN_REQUEST_INTERVAL", 0.0)

    def factory(**kwargs: Any) -> _RealSafewayClient:
        """Build a real SafewayClient wired to the mock transport."""
        return _RealSafewayClient(
            **kwargs,
            http_client=httpx.Client(transport=httpx.MockTransport(handler)),
        )

    monkeypatch.setattr("grocery_butler.safeway_pipeline.SafewayClient", factory)
    return state
