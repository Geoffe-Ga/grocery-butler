"""Tests for the production process wiring (issue #49).

One gunicorn web process serves both the HTML dashboard and the /api/v1
blueprint; the Discord worker process is retired from the Procfile (RubotPaul
is the Discord interface after cutover).
"""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient

from grocery_butler.app import create_app
from grocery_butler.auth_middleware import SECRET_ENV_VAR, mint_token

TEST_SECRET = "test-shared-secret"

PROCFILE = Path(__file__).resolve().parent.parent / "Procfile"

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a temporary database path for test isolation."""
    return str(tmp_path / "test_wiring.db")


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
# One web process serves both surfaces
# ---------------------------------------------------------------------------


class TestSingleProcessServesBothSurfaces:
    """The web app instance serves the HTML UI and the authed JSON API."""

    def test_html_dashboard_and_authed_api_in_one_app(
        self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """One app instance answers an HTML route and an authed API route."""
        monkeypatch.setenv(SECRET_ENV_VAR, TEST_SECRET)

        html_response = client.get("/")
        assert html_response.status_code == 200
        assert "text/html" in html_response.content_type

        token = mint_token("rubotpaul")
        api_response = client.get(
            "/api/v1/inventory",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert api_response.status_code == 200
        assert api_response.is_json
        assert api_response.get_json() == {"items": []}

    def test_api_still_rejects_unauthenticated_while_html_is_open(
        self, client: FlaskClient, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The API keeps HMAC auth while the kitchen-phone UI stays open."""
        monkeypatch.setenv(SECRET_ENV_VAR, TEST_SECRET)

        assert client.get("/").status_code == 200
        api_response = client.get("/api/v1/inventory")
        assert api_response.status_code == 401
        assert api_response.is_json


# ---------------------------------------------------------------------------
# Procfile topology
# ---------------------------------------------------------------------------


class TestProcfileTopology:
    """The Procfile deploys release + web only; the Discord worker retired."""

    def test_procfile_has_no_worker_process(self) -> None:
        """RubotPaul owns Discord after cutover; no bot process deploys."""
        processes = _procfile_processes()
        assert "worker" not in processes

    def test_procfile_keeps_release_and_web(self) -> None:
        """Migrations still run on deploy and gunicorn serves the app."""
        processes = _procfile_processes()
        assert processes["release"] == "python -m grocery_butler.db.migrate"
        assert "gunicorn 'grocery_butler.app:create_app()'" in processes["web"]


def _procfile_processes() -> dict[str, str]:
    """Parse the Procfile into a {process_name: command} mapping."""
    processes: dict[str, str] = {}
    for line in PROCFILE.read_text().splitlines():
        stripped = line.strip()
        if not stripped or stripped.startswith("#"):
            continue
        name, _, command = stripped.partition(":")
        processes[name.strip()] = command.strip()
    return processes
