"""Tests for grocery_butler.config module."""

from __future__ import annotations

import os
from decimal import Decimal
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from grocery_butler.config import Config, ConfigError, load_config


class TestConfig:
    """Tests for the Config dataclass."""

    def test_config_defaults(self) -> None:
        """Test Config uses correct default values."""
        cfg = Config(anthropic_api_key="sk-test")
        assert cfg.discord_bot_token == ""
        assert cfg.database_path == "mealbot.db"
        assert cfg.database_url == ""
        assert cfg.flask_port == 5000
        assert cfg.flask_debug is False
        assert cfg.default_servings == 4
        assert cfg.default_units == "imperial"
        # Issue #60: order submission must be off by default (fail-safe gate).
        assert cfg.safeway_order_submission_enabled is False

    def test_config_all_fields(self) -> None:
        """Test Config accepts all fields."""
        cfg = Config(
            anthropic_api_key="sk-test",
            discord_bot_token="tok-123",
            database_path="/tmp/test.db",
            flask_port=8080,
            flask_debug=True,
            default_servings=2,
            default_units="metric",
            safeway_order_submission_enabled=True,
        )
        assert cfg.anthropic_api_key == "sk-test"
        assert cfg.discord_bot_token == "tok-123"
        assert cfg.database_path == "/tmp/test.db"
        assert cfg.flask_port == 8080
        assert cfg.flask_debug is True
        assert cfg.default_servings == 2
        assert cfg.default_units == "metric"
        assert cfg.safeway_order_submission_enabled is True

    def test_config_is_frozen(self) -> None:
        """Test Config is immutable (frozen dataclass)."""
        cfg = Config(anthropic_api_key="sk-test")
        with pytest.raises(AttributeError):
            cfg.anthropic_api_key = "other"  # type: ignore[misc]


class TestConfigError:
    """Tests for ConfigError exception."""

    def test_config_error_is_exception(self) -> None:
        """Test ConfigError is a proper exception."""
        err = ConfigError("missing key")
        assert str(err) == "missing key"
        assert isinstance(err, Exception)


