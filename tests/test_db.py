"""Tests for grocery_butler.db module."""

from __future__ import annotations

import sqlite3
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from grocery_butler.db import (
    DEFAULT_PANTRY,
    DEFAULT_PREFERENCES,
    SCHEMA_PATH,
    get_connection,
    init_db,
)


class TestConstants:
    """Tests for module-level constants."""

    def test_schema_path_exists(self) -> None:
        """Test that SCHEMA_PATH points to an existing file."""
        assert SCHEMA_PATH.exists()
        assert SCHEMA_PATH.name == "schema.sql"

    def test_default_pantry_not_empty(self) -> None:
        """Test DEFAULT_PANTRY has seed data."""
        assert len(DEFAULT_PANTRY) > 0

    def test_default_pantry_items_are_tuples(self) -> None:
        """Test each pantry item is a (name, category) tuple."""
        for item in DEFAULT_PANTRY:
            assert len(item) == 2
            name, category = item
            assert isinstance(name, str)
            assert isinstance(category, str)
            assert len(name) > 0
            assert len(category) > 0

    def test_default_preferences_not_empty(self) -> None:
        """Test DEFAULT_PREFERENCES has seed data."""
        assert len(DEFAULT_PREFERENCES) > 0
        assert "default_servings" in DEFAULT_PREFERENCES
        assert "default_units" in DEFAULT_PREFERENCES


class TestGetConnection:
    """Tests for get_connection function."""

    def test_returns_connection(self, tmp_path: Path) -> None:
        """Test get_connection returns a sqlite3 Connection."""
        db_path = str(tmp_path / "test.db")
        conn = get_connection(db_path)
        try:
            assert isinstance(conn, sqlite3.Connection)
        finally:
            conn.close()

    def test_enables_wal_mode(self, tmp_path: Path) -> None:
        """Test WAL journal mode is enabled."""
        db_path = str(tmp_path / "test.db")
        conn = get_connection(db_path)
        try:
            result = conn.execute("PRAGMA journal_mode").fetchone()
            assert result[0] == "wal"
        finally:
            conn.close()

    def test_enables_foreign_keys(self, tmp_path: Path) -> None:
        """Test foreign key enforcement is enabled."""
        db_path = str(tmp_path / "test.db")
        conn = get_connection(db_path)
        try:
            result = conn.execute("PRAGMA foreign_keys").fetchone()
            assert result[0] == 1
        finally:
            conn.close()

    def test_sets_row_factory(self, tmp_path: Path) -> None:
        """Test row_factory is set to sqlite3.Row."""
        db_path = str(tmp_path / "test.db")
        conn = get_connection(db_path)
        try:
            assert conn.row_factory is sqlite3.Row
        finally:
            conn.close()


class TestInitDb:
    """Tests for init_db function."""

    def test_creates_all_tables(self, tmp_path: Path) -> None:
        """Test init_db creates all expected tables."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)

        conn = get_connection(db_path)
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = [row["name"] for row in cursor.fetchall()]
        finally:
            conn.close()

        expected = [
            "brand_preferences",
            "household_inventory",
            "pantry_staples",
            "preferences",
            "product_mapping",
            "recipe_ingredients",
            "recipes",
        ]
        for table in expected:
            assert table in tables, f"Missing table: {table}"

    def test_seeds_pantry_staples(self, tmp_path: Path) -> None:
        """Test init_db inserts default pantry staples."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)

        conn = get_connection(db_path)
        try:
            cursor = conn.execute("SELECT COUNT(*) as cnt FROM pantry_staples")
            count = cursor.fetchone()["cnt"]
        finally:
            conn.close()

        assert count == len(DEFAULT_PANTRY)

    def test_seeds_default_preferences(self, tmp_path: Path) -> None:
        """Test init_db inserts default preferences."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)

        conn = get_connection(db_path)
        try:
            cursor = conn.execute("SELECT COUNT(*) as cnt FROM preferences")
            count = cursor.fetchone()["cnt"]
        finally:
            conn.close()

        assert count == len(DEFAULT_PREFERENCES)

    def test_preferences_values_correct(self, tmp_path: Path) -> None:
        """Test seeded preference values match DEFAULT_PREFERENCES."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)

        conn = get_connection(db_path)
        try:
            cursor = conn.execute("SELECT key, value FROM preferences")
            rows = {row["key"]: row["value"] for row in cursor.fetchall()}
        finally:
            conn.close()

        for key, value in DEFAULT_PREFERENCES.items():
            assert rows[key] == value

    def test_pantry_staple_display_names(self, tmp_path: Path) -> None:
        """Test pantry staple display_name is title-cased."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)

        conn = get_connection(db_path)
        try:
            cursor = conn.execute("SELECT ingredient, display_name FROM pantry_staples")
            rows = {row["ingredient"]: row["display_name"] for row in cursor.fetchall()}
        finally:
            conn.close()

        assert rows["salt"] == "Salt"
        assert rows["black pepper"] == "Black Pepper"
        assert rows["olive oil"] == "Olive Oil"

    def test_idempotent_double_init(self, tmp_path: Path) -> None:
        """Test init_db is safe to call twice (idempotent)."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        init_db(db_path)  # Should not raise

        conn = get_connection(db_path)
        try:
            cursor = conn.execute("SELECT COUNT(*) as cnt FROM pantry_staples")
            count = cursor.fetchone()["cnt"]
        finally:
            conn.close()

        # Should still have the same number (INSERT OR IGNORE)
        assert count == len(DEFAULT_PANTRY)

    def test_creates_indexes(self, tmp_path: Path) -> None:
        """Test init_db creates expected indexes."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)

        conn = get_connection(db_path)
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name NOT LIKE 'sqlite_%'"
            )
            indexes = [row["name"] for row in cursor.fetchall()]
        finally:
            conn.close()

        assert "idx_household_inventory_status" in indexes
        assert "idx_product_mapping_ingredient" in indexes

    def test_foreign_keys_enforced(self, tmp_path: Path) -> None:
        """Test that foreign key constraints are active after init_db."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)

        conn = get_connection(db_path)
        try:
            # Inserting a recipe_ingredient with a non-existent recipe_id should fail
            with pytest.raises(sqlite3.IntegrityError):
                conn.execute(
                    "INSERT INTO recipe_ingredients "
                    "(recipe_id, ingredient, quantity, unit, category, "
                    "quantity_per_serving) "
                    "VALUES (9999, 'flour', 2.0, 'cups', 'pantry_dry', 0.5)"
                )
        finally:
            conn.close()
