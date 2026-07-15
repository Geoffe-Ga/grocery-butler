"""Configuration loading and validation for MealBot.

Loads settings from .env via python-dotenv. Validates required vars
at import time so the app fails fast with a clear error.
"""

from __future__ import annotations

import os
from dataclasses import dataclass
from decimal import Decimal, InvalidOperation
from typing import TYPE_CHECKING

from dotenv import load_dotenv

if TYPE_CHECKING:
    from pathlib import Path


class ConfigError(Exception):
    """Raised when required configuration is missing or invalid."""


#: Issue #73: default Safeway order-value cap when
#: ``SAFEWAY_ORDER_VALUE_CAP_USD`` is not set.
DEFAULT_ORDER_VALUE_CAP = Decimal("300")


def parse_order_value_cap(raw: str) -> Decimal:
    """Parse and validate the Safeway order-value cap (Issue #73).

    Shared by :func:`load_config` and callers (such as the API layer)
    that need the cap without loading full application configuration
    (e.g. without requiring ``ANTHROPIC_API_KEY``) — single source of
    truth for the parsing/validation rules.

    Args:
        raw: Raw string value of ``SAFEWAY_ORDER_VALUE_CAP_USD``.

    Returns:
        The parsed, non-negative cap as a Decimal.

    Raises:
        ConfigError: If ``raw`` is not a valid decimal amount or is
            negative.
    """
    try:
        cap = Decimal(raw)
    except InvalidOperation as err:
        raise ConfigError(
            "SAFEWAY_ORDER_VALUE_CAP_USD must be a valid decimal amount, "
            f"got: {raw!r}"
        ) from err
    if cap < 0:
        raise ConfigError(
            f"SAFEWAY_ORDER_VALUE_CAP_USD must not be negative, got: {raw!r}"
        )
    return cap


@dataclass(frozen=True)
class Config:
    """Typed, validated application configuration."""

    # Required for Claude API calls
    anthropic_api_key: str

    # Required for Discord bot (optional if only running web/CLI)
    discord_bot_token: str = ""

    # Database
    database_path: str = "mealbot.db"
    database_url: str = ""

    # Flask
    flask_port: int = 5000
    flask_debug: bool = False

    # Meal planning defaults
    default_servings: int = 4
    default_units: str = "imperial"  # "imperial" or "metric"

    # Safeway integration (optional — only needed for Phase 3)
    safeway_username: str = ""
    safeway_password: str = ""
    safeway_store_id: str = ""

    # Issue #60: order submission is descoped for v1.0 (unverified checkout
    # API surface). Fail-safe default is off; opt in explicitly once the
    # live Safeway checkout flow has been verified.
    safeway_order_submission_enabled: bool = False

    # Issue #73: order-value cap. Orders whose fee-inclusive total
    # exceeds this amount are blocked unless explicitly overridden.
    safeway_order_value_cap: Decimal = DEFAULT_ORDER_VALUE_CAP


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
        raise ConfigError(
            "ANTHROPIC_API_KEY is required. "
            "Copy .env.example to .env and fill in your key."
        )

    flask_port_raw = os.getenv("FLASK_PORT", "5000")
    try:
        flask_port = int(flask_port_raw)
    except ValueError as err:
        raise ConfigError(
            f"FLASK_PORT must be an integer, got: {flask_port_raw!r}"
        ) from err

    default_servings_raw = os.getenv("DEFAULT_SERVINGS", "4")
    try:
        default_servings = int(default_servings_raw)
    except ValueError as err:
        raise ConfigError(
            f"DEFAULT_SERVINGS must be an integer, got: {default_servings_raw!r}"
        ) from err

    database_url = os.getenv("DATABASE_URL", "")

    order_value_cap_raw = os.getenv("SAFEWAY_ORDER_VALUE_CAP_USD", "300")
    safeway_order_value_cap = parse_order_value_cap(order_value_cap_raw)

    return Config(
        anthropic_api_key=anthropic_api_key,
        discord_bot_token=os.getenv("DISCORD_BOT_TOKEN", ""),
        database_path=os.getenv("DATABASE_PATH", "mealbot.db"),
        database_url=database_url,
        flask_port=flask_port,
        flask_debug=os.getenv("FLASK_DEBUG", "false").lower() in ("true", "1", "yes"),
        default_servings=default_servings,
        default_units=os.getenv("DEFAULT_UNITS", "imperial"),
        safeway_username=os.getenv("SAFEWAY_USERNAME", ""),
        safeway_password=os.getenv("SAFEWAY_PASSWORD", ""),
        safeway_store_id=os.getenv("SAFEWAY_STORE_ID", ""),
        safeway_order_submission_enabled=os.getenv(
            "SAFEWAY_ORDER_SUBMISSION_ENABLED", "false"
        ).lower()
        in ("true", "1", "yes"),
        safeway_order_value_cap=safeway_order_value_cap,
    )
