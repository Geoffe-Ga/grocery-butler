"""Tests for fail-fast production startup config validation (issue #64).

``create_app()`` must refuse to start in production with missing secrets
(``FLASK_SECRET_KEY``, ``RUBOTPAUL_SHARED_SECRET``) rather than silently
booting with a random session key or deferring the auth failure to the
first request. In dev/test mode (``APP_ENV`` unset or not "production")
those same gaps are tolerated but loudly warned about. A missing
``ANTHROPIC_API_KEY`` is warn-only in every mode since Claude-backed
pipelines already degrade gracefully without it.
"""

from __future__ import annotations

import logging
import re
from pathlib import Path

import pytest

from grocery_butler.app import create_app
from grocery_butler.auth_middleware import SECRET_ENV_VAR

REPO_ROOT = Path(__file__).resolve().parent.parent
ENV_EXAMPLE = REPO_ROOT / ".env.example"
DOCKERFILE = REPO_ROOT / "Dockerfile"

_STARTUP_ENV_VARS = (
    "APP_ENV",
    "FLASK_SECRET_KEY",
    "RUBOTPAUL_SHARED_SECRET",
    "ANTHROPIC_API_KEY",
)


def _clear_env(monkeypatch: pytest.MonkeyPatch) -> None:
    """Delete every startup-config env var so ambient env can't leak in.

    A developer's real ``.env`` or shell environment must never influence
    these tests, so each test clears the full set explicitly before
    setting only the variables it cares about.

    Args:
        monkeypatch: Pytest fixture used to delete environment variables
            for the duration of the current test.
    """
    for name in _STARTUP_ENV_VARS:
        monkeypatch.delenv(name, raising=False)


def _env_example_has_key(key: str) -> bool:
    """Return whether ``.env.example`` declares a line assigning ``key``.

    Args:
        key: Environment variable name to look for, e.g. ``"APP_ENV"``.

    Returns:
        True if a line of the form ``key=...`` is present in the file.
    """
    pattern = re.compile(rf"^{re.escape(key)}=")
    return any(
        pattern.match(line.strip()) for line in ENV_EXAMPLE.read_text().splitlines()
    )


def _warning_messages(
    caplog: pytest.LogCaptureFixture, logger_name: str = "grocery_butler.app"
) -> list[str]:
    """Return formatted messages of WARNING+ records emitted by ``logger_name``.

    Args:
        caplog: Pytest fixture capturing log records for the current test.
        logger_name: Logger name to filter records by.

    Returns:
        List of formatted (``getMessage()``) warning-or-above log messages.
    """
    return [
        record.getMessage()
        for record in caplog.records
        if record.name == logger_name and record.levelno >= logging.WARNING
    ]


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a temporary database path for test isolation."""
    return str(tmp_path / "test_startup.db")


# ---------------------------------------------------------------------------
# _is_production() helper
# ---------------------------------------------------------------------------


class TestIsProductionHelper:
    """Tests for the ``_is_production`` startup helper in grocery_butler.app."""

    def test_is_production_true_when_app_env_production(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """APP_ENV=production reports production mode."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", "production")
        from grocery_butler.app import _is_production

        assert _is_production() is True

    def test_is_production_true_case_insensitive(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """APP_ENV comparison ignores case, e.g. "Production" or "PRODUCTION"."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", "PRODUCTION")
        from grocery_butler.app import _is_production

        assert _is_production() is True

    def test_is_production_false_when_unset(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """An unset APP_ENV means dev/test mode, not production."""
        _clear_env(monkeypatch)
        from grocery_butler.app import _is_production

        assert _is_production() is False

    def test_is_production_false_when_other_value(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """A non-production APP_ENV value (e.g. "staging") is not production."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", "staging")
        from grocery_butler.app import _is_production

        assert _is_production() is False


# ---------------------------------------------------------------------------
# Production: missing FLASK_SECRET_KEY fails fast
# ---------------------------------------------------------------------------


class TestProductionMissingFlaskSecretKey:
    """Production startup must fail fast when FLASK_SECRET_KEY is unset."""

    def test_create_app_production_missing_flask_secret_key_raises(
        self, monkeypatch: pytest.MonkeyPatch, db_path: str
    ) -> None:
        """Production with no FLASK_SECRET_KEY raises RuntimeError naming it."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv(SECRET_ENV_VAR, "shared-secret")

        with pytest.raises(RuntimeError, match="FLASK_SECRET_KEY"):
            create_app(db_path=db_path)

    def test_create_app_production_empty_flask_secret_key_raises(
        self, monkeypatch: pytest.MonkeyPatch, db_path: str
    ) -> None:
        """Production with an empty-string FLASK_SECRET_KEY also raises."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("FLASK_SECRET_KEY", "")
        monkeypatch.setenv(SECRET_ENV_VAR, "shared-secret")

        with pytest.raises(RuntimeError, match="FLASK_SECRET_KEY"):
            create_app(db_path=db_path)


