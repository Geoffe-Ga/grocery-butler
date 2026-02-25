"""Tests for grocery_butler.db.migrate_unit_enum migration script."""

from __future__ import annotations

import argparse
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from grocery_butler.db import get_connection, init_db
from grocery_butler.db.migrate_unit_enum import (
    _build_parser,
    _migrate_household_inventory,
    _migrate_recipe_ingredients,
    main,
    migrate,
)
from grocery_butler.models import Unit

# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------


def _seed_recipe(db_path: str, name: str = "test_recipe") -> int:
    """Insert a recipe and return its id.

    Args:
        db_path: Path to the SQLite database file.
        name: Unique recipe name.

    Returns:
        The new recipe row id.
    """
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            "INSERT INTO recipes (name, display_name) VALUES (?, ?)",
            (name, name),
        )
        assert cursor.lastrowid is not None
        row_id: int = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()
    return row_id


def _seed_recipe_ingredient(db_path: str, recipe_id: int, unit: str) -> int:
    """Insert a recipe ingredient and return its id.

    Args:
        db_path: Path to the SQLite database file.
        recipe_id: FK to recipes.id.
        unit: Unit string value to store.

    Returns:
        The new recipe_ingredient row id.
    """
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            "INSERT INTO recipe_ingredients "
            "(recipe_id, ingredient, quantity, unit, category, quantity_per_serving) "
            "VALUES (?, ?, ?, ?, ?, ?)",
            (recipe_id, "flour", 2.0, unit, "pantry_dry", 0.5),
        )
        assert cursor.lastrowid is not None
        row_id: int = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()
    return row_id


def _seed_inventory_item(
    db_path: str, ingredient: str, default_unit: str | None
) -> int:
    """Insert a household_inventory row and return its id.

    Args:
        db_path: Path to the SQLite database file.
        ingredient: Unique ingredient name.
        default_unit: Unit string or None.

    Returns:
        The new household_inventory row id.
    """
    conn = get_connection(db_path)
    try:
        cursor = conn.execute(
            "INSERT INTO household_inventory (ingredient, display_name, default_unit) "
            "VALUES (?, ?, ?)",
            (ingredient, ingredient.title(), default_unit),
        )
        assert cursor.lastrowid is not None
        row_id: int = cursor.lastrowid
        conn.commit()
    finally:
        conn.close()
    return row_id


