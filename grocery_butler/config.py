"""Configuration loading and validation for MealBot.

Loads settings from .env via python-dotenv. Validates required vars
at import time so the app fails fast with a clear error.
"""

from __future__ import annotations

import os
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

from dotenv import load_dotenv

if TYPE_CHECKING:
    from pathlib import Path


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


@dataclass(frozen=True)
class Config:
    """Typed, validated application configuration."""

    # Secrets are kept out of repr(), so logging a Config can't leak them.

    # Required for Claude API calls
    anthropic_api_key: str = field(repr=False)

    # Required for Discord bot (optional if only running web/CLI)
    discord_bot_token: str = field(default="", repr=False)

    # Database. The URL can embed a password.
    database_path: str = "mealbot.db"
    database_url: str = field(default="", repr=False)

    # Flask
    flask_port: int = 5000
    flask_debug: bool = False

    # Meal planning defaults
    default_servings: int = 4
    default_units: str = "imperial"  # "imperial" or "metric"


def _int_env(name: str, default: str) -> int:
    """Read an integer environment variable.

    Args:
        name: Environment variable name.
        default: Value used when the variable is unset.

    Returns:
        The parsed integer.

    Raises:
        ConfigError: If the value is not an integer.
    """
    raw = os.getenv(name, default)
    try:
        return int(raw)
    except ValueError as err:
        msg = f"{name} must be an integer, got: {raw!r}"
        raise ConfigError(msg) from err


def load_config(env_path: str | Path | None = None) -> Config:
    """Load and validate configuration from environment / .env file.

    Args:
        env_path: Optional path to .env file. If None, searches from cwd upward.

    Returns:
        Validated Config instance.

    Raises:
        ConfigError: If required configuration is missing.
    """
    load_dotenv(dotenv_path=env_path)

    anthropic_api_key = os.getenv("ANTHROPIC_API_KEY", "")
    if not anthropic_api_key:
        msg = (
            "ANTHROPIC_API_KEY is required. "
            "Copy .env.example to .env and fill in your key."
        )
        raise ConfigError(msg)

    flask_port = _int_env("FLASK_PORT", "5000")
    default_servings = _int_env("DEFAULT_SERVINGS", "4")

    database_url = os.getenv("DATABASE_URL", "")

    return Config(
        anthropic_api_key=anthropic_api_key,
        discord_bot_token=os.getenv("DISCORD_BOT_TOKEN", ""),
        database_path=os.getenv("DATABASE_PATH", "mealbot.db"),
        database_url=database_url,
        flask_port=flask_port,
        flask_debug=os.getenv("FLASK_DEBUG", "false").lower() in ("true", "1", "yes"),
        default_servings=default_servings,
        default_units=os.getenv("DEFAULT_UNITS", "imperial"),
    )