# ---------------------------------------------------------------------------
# Production: missing RUBOTPAUL_SHARED_SECRET fails fast
# ---------------------------------------------------------------------------


class TestProductionMissingSharedSecret:
    """Production startup must fail fast when RUBOTPAUL_SHARED_SECRET is unset.

    This is expected to surface the existing
    ``auth_middleware._shared_secret()`` RuntimeError, not a new one, so the
    match target is the same ``SECRET_ENV_VAR`` name that function already
    raises against.
    """

    def test_create_app_production_missing_shared_secret_raises(
        self, monkeypatch: pytest.MonkeyPatch, db_path: str
    ) -> None:
        """Production with FLASK_SECRET_KEY set but no shared secret raises."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("FLASK_SECRET_KEY", "prod-secret-value")

        with pytest.raises(RuntimeError, match=SECRET_ENV_VAR):
            create_app(db_path=db_path)

    def test_create_app_production_empty_shared_secret_raises(
        self, monkeypatch: pytest.MonkeyPatch, db_path: str
    ) -> None:
        """Production with an empty-string shared secret also raises."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("FLASK_SECRET_KEY", "prod-secret-value")
        monkeypatch.setenv(SECRET_ENV_VAR, "")

        with pytest.raises(RuntimeError, match=SECRET_ENV_VAR):
            create_app(db_path=db_path)


# ---------------------------------------------------------------------------
# Dev/test mode: missing FLASK_SECRET_KEY is tolerated but warned about
# ---------------------------------------------------------------------------


class TestDevModeMissingFlaskSecretKey:
    """Dev/test mode tolerates a missing FLASK_SECRET_KEY but warns loudly."""

    def test_dev_mode_missing_flask_secret_key_boots_with_random_key(
        self, monkeypatch: pytest.MonkeyPatch, db_path: str
    ) -> None:
        """No APP_ENV and no FLASK_SECRET_KEY still boots with a non-empty key."""
        _clear_env(monkeypatch)

        app = create_app(db_path=db_path)

        assert app.config["SECRET_KEY"]

    def test_dev_mode_missing_flask_secret_key_logs_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        db_path: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A missing FLASK_SECRET_KEY logs a warning naming it and sessions."""
        _clear_env(monkeypatch)
        caplog.set_level(logging.WARNING, logger="grocery_butler.app")

        create_app(db_path=db_path)

        messages = _warning_messages(caplog)
        assert any(
            "FLASK_SECRET_KEY" in message and "session" in message.lower()
            for message in messages
        )


# ---------------------------------------------------------------------------
# Dev/test mode: missing RUBOTPAUL_SHARED_SECRET is tolerated at boot
# ---------------------------------------------------------------------------


class TestDevModeMissingSharedSecret:
    """Dev/test mode boots without RUBOTPAUL_SHARED_SECRET; auth still 401s."""

    def test_dev_mode_missing_shared_secret_registers_api_blueprint(
        self, monkeypatch: pytest.MonkeyPatch, db_path: str
    ) -> None:
        """No shared secret in dev mode still creates the app with /api/v1."""
        _clear_env(monkeypatch)

        app = create_app(db_path=db_path)

        assert "api_v1" in app.blueprints

    def test_dev_mode_missing_shared_secret_unauthenticated_get_401(
        self, monkeypatch: pytest.MonkeyPatch, db_path: str
    ) -> None:
        """An unauthenticated GET to /api/v1 still returns 401 without a secret."""
        _clear_env(monkeypatch)
        app = create_app(db_path=db_path)
        client = app.test_client()

        response = client.get("/api/v1/inventory")

        assert response.status_code == 401


# ---------------------------------------------------------------------------
# FLASK_SECRET_KEY, when set, is used verbatim in any mode
# ---------------------------------------------------------------------------


class TestFlaskSecretKeyFromEnv:
    """When FLASK_SECRET_KEY is set, config SECRET_KEY must match it exactly."""

    def test_flask_secret_key_env_used_exactly_in_dev(
        self, monkeypatch: pytest.MonkeyPatch, db_path: str
    ) -> None:
        """A set FLASK_SECRET_KEY becomes SECRET_KEY verbatim in dev mode."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("FLASK_SECRET_KEY", "dev-secret-value")

        app = create_app(db_path=db_path)

        assert app.config["SECRET_KEY"] == "dev-secret-value"

    def test_flask_secret_key_env_used_exactly_in_production(
        self, monkeypatch: pytest.MonkeyPatch, db_path: str
    ) -> None:
        """A set FLASK_SECRET_KEY becomes SECRET_KEY verbatim in production too."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("FLASK_SECRET_KEY", "prod-secret-value")
        monkeypatch.setenv(SECRET_ENV_VAR, "shared-secret")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-test")

        app = create_app(db_path=db_path)

        assert app.config["SECRET_KEY"] == "prod-secret-value"


# ---------------------------------------------------------------------------
# ANTHROPIC_API_KEY is warn-only in every mode
# ---------------------------------------------------------------------------


class TestAnthropicApiKeyWarnOnly:
    """A missing ANTHROPIC_API_KEY only warns at startup; it never blocks boot."""

    def test_missing_anthropic_api_key_logs_startup_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        db_path: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """No ANTHROPIC_API_KEY logs a prominent warning naming the var."""
        _clear_env(monkeypatch)
        caplog.set_level(logging.WARNING, logger="grocery_butler.app")

        create_app(db_path=db_path)

        messages = _warning_messages(caplog)
        assert any("ANTHROPIC_API_KEY" in message for message in messages)

    def test_present_anthropic_api_key_logs_no_warning(
        self,
        monkeypatch: pytest.MonkeyPatch,
        db_path: str,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """A configured ANTHROPIC_API_KEY suppresses the startup warning."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-configured")
        caplog.set_level(logging.WARNING, logger="grocery_butler.app")

        create_app(db_path=db_path)

        messages = _warning_messages(caplog)
        assert not any("ANTHROPIC_API_KEY" in message for message in messages)


