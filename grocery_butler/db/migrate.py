"""Database migration runner for grocery-butler.

Discovers numbered SQL migration files in the ``migrations/`` directory,
tracks which have been applied in a ``schema_migrations`` table, and
runs only the pending ones in order.

Supports both SQLite (``NNN_name.sql``) and PostgreSQL
(``NNN_name_pg.sql``) dialects.

Usage::

    python -m grocery_butler.db.migrate          # uses DATABASE_URL or default
    python -m grocery_butler.db.migrate /path/to/db.sqlite
"""

from __future__ import annotations

import argparse
import importlib
import logging
import os
import re
import sys
from pathlib import Path
from typing import TYPE_CHECKING

from grocery_butler.db import get_connection

if TYPE_CHECKING:
    from collections.abc import Callable

    from grocery_butler.db.adapter import DatabaseConnection

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_MIGRATION_RE = re.compile(r"^(\d{3})_(.+?)(?:_pg)?\.sql$")


def _is_postgres(db_path: str) -> bool:
    """Check if the database path is a PostgreSQL URL.

    Args:
        db_path: Database path or URL.

    Returns:
        True if the path is a PostgreSQL URL.
    """
    return db_path.startswith(("postgresql://", "postgres://"))


def _ensure_schema_migrations_table(conn: DatabaseConnection) -> None:
    """Create the schema_migrations tracking table if it does not exist.

    The DDL is compatible with both SQLite and PostgreSQL (INTEGER
    PRIMARY KEY works on both backends).

    Args:
        conn: Active database connection.
    """
    sql = (
        "CREATE TABLE IF NOT EXISTS schema_migrations ("
        "version INTEGER PRIMARY KEY, "
        "name TEXT NOT NULL, "
        "applied_at TIMESTAMP DEFAULT CURRENT_TIMESTAMP)"
    )
    conn.executescript(sql)


def _get_applied_versions(conn: DatabaseConnection) -> set[int]:
    """Read already-applied migration version numbers.

    Args:
        conn: Active database connection.

    Returns:
        Set of applied version numbers.
    """
    cursor = conn.execute("SELECT version FROM schema_migrations")
    return {row["version"] for row in cursor.fetchall()}


def _discover_migrations(is_pg: bool) -> list[tuple[int, str, Path]]:
    """Scan the migrations directory for SQL files matching the dialect.

    Files are named ``NNN_name.sql`` (SQLite) or ``NNN_name_pg.sql``
    (PostgreSQL).  Only files matching the requested dialect are returned.

    Args:
        is_pg: Whether to select PostgreSQL dialect files.

    Returns:
        Sorted list of ``(version, name, path)`` tuples.
    """
    results: list[tuple[int, str, Path]] = []

    for path in MIGRATIONS_DIR.iterdir():
        if not path.is_file():
            continue
        # Skip files of the wrong dialect
        if is_pg and not path.name.endswith("_pg.sql"):
            continue
        if not is_pg and path.name.endswith("_pg.sql"):
            continue
        if path.suffix != ".sql":
            continue

        match = _MIGRATION_RE.match(path.name)
        if match:
            version = int(match.group(1))
            name = match.group(2)
            results.append((version, name, path))

    results.sort(key=lambda t: t[0])
    return results


def _record_migration(conn: DatabaseConnection, version: int, name: str) -> None:
    """Record a migration as applied in the tracking table.

    Uses ``?`` placeholders which the adapter layer translates to
    ``%s`` for PostgreSQL (see :meth:`PostgresConnection.execute`).

    The explicit ``RETURNING version`` prevents the PostgreSQL adapter
    from injecting ``RETURNING id`` (schema_migrations has no ``id``
    column; its primary key is ``version``).

    Args:
        conn: Active database connection.
        version: Migration version number.
        name: Migration name.
    """
    result = conn.execute(
        "INSERT INTO schema_migrations (version, name) VALUES (?, ?) RETURNING version",
        (version, name),
    )
    result.fetchall()  # drain RETURNING so commit sees no open cursor
    conn.commit()


def _discover_hook(version: int, name: str) -> Callable[[str], None] | None:
    """Discover a Python migration hook by file-naming convention.

    A migration may have an associated Python hook for logic that
    cannot be expressed in SQL alone (e.g. data transforms). Convention:
    a hook lives alongside its SQL file as ``NNN_name.py`` under
    ``migrations/`` and must expose a ``migrate(db_path: str) -> None``
    callable. If no such sibling file exists, there is no hook for this
    version/name pair.

    Args:
        version: Migration version number.
        name: Migration name, as parsed from its SQL filename.

    Returns:
        The hook module's ``migrate`` callable, or None if no matching
        ``NNN_name.py`` file exists in ``migrations/``.
    """
    hook_path = MIGRATIONS_DIR / f"{version:03d}_{name}.py"
    if not hook_path.is_file():
        return None

    module = importlib.import_module(
        f"grocery_butler.db.migrations.{version:03d}_{name}"
    )
    hook: Callable[[str], None] = module.migrate
    return hook


def _run_python_hook(version: int, name: str, db_path: str) -> None:
    """Run a Python migration hook if one is discovered for this version.

    Hooks are discovered by convention via :func:`_discover_hook`: a
    version with a sibling ``NNN_name.py`` file in ``migrations/`` gets
    its ``migrate`` callable invoked with ``db_path``; a version with no
    such file is a no-op.

    Args:
        version: Migration version number.
        name: Migration name (for logging).
        db_path: Database file path or PostgreSQL URL.
    """
    hook = _discover_hook(version, name)
    if hook is None:
        return

    logger.info("Running Python hook for %03d_%s ...", version, name)
    hook(db_path)


def migrate(db_path: str) -> int:
    """Apply all pending migrations to the database.

    Creates the schema_migrations tracking table if needed, discovers
    SQL migration files for the appropriate dialect, and applies any
    that haven't been run yet in version order.

    Args:
        db_path: Database file path or PostgreSQL URL.

    Returns:
        Number of migrations applied.
    """
    is_pg = _is_postgres(db_path)
    conn = get_connection(db_path)
    try:
        _ensure_schema_migrations_table(conn)
        applied = _get_applied_versions(conn)
        migrations = _discover_migrations(is_pg)

        count = 0
        for version, name, path in migrations:
            if version in applied:
                logger.debug(
                    "Skipping migration %03d_%s (already applied)",
                    version,
                    name,
                )
                continue

            logger.info("Applying migration %03d_%s ...", version, name)
            sql = path.read_text()
            conn.executescript(sql)
            _run_python_hook(version, name, db_path)
            _record_migration(conn, version, name)
            count += 1

        if count == 0:
            logger.info("Database is up to date.")
        else:
            logger.info("Applied %d migration(s).", count)

        return count
    finally:
        conn.close()


def _build_parser() -> argparse.ArgumentParser:
    """Build the CLI argument parser.

    Returns:
        Configured ArgumentParser.
    """
    parser = argparse.ArgumentParser(description="Apply pending database migrations.")
    parser.add_argument(
        "db_path",
        nargs="?",
        default=None,
        help="Database path or URL (defaults to DATABASE_URL env var).",
    )
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
    db_path = args.db_path or os.environ.get("DATABASE_URL", "mealbot.db")
    migrate(db_path)
    return 0


if __name__ == "__main__":
    sys.exit(main())