class TestLoadConfig:
    """Tests for load_config function."""

    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": "sk-test-key"}, clear=True)
    def test_load_config_minimal(self) -> None:
        """Test load_config with only required env var."""
        cfg = load_config()
        assert cfg.anthropic_api_key == "sk-test-key"
        assert cfg.flask_port == 5000
        assert cfg.default_servings == 4

    @patch("grocery_butler.config.load_dotenv")
    @patch.dict(os.environ, {}, clear=True)
    def test_load_config_missing_api_key_raises(self, _mock_dotenv: object) -> None:
        """Test load_config raises ConfigError when ANTHROPIC_API_KEY missing."""
        with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY is required"):
            load_config()

    @patch("grocery_butler.config.load_dotenv")
    @patch.dict(os.environ, {"ANTHROPIC_API_KEY": ""}, clear=True)
    def test_load_config_empty_api_key_raises(self, _mock_dotenv: object) -> None:
        """Test load_config raises ConfigError when ANTHROPIC_API_KEY is empty."""
        with pytest.raises(ConfigError, match="ANTHROPIC_API_KEY is required"):
            load_config()

    @patch.dict(
        os.environ,
        {"ANTHROPIC_API_KEY": "sk-test", "FLASK_PORT": "not_a_number"},
        clear=True,
    )
    def test_load_config_invalid_flask_port_raises(self) -> None:
        """Test load_config raises ConfigError for non-integer FLASK_PORT."""
        with pytest.raises(ConfigError, match="FLASK_PORT must be an integer"):
            load_config()

    @patch.dict(
        os.environ,
        {"ANTHROPIC_API_KEY": "sk-test", "DEFAULT_SERVINGS": "abc"},
        clear=True,
    )
    def test_load_config_invalid_servings_raises(self) -> None:
        """Test load_config raises ConfigError for non-integer DEFAULT_SERVINGS."""
        with pytest.raises(ConfigError, match="DEFAULT_SERVINGS must be an integer"):
            load_config()

    @patch.dict(
        os.environ,
        {
            "ANTHROPIC_API_KEY": "sk-full",
            "DISCORD_BOT_TOKEN": "bot-tok",
            "DATABASE_PATH": "/data/meals.db",
            "FLASK_PORT": "9090",
            "FLASK_DEBUG": "true",
            "DEFAULT_SERVINGS": "6",
            "DEFAULT_UNITS": "metric",
        },
        clear=True,
    )
    def test_load_config_all_env_vars(self) -> None:
        """Test load_config reads all environment variables correctly."""
        cfg = load_config()
        assert cfg.anthropic_api_key == "sk-full"
        assert cfg.discord_bot_token == "bot-tok"
        assert cfg.database_path == "/data/meals.db"
        assert cfg.flask_port == 9090
        assert cfg.flask_debug is True
        assert cfg.default_servings == 6
        assert cfg.default_units == "metric"

    @patch.dict(
        os.environ,
        {"ANTHROPIC_API_KEY": "sk-test", "FLASK_DEBUG": "1"},
        clear=True,
    )
    def test_load_config_flask_debug_truthy_1(self) -> None:
        """Test FLASK_DEBUG=1 is treated as True."""
        cfg = load_config()
        assert cfg.flask_debug is True

    @patch.dict(
        os.environ,
        {"ANTHROPIC_API_KEY": "sk-test", "FLASK_DEBUG": "yes"},
        clear=True,
    )
    def test_load_config_flask_debug_truthy_yes(self) -> None:
        """Test FLASK_DEBUG=yes is treated as True."""
        cfg = load_config()
        assert cfg.flask_debug is True

    @patch.dict(
        os.environ,
        {"ANTHROPIC_API_KEY": "sk-test", "FLASK_DEBUG": "FALSE"},
        clear=True,
    )
    def test_load_config_flask_debug_case_insensitive(self) -> None:
        """Test FLASK_DEBUG comparison is case-insensitive."""
        cfg = load_config()
        assert cfg.flask_debug is False

    @patch.dict(
        os.environ,
        {"ANTHROPIC_API_KEY": "sk-test", "FLASK_DEBUG": "TRUE"},
        clear=True,
    )
    def test_load_config_flask_debug_uppercase_true(self) -> None:
        """Test FLASK_DEBUG=TRUE is treated as True."""
        cfg = load_config()
        assert cfg.flask_debug is True

    def test_load_config_from_env_file(self, tmp_path: Path) -> None:
        """Test load_config reads from a .env file."""
        env_file = tmp_path / ".env"
        env_file.write_text("ANTHROPIC_API_KEY=sk-from-file\n")
        with patch.dict(os.environ, {}, clear=True):
            cfg = load_config(env_path=env_file)
        assert cfg.anthropic_api_key == "sk-from-file"

    @patch.dict(
        os.environ,
        {
            "ANTHROPIC_API_KEY": "sk-test",
            "DATABASE_URL": "postgresql://user:pass@host:5432/db",
        },
        clear=True,
    )
    def test_load_config_database_url(self) -> None:
        """Test load_config reads DATABASE_URL environment variable."""
        cfg = load_config()
        assert cfg.database_url == "postgresql://user:pass@host:5432/db"

    @patch.dict(
        os.environ,
        {"ANTHROPIC_API_KEY": "sk-test"},
        clear=True,
    )
    def test_load_config_database_url_defaults_empty(self) -> None:
        """Test database_url defaults to empty string when not set."""
        cfg = load_config()
        assert cfg.database_url == ""

    @patch.dict(
        os.environ,
        {"ANTHROPIC_API_KEY": "sk-test"},
        clear=True,
    )
    def test_load_config_safeway_order_submission_unset_defaults_false(self) -> None:
        """Test Issue #60 gate defaults to False when the env var is unset."""
        cfg = load_config()
        assert cfg.safeway_order_submission_enabled is False

    @pytest.mark.parametrize(
        ("raw_value", "expected"),
        [
            ("true", True),
            ("1", True),
            ("yes", True),
            ("TRUE", True),
            ("Yes", True),
            ("false", False),
            ("0", False),
            ("garbage", False),
            ("", False),
        ],
    )
    def test_load_config_safeway_order_submission_enabled_parsing(
        self, raw_value: str, expected: bool
    ) -> None:
        """Test SAFEWAY_ORDER_SUBMISSION_ENABLED uses the flask_debug truthy parse."""
        env = {
            "ANTHROPIC_API_KEY": "sk-test",
            "SAFEWAY_ORDER_SUBMISSION_ENABLED": raw_value,
        }
        with patch.dict(os.environ, env, clear=True):
            cfg = load_config()
        assert cfg.safeway_order_submission_enabled is expected


class TestOrderValueCap:
    """Tests for the Issue #73 order-value cap config field.

    ``Config.safeway_order_value_cap`` does not exist yet, so reading it
    below raises ``AttributeError`` — the acceptable RED for a
    not-yet-existing config field.
    """

    def test_order_value_cap_defaults_to_300(self) -> None:
        """Test Config.safeway_order_value_cap defaults to $300."""
        cfg = Config(anthropic_api_key="sk-test")
        assert cfg.safeway_order_value_cap == Decimal("300")

    @patch.dict(
        os.environ,
        {
            "ANTHROPIC_API_KEY": "sk-test",
            "SAFEWAY_ORDER_VALUE_CAP_USD": "150.50",
        },
        clear=True,
    )
    def test_order_value_cap_reads_env(self) -> None:
        """Test load_config parses SAFEWAY_ORDER_VALUE_CAP_USD as a Decimal."""
        cfg = load_config()
        assert cfg.safeway_order_value_cap == Decimal("150.50")

    @pytest.mark.parametrize("raw_value", ["abc", "-5"])
    def test_order_value_cap_invalid_raises_config_error(self, raw_value: str) -> None:
        """Test a non-numeric or negative cap value raises ConfigError.

        Today load_config performs no validation at all on this env var
        (it isn't even read), so this currently fails with "DID NOT
        RAISE" rather than a real ConfigError.
        """
        env = {
            "ANTHROPIC_API_KEY": "sk-test",
            "SAFEWAY_ORDER_VALUE_CAP_USD": raw_value,
        }
        with (
            patch.dict(os.environ, env, clear=True),
            pytest.raises(ConfigError, match="SAFEWAY_ORDER_VALUE_CAP_USD"),
        ):
            load_config()
