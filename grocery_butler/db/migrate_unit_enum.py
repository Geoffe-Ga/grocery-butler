"""Migration script for normalizing unit string fields to valid Unit enum values.

Reads existing rows in ``recipe_ingredients`` and ``household_inventory``
and rewrites any ``unit`` / ``default_unit`` column values through
:func:`~grocery_butler.models.parse_unit` so that they match a valid
:class:`~grocery_butler.models.Unit` member.

Usage::

    python -m grocery_butler.db.migrate_unit_enum /path/to/grocery_butler.db

The script is idempotent: running it multiple times on an already-migrated
database is safe.
"""

from __future__ import annotations

import argparse
import logging
import sys

from grocery_butler.db import get_connection
from grocery_butler.models import parse_unit

logger = logging.getLogger(__name__)


def _migrate_recipe_ingredients(db_path: str) -> int:
    """Normalize unit values in the recipe_ingredients table.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        Number of rows updated.
    """
    conn = get_connection(db_path)
    updated = 0
    try:
        rows = conn.execute("SELECT id, unit FROM recipe_ingredients").fetchall()
        for row in rows:
            row_id: int = row["id"]
            raw_unit: str = row["unit"]
            normalized = parse_unit(raw_unit).value
            if normalized != raw_unit:
                conn.execute(
                    "UPDATE recipe_ingredients SET unit = ? WHERE id = ?",
                    (normalized, row_id),
                )
                updated += 1
                logger.debug(
                    "recipe_ingredients id=%d: %r -> %r", row_id, raw_unit, normalized
                )
        conn.commit()
    finally:
        conn.close()
    return updated


def _migrate_household_inventory(db_path: str) -> int:
    """Normalize default_unit values in the household_inventory table.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        Number of rows updated.
    """
    conn = get_connection(db_path)
    updated = 0
    try:
        rows = conn.execute(
            "SELECT id, default_unit FROM household_inventory"
        ).fetchall()
        for row in rows:
            row_id: int = row["id"]
            raw_unit: str | None = row["default_unit"]
            if raw_unit is None:
                continue
            normalized = parse_unit(raw_unit).value
            if normalized != raw_unit:
                conn.execute(
                    "UPDATE household_inventory SET default_unit = ? WHERE id = ?",
                    (normalized, row_id),
                )
                updated += 1
                logger.debug(
                    "household_inventory id=%d: %r -> %r",
                    row_id,
                    raw_unit,
                    normalized,
                )
        conn.commit()
    finally:
        conn.close()
    return updated


def migrate(db_path: str) -> None:
    """Run all unit-enum migrations against the given database.

    Args:
        db_path: Path to the SQLite database file.
    """
    ri_count = _migrate_recipe_ingredients(db_path)
    hi_count = _migrate_household_inventory(db_path)
    logger.info(
        "Migration complete: %d recipe_ingredients row(s) updated, "
        "%d household_inventory row(s) updated.",
        ri_count,
        hi_count,
    )


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        Configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(
        description="Normalize unit columns to valid Unit enum values."
    )
    parser.add_argument("db_path", help="Path to the SQLite database file.")
    parser.add_argument(
        "-v",
        "--verbose",
        action="store_true",
        help="Enable DEBUG logging.",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    """Entry point for the migration CLI.

    Args:
        argv: Command-line arguments (defaults to sys.argv).

    Returns:
        Exit code (0 on success).
    """
    args = _build_parser().parse_args(argv)
    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(levelname)s: %(message)s",
    )
    migrate(args.db_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
