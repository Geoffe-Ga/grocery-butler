"""Database initialization and connection utilities for MealBot."""

from __future__ import annotations

from pathlib import Path
from typing import TYPE_CHECKING

from grocery_butler.db.adapter import create_connection

if TYPE_CHECKING:
    from grocery_butler.db.adapter import DatabaseConnection

SCHEMA_PATH = Path(__file__).parent / "schema.sql"
SCHEMA_PG_PATH = Path(__file__).parent / "schema_pg.sql"

# Keeps the shared-cache :memory: database alive between connections.
# Without this, the in-memory DB is destroyed when init_db closes its
# connection and no other connections exist.
_memory_keepalive: DatabaseConnection | None = None

DEFAULT_PANTRY: list[tuple[str, str]] = [
    ("salt", "pantry_dry"),
    ("black pepper", "pantry_dry"),
    ("olive oil", "pantry_dry"),
    ("vegetable oil", "pantry_dry"),
    ("butter", "dairy"),
    ("garlic", "produce"),
    ("onion", "produce"),
    ("sugar", "pantry_dry"),
    ("flour", "pantry_dry"),
    ("soy sauce", "pantry_dry"),
]

DEFAULT_PREFERENCES: dict[str, str] = {
    "default_servings": "4",
    "default_units": "imperial",
}


def get_connection(db_path: str) -> DatabaseConnection:
    """Create a database connection with appropriate settings.

    Uses shared-cache URI mode for ``:memory:`` databases so that
    multiple connections share the same in-memory database.
    Routes to the correct backend via the adapter layer.

    Args:
        db_path: Path to the database file or a database URL.

    Returns:
        Configured DatabaseConnection.
    """
    return create_connection(db_path)


def init_db(db_path: str) -> None:
    """Initialize the database schema and seed data via migrations.

    Delegates to the migration runner which applies any pending SQL
    migrations in version order. Idempotent: safe to call multiple
    times.

    For ``:memory:`` databases, opens a keepalive connection so that
    the shared in-memory database persists across connections.

    Args:
        db_path: Path to the database file or a database URL.
    """
    global _memory_keepalive
    if db_path == ":memory:" and _memory_keepalive is None:
        _memory_keepalive = get_connection(db_path)

    from grocery_butler.db.migrate import migrate

    migrate(db_path)
