"""Tests for the production process wiring (issue #49).

One gunicorn web process serves both the HTML dashboard and the /api/v1
blueprint; the Discord worker process is retired from the Procfile (RubotPaul
is the Discord interface after cutover).
"""

from __future__ import annotations

import re
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
DOCKERFILE = Path(__file__).resolve().parent.parent / "Dockerfile"

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


# ---------------------------------------------------------------------------
# Dockerfile topology
# ---------------------------------------------------------------------------


class TestDockerfileTopology:
    """The container image runs gunicorn directly; it never reads Procfile."""

    def test_dockerfile_cmd_uses_web_concurrency_env(self) -> None:
        """Worker count is configurable via WEB_CONCURRENCY at runtime."""
        cmd = _dockerfile_cmd()
        assert "--workers ${WEB_CONCURRENCY:-2}" in cmd

    def test_dockerfile_cmd_has_no_hardcoded_worker_count(self) -> None:
        """The CMD must not pin a literal worker count like `--workers 2`."""
        cmd = _dockerfile_cmd()
        assert re.search(r"--workers\s+\d+", cmd) is None

    def test_dockerfile_does_not_copy_procfile(self) -> None:
        """The image is Heroku-agnostic; it must not COPY the Procfile in."""
        lines = DOCKERFILE.read_text().splitlines()
        assert not any("COPY Procfile" in line for line in lines)

    def test_dockerfile_still_runs_gunicorn_app_factory(self) -> None:
        """The container still boots the Flask app via the gunicorn factory."""
        cmd = _dockerfile_cmd()
        assert "gunicorn 'grocery_butler.app:create_app()'" in cmd


def _dockerfile_cmd() -> str:
    """Return the command string of the Dockerfile's container-entrypoint CMD.

    The HEALTHCHECK instruction's continuation line also starts with
    `CMD ` (it is itself a `CMD <command>` clause), so this returns the
    *last* matching line in the file, which is the real `CMD` instruction
    that becomes the container's entrypoint.

    Returns:
        The command text following the `CMD ` prefix on the last line of
        the Dockerfile that starts with `CMD `.
    """
    command = ""
    for line in DOCKERFILE.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("CMD "):
            command = stripped.removeprefix("CMD ")
    return command


# ---------------------------------------------------------------------------
# App factory database wiring (issue #57)
# ---------------------------------------------------------------------------

FAKE_POSTGRES_DSN = "postgresql://user:pass@db.example/testdb"


def _noop_init_db(db_path: str) -> None:
    """Stand in for ``grocery_butler.app.init_db`` without a live connection.

    A ``postgresql://`` DSN passed to the real ``init_db`` would attempt to
    open an actual network connection via psycopg2, which these tests must
    not do since ``FAKE_POSTGRES_DSN`` points at a non-existent host.

    Args:
        db_path: Path or URL that would normally be initialized. Unused.
    """


class TestAppFactoryDatabaseWiring:
    """create_app() must resolve DATABASE_URL/DATABASE_PATH env vars.

    The production gunicorn invocation (``gunicorn
    'grocery_butler.app:create_app()'``) calls ``create_app()`` with no
    arguments, so the factory itself must read the Railway-provided
    ``DATABASE_URL`` (or ``DATABASE_PATH``) environment variable. Without
    this, every deploy silently falls back to the ``"mealbot.db"`` default,
    writing to ephemeral SQLite instead of the persistent Postgres database
    (issue #57).
    """

    def test_create_app_no_args_uses_database_url_when_set(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """create_app() with no args stores DATABASE_URL as the db target.

        Args:
            monkeypatch: Pytest fixture for patching env vars and attributes.
        """
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("DATABASE_PATH", raising=False)
        monkeypatch.setenv("DATABASE_URL", FAKE_POSTGRES_DSN)
        monkeypatch.setattr("grocery_butler.app.init_db", _noop_init_db)

        application = create_app()

        assert application.config["DATABASE_PATH"] == FAKE_POSTGRES_DSN

    def test_create_app_no_args_uses_database_path_when_only_path_set(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """create_app() with no args falls back to DATABASE_PATH when set.

        Args:
            monkeypatch: Pytest fixture for patching env vars.
            tmp_path: Pytest fixture providing an isolated temp directory.
        """
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("DATABASE_PATH", raising=False)
        env_db_path = str(tmp_path / "env_configured.db")
        monkeypatch.setenv("DATABASE_PATH", env_db_path)

        application = create_app()

        assert application.config["DATABASE_PATH"] == env_db_path

    def test_create_app_prefers_database_url_over_database_path(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """DATABASE_URL takes precedence when both env vars are set.

        Args:
            monkeypatch: Pytest fixture for patching env vars and attributes.
            tmp_path: Pytest fixture providing an isolated temp directory.
        """
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("DATABASE_PATH", raising=False)
        monkeypatch.setenv("DATABASE_URL", FAKE_POSTGRES_DSN)
        monkeypatch.setenv("DATABASE_PATH", str(tmp_path / "should_be_ignored.db"))
        monkeypatch.setattr("grocery_butler.app.init_db", _noop_init_db)

        application = create_app()

        assert application.config["DATABASE_PATH"] == FAKE_POSTGRES_DSN

    def test_create_app_defaults_to_mealbot_db_when_env_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """create_app() falls back to "mealbot.db" when no env var is set.

        Args:
            monkeypatch: Pytest fixture for patching env vars and attributes.
        """
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("DATABASE_PATH", raising=False)
        monkeypatch.setattr("grocery_butler.app.init_db", _noop_init_db)

        application = create_app()

        assert application.config["DATABASE_PATH"] == "mealbot.db"

    def test_create_app_explicit_db_path_overrides_env(
        self, monkeypatch: pytest.MonkeyPatch, tmp_path: Path
    ) -> None:
        """An explicit db_path argument wins over any env var.

        Args:
            monkeypatch: Pytest fixture for patching env vars.
            tmp_path: Pytest fixture providing an isolated temp directory.
        """
        monkeypatch.delenv("DATABASE_URL", raising=False)
        monkeypatch.delenv("DATABASE_PATH", raising=False)
        monkeypatch.setenv("DATABASE_URL", FAKE_POSTGRES_DSN)
        explicit_path = str(tmp_path / "explicit.db")

        application = create_app(db_path=explicit_path)

        assert application.config["DATABASE_PATH"] == explicit_path
