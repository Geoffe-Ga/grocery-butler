"""Shared fixtures for the grocery-butler end-to-end test suite.

Every fixture wires REAL application components together: the Flask app
via ``create_app``, a real temporary SQLite database, and real
stores/services. Mocking is restricted to the one external network
boundary: the Anthropic SDK client object (``make_anthropic_client``).
The suite sets no retailer configuration at all, the same as production
(ADR 0001).

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
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from grocery_butler.app import create_app
from grocery_butler.auth_middleware import SECRET_ENV_VAR, RequestBinding, mint_token
from grocery_butler.db import init_db
from grocery_butler.models import Ingredient, IngredientCategory, ParsedMeal
from grocery_butler.recipe_store import RecipeStore

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
    path = str(tmp_path / "e2e.db")
    init_db(path)
    return path


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
    monkeypatch.setenv("DATABASE_PATH", db_path)


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
    # CSRF has its own tests (tests/test_web_security.py); off here so these
    # tests can post forms directly.
    application.config["WEB_CSRF_ENABLED"] = False
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
