"""Database initialization and connection utilities for MealBot."""

from __future__ import annotations

import sqlite3
from pathlib import Path

SCHEMA_PATH = Path(__file__).parent / "schema.sql"

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

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        Configured sqlite3.Connection.
    """
    conn = sqlite3.connect(db_path)
    conn.execute("PRAGMA journal_mode=WAL")
    conn.execute("PRAGMA foreign_keys=ON")
    conn.row_factory = sqlite3.Row
    return conn


def init_db(db_path: str) -> None:
    """Initialize the database schema and seed data.

    Idempotent: safe to call multiple times. Uses IF NOT EXISTS for
    all table creation and INSERT OR IGNORE for seed data.

    Args:
        db_path: Path to the SQLite database file.
    """
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