def _fetch_recipe_ingredient_unit(db_path: str, row_id: int) -> str:
    """Fetch the unit value for a recipe_ingredients row.

    Args:
        db_path: Path to the SQLite database file.
        row_id: The row id to fetch.

    Returns:
        Current unit string.
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT unit FROM recipe_ingredients WHERE id = ?", (row_id,)
        ).fetchone()
        return str(row["unit"])
    finally:
        conn.close()


def _fetch_inventory_default_unit(db_path: str, row_id: int) -> str | None:
    """Fetch the default_unit value for a household_inventory row.

    Args:
        db_path: Path to the SQLite database file.
        row_id: The row id to fetch.

    Returns:
        Current default_unit string, or None.
    """
    conn = get_connection(db_path)
    try:
        row = conn.execute(
            "SELECT default_unit FROM household_inventory WHERE id = ?", (row_id,)
        ).fetchone()
        value = row["default_unit"]
        return str(value) if value is not None else None
    finally:
        conn.close()


# ---------------------------------------------------------------------------
# Tests for _migrate_recipe_ingredients
# ---------------------------------------------------------------------------


class TestMigrateRecipeIngredients:
    """Tests for _migrate_recipe_ingredients helper."""

    def test_normalizes_alias(self, tmp_path: Path) -> None:
        """Test alias unit strings are normalized."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        recipe_id = _seed_recipe(db_path)
        row_id = _seed_recipe_ingredient(db_path, recipe_id, "lbs")

        count = _migrate_recipe_ingredients(db_path)

        assert count == 1
        assert _fetch_recipe_ingredient_unit(db_path, row_id) == Unit.LB.value

    def test_already_normalized_not_counted(self, tmp_path: Path) -> None:
        """Test already-valid unit strings produce zero updates."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        recipe_id = _seed_recipe(db_path)
        _seed_recipe_ingredient(db_path, recipe_id, "lb")

        count = _migrate_recipe_ingredients(db_path)

        assert count == 0

    def test_multiple_rows_partially_migrated(self, tmp_path: Path) -> None:
        """Test that only non-normalized rows are counted."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        recipe_id = _seed_recipe(db_path)
        _seed_recipe_ingredient(db_path, recipe_id, "lb")
        _seed_recipe_ingredient(db_path, recipe_id, "pounds")

        count = _migrate_recipe_ingredients(db_path)

        assert count == 1

    def test_empty_table_returns_zero(self, tmp_path: Path) -> None:
        """Test migration on an empty table returns zero updates."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)

        count = _migrate_recipe_ingredients(db_path)

        assert count == 0


# ---------------------------------------------------------------------------
# Tests for _migrate_household_inventory
# ---------------------------------------------------------------------------


class TestMigrateHouseholdInventory:
    """Tests for _migrate_household_inventory helper."""

    def test_normalizes_alias(self, tmp_path: Path) -> None:
        """Test alias default_unit strings are normalized."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        row_id = _seed_inventory_item(db_path, "milk", "gallon")

        count = _migrate_household_inventory(db_path)

        assert count == 1
        assert _fetch_inventory_default_unit(db_path, row_id) == Unit.GAL.value

    def test_none_default_unit_skipped(self, tmp_path: Path) -> None:
        """Test rows with NULL default_unit are not modified."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        row_id = _seed_inventory_item(db_path, "salt", None)

        count = _migrate_household_inventory(db_path)

        assert count == 0
        assert _fetch_inventory_default_unit(db_path, row_id) is None

    def test_already_normalized_not_counted(self, tmp_path: Path) -> None:
        """Test already-valid default_unit strings produce zero updates."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        _seed_inventory_item(db_path, "oil", "bottle")

        count = _migrate_household_inventory(db_path)

        assert count == 0

    def test_empty_table_returns_zero(self, tmp_path: Path) -> None:
        """Test migration on an empty table returns zero updates."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)

        count = _migrate_household_inventory(db_path)

        assert count == 0


# ---------------------------------------------------------------------------
# Tests for migrate (integration)
# ---------------------------------------------------------------------------


class TestMigrate:
    """Integration tests for the migrate function."""

    def test_migrate_runs_both_tables(self, tmp_path: Path) -> None:
        """Test migrate updates both recipe_ingredients and household_inventory."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        recipe_id = _seed_recipe(db_path)
        ri_id = _seed_recipe_ingredient(db_path, recipe_id, "cups")
        hi_id = _seed_inventory_item(db_path, "flour", "pounds")

        migrate(db_path)

        # recipe_ingredients: "cups" is already canonical
        assert _fetch_recipe_ingredient_unit(db_path, ri_id) == Unit.CUP.value
        # household_inventory: "pounds" -> "lb"
        assert _fetch_inventory_default_unit(db_path, hi_id) == Unit.LB.value

    def test_migrate_is_idempotent(self, tmp_path: Path) -> None:
        """Test running migrate twice does not corrupt data."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)
        recipe_id = _seed_recipe(db_path)
        ri_id = _seed_recipe_ingredient(db_path, recipe_id, "lbs")

        migrate(db_path)
        migrate(db_path)

        assert _fetch_recipe_ingredient_unit(db_path, ri_id) == Unit.LB.value


# ---------------------------------------------------------------------------
# Tests for CLI entry point
# ---------------------------------------------------------------------------


class TestMain:
    """Tests for the main CLI entry point."""

    def test_main_exits_zero(self, tmp_path: Path) -> None:
        """Test main returns 0 on success."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)

        exit_code = main([db_path])

        assert exit_code == 0

    def test_main_verbose_flag(self, tmp_path: Path) -> None:
        """Test main accepts --verbose flag."""
        db_path = str(tmp_path / "test.db")
        init_db(db_path)

        exit_code = main([db_path, "--verbose"])

        assert exit_code == 0


class TestBuildParser:
    """Tests for _build_parser."""

    def test_parser_returns_argparser(self) -> None:
        """Test _build_parser returns an ArgumentParser."""
        parser = _build_parser()
        assert isinstance(parser, argparse.ArgumentParser)

    def test_parser_requires_db_path(self) -> None:
        """Test that omitting db_path causes a parse error."""
        parser = _build_parser()
        with pytest.raises(SystemExit):
            parser.parse_args([])
