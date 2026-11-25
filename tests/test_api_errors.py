"""Tests for the /api/v1 error contract (Issue #77).

Pins down that ``/api/v1`` never leaks an HTML error page or internal
exception detail to a JSON client: unknown paths, wrong HTTP methods,
and uncaught exceptions must all render terse JSON that never echoes
the underlying exception text or config value into the response body.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient

    AuthHeaderFactory = Callable[[str, str, bytes], dict[str, str]]

from grocery_butler.app import create_app
from grocery_butler.auth_middleware import SECRET_ENV_VAR
from tests.conftest import bearer_header

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
def auth_headers() -> AuthHeaderFactory:
    """Return a factory that mints a bearer header for one exact request.

    The request-bound token contract (issue #74) signs method, path, and
    body together, so a single static header cannot authorize every
    request in this module. Callers invoke the returned factory once per
    request.

    Returns:
        A callable ``(method, path, body=b"") -> {"Authorization": ...}``
        that mints tokens under the test shared secret.
    """

    def _make(method: str, path: str, body: bytes = b"") -> dict[str, str]:
        """Mint a bearer header bound to the given method/path/body."""
        with patch.dict(os.environ, SECRET_ENV):
            return bearer_header("rubotpaul", method, path, body)

    return _make


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
        auth_headers: AuthHeaderFactory,
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
            response = client.get(
                "/api/v1/inventory",
                headers=auth_headers("GET", "/api/v1/inventory"),
            )
        assert response.status_code == 500
        assert response.is_json
        assert response.get_json() == {"error": "internal server error"}
        body_text = response.get_data(as_text=True)
        assert "sk-secret" not in body_text
        assert "ANTHROPIC_API_KEY" not in body_text
        assert "<!doctype html" not in body_text.lower()


# ---------------------------------------------------------------------------
# Regression guards: behavior that is already correct and must not regress
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestRegressionGuards:
    """Already-correct /api/v1 behavior that the #77 fix must not break."""

    def test_recipe_not_found_still_uses_blueprint_json_404(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """An in-route abort(404) still renders the blueprint's descriptive JSON.

        Distinct from the unmatched-path case above: this hits a real
        route whose handler explicitly aborts 404 for an unknown id, so
        the blueprint's existing ``@api_v1.errorhandler(404)`` already
        renders JSON correctly and must keep doing so.
        """
        response = client.get(
            "/api/v1/recipes/999999",
            headers=auth_headers("GET", "/api/v1/recipes/999999"),
        )
        assert response.status_code == 404
        assert response.get_json() == {"error": "recipe not found"}

    def test_missing_bearer_on_inventory_still_returns_json_401(
        self, client: FlaskClient
    ) -> None:
        """No Authorization header on a real route still returns JSON 401."""
        response = client.get("/api/v1/inventory")
        assert response.status_code == 401
        assert response.is_json
