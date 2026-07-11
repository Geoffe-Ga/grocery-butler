"""Tests for the production process wiring (issues #49, #57, and #58).

One gunicorn web process serves both the HTML dashboard and the /api/v1
blueprint; the Discord worker process is retired from the Procfile (RubotPaul
is the Discord interface after cutover). The Dockerfile is the single
canonical deploy path (issue #58): a `docker-entrypoint.sh` wrapper runs
database migrations before `exec`-ing into the gunicorn server, and the
Heroku-only Procfile is deleted entirely. The `create_app()` factory itself
resolves the `DATABASE_URL`/`DATABASE_PATH` environment variables so the
no-argument gunicorn invocation targets the persistent database (issue #57).
"""

from __future__ import annotations

import os
import re
import subprocess
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
ENTRYPOINT_SCRIPT = Path(__file__).resolve().parent.parent / "docker-entrypoint.sh"
MIGRATE_COMMAND = "python -m grocery_butler.db.migrate"

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
# Single canonical deploy path (issue #58)
# ---------------------------------------------------------------------------


class TestSingleCanonicalDeployPath:
    """The Dockerfile is the only deploy path; the Heroku Procfile is gone."""

    def test_procfile_absent(self) -> None:
        """Procfile is deleted; Railway never reads it, so it must not exist."""
        assert not PROCFILE.exists()


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
# Dockerfile wires in docker-entrypoint.sh (issue #58)
# ---------------------------------------------------------------------------


class TestDockerfileEntrypointWiring:
    """The image COPYs docker-entrypoint.sh in and declares it ENTRYPOINT."""

    def test_dockerfile_declares_entrypoint(self) -> None:
        """An ENTRYPOINT instruction must invoke docker-entrypoint.sh."""
        entrypoint_line = _dockerfile_entrypoint_line()
        assert "docker-entrypoint.sh" in entrypoint_line

    def test_dockerfile_copies_entrypoint_executable(self) -> None:
        """docker-entrypoint.sh is COPYed in and made executable."""
        text = DOCKERFILE.read_text()
        copies_script = any(
            line.strip().startswith("COPY") and "docker-entrypoint.sh" in line
            for line in text.splitlines()
        )
        assert copies_script, "Dockerfile must COPY docker-entrypoint.sh"
        assert _dockerfile_makes_entrypoint_executable(text)


def _dockerfile_entrypoint_line() -> str:
    """Return the Dockerfile's `ENTRYPOINT` instruction line, if present.

    Returns:
        The stripped text of the line starting with `ENTRYPOINT `, or an
        empty string if the Dockerfile declares no ENTRYPOINT.
    """
    for line in DOCKERFILE.read_text().splitlines():
        stripped = line.strip()
        if stripped.startswith("ENTRYPOINT "):
            return stripped
    return ""


def _dockerfile_makes_entrypoint_executable(text: str) -> bool:
    """Return whether the Dockerfile marks docker-entrypoint.sh executable.

    Args:
        text: Full contents of the Dockerfile.

    Returns:
        True if a `COPY --chmod=0755` clause targets the entrypoint script,
        or a separate `RUN` instruction `chmod +x`s it; False otherwise.
    """
    for line in text.splitlines():
        stripped = line.strip()
        if "docker-entrypoint.sh" not in stripped:
            continue
        if stripped.startswith("COPY") and "--chmod=0755" in stripped:
            return True
        if stripped.startswith("RUN") and "chmod" in stripped and "+x" in stripped:
            return True
    return False


# ---------------------------------------------------------------------------
# docker-entrypoint.sh contents (issue #58)
# ---------------------------------------------------------------------------


class TestEntrypointScriptContents:
    """Static checks on the text of docker-entrypoint.sh."""

    def test_entrypoint_invokes_migration_runner(self) -> None:
        """The script must run the migration module before serving."""
        text = ENTRYPOINT_SCRIPT.read_text()
        assert MIGRATE_COMMAND in text

    def test_entrypoint_execs_cmd_last(self) -> None:
        """`exec "$@"` must appear, and only after the migration command."""
        text = ENTRYPOINT_SCRIPT.read_text()
        exec_snippet = 'exec "$@"'
        assert exec_snippet in text
        assert text.index(MIGRATE_COMMAND) < text.index(exec_snippet)

    def test_entrypoint_fails_fast(self) -> None:
        """`set -e` (or `-eu`) must appear near the top so failures abort."""
        non_comment_lines = [
            line.strip()
            for line in ENTRYPOINT_SCRIPT.read_text().splitlines()
            if line.strip() and not line.strip().startswith("#")
        ][:5]
        assert any(line.startswith("set -e") for line in non_comment_lines)


# ---------------------------------------------------------------------------
# docker-entrypoint.sh behavior (issue #58)
# ---------------------------------------------------------------------------


class TestEntrypointBehavior:
    """Runs docker-entrypoint.sh against fake `python`/server shims."""

    def test_entrypoint_runs_migrations_before_server(self, tmp_path: Path) -> None:
        """Migrations run, then the wrapped server command execs, in order."""
        log_path = tmp_path / "log.txt"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_shim(bin_dir / "python", log_path, "migrate", exit_code=0)
        server_shim = bin_dir / "server"
        _write_shim(server_shim, log_path, "server", exit_code=0)

        result = _run_entrypoint(bin_dir, [str(server_shim)])

        assert result.returncode == 0
        assert log_path.read_text().splitlines() == ["migrate", "server"]

    def test_entrypoint_aborts_server_when_migration_fails(
        self, tmp_path: Path
    ) -> None:
        """A failing migration must abort before the server ever runs."""
        log_path = tmp_path / "log.txt"
        bin_dir = tmp_path / "bin"
        bin_dir.mkdir()
        _write_shim(bin_dir / "python", log_path, "migrate", exit_code=1)
        server_shim = bin_dir / "server"
        _write_shim(server_shim, log_path, "server", exit_code=0)

        result = _run_entrypoint(bin_dir, [str(server_shim)])

        assert result.returncode != 0
        log_contents = log_path.read_text() if log_path.exists() else ""
        assert "migrate" in log_contents.splitlines()
        assert "server" not in log_contents.splitlines()


def _write_shim(path: Path, log_path: Path, label: str, *, exit_code: int) -> None:
    """Write an executable POSIX shell shim that logs one line and exits.

    Args:
        path: Filesystem path at which to create the shim script.
        log_path: File the shim appends `label` to when invoked.
        label: Line appended to `log_path` on invocation.
        exit_code: Process exit status the shim returns.
    """
    path.write_text(f'#!/bin/sh\necho "{label}" >> "{log_path}"\nexit {exit_code}\n')
    path.chmod(0o755)


def _run_entrypoint(
    bin_dir: Path, server_argv: list[str]
) -> subprocess.CompletedProcess[str]:
    """Run docker-entrypoint.sh with a fake `python` shim ahead on PATH.

    Args:
        bin_dir: Directory containing the fake `python` shim, prepended to
            `PATH` so the script's migration step invokes the shim.
        server_argv: Argv passed to the script, standing in for the real
            gunicorn `CMD` that `exec "$@"` should hand off to.

    Returns:
        The completed subprocess, including its captured exit code.
    """
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env.get('PATH', '')}"
    return subprocess.run(
        ["sh", str(ENTRYPOINT_SCRIPT), *server_argv],
        env=env,
        capture_output=True,
        text=True,
        timeout=10,
        check=False,
    )


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
