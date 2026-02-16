"""Database initialization and connection utilities for MealBot."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

# Keeps the shared-cache :memory: database alive between connections.
# Without this, the in-memory DB is destroyed when init_db closes its
# connection and no other connections exist.
_memory_keepalive: sqlite3.Connection | None = None

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


def get_connection(db_path: str) -> sqlite3.Connection:
    """Create a SQLite connection with WAL mode and foreign keys enabled.

    Uses shared-cache URI mode for ``:memory:`` databases so that
    multiple connections share the same in-memory database.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        Configured sqlite3.Connection.
    """
    if db_path == ":memory:":
        conn = sqlite3.connect("file::memory:?cache=shared", uri=True)
    else:
        conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str) -> None:
    """Initialize the database schema and seed data.

    Idempotent: safe to call multiple times. Uses IF NOT EXISTS for
    all table creation and INSERT OR IGNORE for seed data.

    For ``:memory:`` databases, opens a keepalive connection so that
    the shared in-memory database persists across connections.

    Args:
        db_path: Path to the SQLite database file.
    """
    global _memory_keepalive
    if db_path == ":memory:" and _memory_keepalive is None:
        _memory_keepalive = get_connection(db_path)

    conn = get_connection(db_path)
    try:
        # Create tables from schema.sql
        schema_sql = SCHEMA_PATH.read_text()
        conn.executescript(schema_sql)

        # Seed pantry staples
        for ingredient, category in DEFAULT_PANTRY:
            display_name = ingredient.replace("_", " ").title()
            conn.execute(
                "INSERT OR IGNORE INTO pantry_staples "
                "(ingredient, display_name, category) VALUES (?, ?, ?)",
                (ingredient, display_name, category),
            )

        # Seed default preferences
        for key, value in DEFAULT_PREFERENCES.items():
            conn.execute(
                "INSERT OR IGNORE INTO preferences (key, value) VALUES (?, ?)",
                (key, value),
            )

        conn.commit()
    finally:
        conn.close()