# ---------------------------------------------------------------------------
# Production with every required secret present succeeds
# ---------------------------------------------------------------------------


class TestProductionAllSecretsSetSucceeds:
    """Production startup with every required secret set must succeed."""

    def test_create_app_production_all_secrets_set_succeeds(
        self, monkeypatch: pytest.MonkeyPatch, db_path: str
    ) -> None:
        """create_app() returns normally when all prod secrets are present."""
        _clear_env(monkeypatch)
        monkeypatch.setenv("APP_ENV", "production")
        monkeypatch.setenv("FLASK_SECRET_KEY", "prod-secret-value")
        monkeypatch.setenv(SECRET_ENV_VAR, "shared-secret")
        monkeypatch.setenv("ANTHROPIC_API_KEY", "sk-ant-configured")

        app = create_app(db_path=db_path)

        assert app.config["SECRET_KEY"] == "prod-secret-value"


# ---------------------------------------------------------------------------
# .env.example must document the vars the startup checks depend on
# ---------------------------------------------------------------------------


class TestEnvExampleGuardsProductionConfig:
    """.env.example must document every var create_app()'s checks need."""

    def test_env_example_documents_flask_secret_key(self) -> None:
        """FLASK_SECRET_KEY must be documented for local/prod parity."""
        assert _env_example_has_key("FLASK_SECRET_KEY")

    def test_env_example_documents_database_url(self) -> None:
        """DATABASE_URL must be documented alongside DATABASE_PATH."""
        assert _env_example_has_key("DATABASE_URL")

    def test_env_example_documents_app_env(self) -> None:
        """APP_ENV must be documented so devs know how to opt into prod checks."""
        assert _env_example_has_key("APP_ENV")

    def test_env_example_still_documents_anthropic_api_key(self) -> None:
        """The pre-existing ANTHROPIC_API_KEY line must not regress."""
        assert _env_example_has_key("ANTHROPIC_API_KEY")

    def test_env_example_still_documents_shared_secret(self) -> None:
        """The pre-existing RUBOTPAUL_SHARED_SECRET line must not regress."""
        assert _env_example_has_key("RUBOTPAUL_SHARED_SECRET")


# ---------------------------------------------------------------------------
# Dockerfile must default the runtime stage to production
# ---------------------------------------------------------------------------


class TestDockerfileSetsProductionAppEnv:
    """The runtime image must default APP_ENV to production."""

    def test_dockerfile_runtime_stage_sets_app_env_production(self) -> None:
        """An ENV instruction must set APP_ENV=production in the runtime stage."""
        pattern = re.compile(r"^ENV\s+.*\bAPP_ENV=production\b")
        lines = DOCKERFILE.read_text().splitlines()
        assert any(pattern.search(line.strip()) for line in lines)
