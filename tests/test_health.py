"""Tests for the /health endpoint."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient

from grocery_butler.app import create_app

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a temporary database path for test isolation."""
    return str(tmp_path / "test_health.db")


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


# ---------------------------------------------------------------------------
# Tests
# ---------------------------------------------------------------------------


class TestHealthEndpoint:
    """Tests for GET /health."""

    def test_returns_200_ok(self, client: FlaskClient) -> None:
        """Test health endpoint returns 200 when database is connected."""
        response = client.get("/health")
        assert response.status_code == 200

    def test_json_content_type(self, client: FlaskClient) -> None:
        """Test health endpoint returns JSON content type."""
        response = client.get("/health")
        assert response.content_type == "application/json"

    def test_response_shape(self, client: FlaskClient) -> None:
        """Test health response contains expected fields."""
        response = client.get("/health")
        data = json.loads(response.data)
        assert data["status"] == "healthy"
        assert data["database"] == "connected"

    def test_returns_503_on_db_failure(self, app: Flask, client: FlaskClient) -> None:
        """Test health endpoint returns 503 when database is unreachable."""
        with patch(
            "grocery_butler.app._get_db",
            side_effect=RuntimeError("DB down"),
        ):
            response = client.get("/health")
            assert response.status_code == 503
            data = json.loads(response.data)
            assert data["status"] == "unhealthy"
            assert data["database"] == "disconnected"
