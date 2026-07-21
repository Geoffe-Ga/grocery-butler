"""Database migration runner for grocery-butler.

Discovers numbered SQL migration files in the ``migrations/`` directory,
tracks which have been applied in a ``schema_migrations`` table, and
runs only the pending ones in order.

Supports both SQLite (``NNN_name.sql``) and PostgreSQL
(``NNN_name_pg.sql``) dialects.

Issue #78: because grocery-butler runs behind multiple gunicorn worker
processes (and, in-process, every store constructor triggers a schema
check via :func:`grocery_butler.db.init_db`), several ``migrate()``
calls can race against a fresh database. :func:`migrate` guards its
check-then-apply sequence with a cross-process lock, held for the
duration of table-creation, applied-version reading, and application:
a PostgreSQL session-level advisory lock (``pg_advisory_lock``) taken
on the same connection used for the migration work, or — for a
file-backed SQLite database — an exclusive ``fcntl.flock`` on a
``<db_path>.migratelock`` sidecar file. ``":memory:"`` databases take
neither lock: they are process-local, and
:mod:`grocery_butler.db`'s in-process ``_init_lock`` already prevents
concurrent callers within a single process.

Usage::

    python -m grocery_butler.db.migrate          # uses DATABASE_URL or default
    python -m grocery_butler.db.migrate /path/to/db.sqlite
"""

from __future__ import annotations

import argparse
import contextlib
import fcntl
import importlib
import logging
import os
import re
import sys
import zlib
from pathlib import Path
from typing import TYPE_CHECKING

from grocery_butler.db import get_connection
from grocery_butler.db.adapter import IntegrityError

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from grocery_butler.db.adapter import DatabaseConnection

logger = logging.getLogger(__name__)

MIGRATIONS_DIR = Path(__file__).parent / "migrations"

_MIGRATION_RE = re.compile(r"^(\d{3})_(.+?)(?:_pg)?\.sql$")

# Fixed advisory-lock key for migrate()'s cross-process PostgreSQL lock
# (Issue #78). Derived once, at import time, from a constant identifier
# string — never from user input or request data — so every process
# that imports this module computes the identical key. A 32-bit CRC is
# more than sufficient: this lock only needs to be unique among locks
# *this application* takes, not globally unique across every process
# sharing the database.
_MIGRATION_LOCK_KEY: int = zlib.crc32(b"grocery_butler:schema_migrations")


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

    Issue #78: a caller can lose the migration-lock acquisition race
    against another process (e.g. a PostgreSQL advisory lock is
    session-scoped, not visible to a process that hasn't yet opened its
    connection) and re-read "not yet applied" for a version a concurrent
    ``migrate()`` call already recorded. A duplicate-version insert then
    raises :data:`~grocery_butler.db.adapter.IntegrityError` (the
    ``version`` primary key rejects the second row); that is tolerated
    here as "already applied" rather than propagated. ``conn.commit()``
    always runs afterward — for SQLite this simply commits an empty
    transaction, and for PostgreSQL it clears the aborted-transaction
    state left by the failed insert (PostgreSQL treats a ``COMMIT`` of
    an aborted transaction as an implicit rollback, with no error) so
    the connection remains usable for the rest of ``migrate()``.

    Args:
        conn: Active database connection.
        version: Migration version number.
        name: Migration name.
    """
    try:
        result = conn.execute(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?) "
            "RETURNING version",
            (version, name),
        )
        result.fetchall()  # drain RETURNING so commit sees no open cursor
    except IntegrityError:
        logger.info(
            "Migration %03d already recorded by a concurrent process; skipping.",
            version,
        )
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


def _acquire_postgres_lock(conn: DatabaseConnection) -> None:
    """Take the session-level PostgreSQL advisory lock for migrate().

    Args:
        conn: Active PostgreSQL connection. The lock is session-scoped,
            so it must be released (see :func:`_release_postgres_lock`)
            on this same connection.
    """
    conn.execute("SELECT pg_advisory_lock(?)", (_MIGRATION_LOCK_KEY,))


def _release_postgres_lock(conn: DatabaseConnection) -> None:
    """Release the session-level PostgreSQL advisory lock for migrate().

    Args:
        conn: The same connection :func:`_acquire_postgres_lock` was
            called on.
    """
    conn.execute("SELECT pg_advisory_unlock(?)", (_MIGRATION_LOCK_KEY,))


def _lock_file_path(db_path: str) -> str:
    """Return the sidecar lock-file path for a file-backed SQLite database.

    Args:
        db_path: Path to the SQLite database file.

    Returns:
        ``db_path`` with a ``.migratelock`` suffix appended.
    """
    return f"{db_path}.migratelock"


@contextlib.contextmanager
def _postgres_migration_lock(conn: DatabaseConnection) -> Iterator[None]:
    """Hold a session-level PostgreSQL advisory lock for migrate().

    The lock is taken and released on ``conn`` itself, since PostgreSQL
    session-level advisory locks are scoped to the session that
    acquired them. Always released, including when the guarded block
    raises.

    Args:
        conn: The PostgreSQL connection that will perform the migration
            work.

    Yields:
        None. Control returns to the caller for the duration of the
        lock.
    """
    _acquire_postgres_lock(conn)
    try:
        yield
    finally:
        _release_postgres_lock(conn)


@contextlib.contextmanager
def _sqlite_file_lock(db_path: str) -> Iterator[None]:
    """Hold a cross-process file lock guarding a SQLite database (Issue #78).

    File-backed SQLite: an exclusive ``fcntl.flock`` on a
    ``<db_path>.migratelock`` sidecar file, released on exit.
    ``":memory:"``: no lock. It is process-local, and
    :mod:`grocery_butler.db`'s in-process ``_init_lock`` already
    prevents concurrent callers within a single process.

    Deliberately takes only ``db_path``, not a connection: unlike the
    PostgreSQL advisory lock, this lock does not need to be held on the
    connection that performs the migration work, which lets callers
    acquire it *before* opening that connection at all. That ordering
    matters — opening a SQLite connection to a brand-new database file
    briefly needs SQLite's own internal exclusive lock (e.g. to convert
    the fresh file to WAL journal mode), and several callers opening
    connections to the same fresh file at once can transiently contend
    for that internal lock and raise a spurious "database is locked"
    error, even though none of them have reached any schema work yet.
    Acquiring this lock first closes that race entirely: only the
    caller holding the lock ever has a connection open to ``db_path``.

    Args:
        db_path: SQLite database file path, or ``":memory:"``.

    Yields:
        None. Control returns to the caller for the duration of the
        lock.
    """
    if db_path == ":memory:":
        yield
        return

    with open(_lock_file_path(db_path), "a", encoding="utf-8") as lock_file:
        fcntl.flock(lock_file.fileno(), fcntl.LOCK_EX)
        try:
            yield
        finally:
            fcntl.flock(lock_file.fileno(), fcntl.LOCK_UN)


def _apply_pending(
    conn: DatabaseConnection,
    migrations: list[tuple[int, str, Path]],
    applied: set[int],
    db_path: str,
) -> int:
    """Apply migrations not yet recorded in ``applied``, in order.

    Args:
        conn: Active database connection (already holding the
            migration lock for the duration of the caller's critical
            section).
        migrations: All discovered migrations for this dialect, sorted
            by version.
        applied: Versions already recorded in schema_migrations.
        db_path: Database file path or PostgreSQL URL, passed through
            to Python hooks.

    Returns:
        Number of migrations applied.
    """
    count = 0
    for version, name, path in migrations:
        if version in applied:
            logger.debug("Skipping migration %03d_%s (already applied)", version, name)
            continue

        logger.info("Applying migration %03d_%s ...", version, name)
        sql = path.read_text()
        conn.executescript(sql)
        _run_python_hook(version, name, db_path)
        _record_migration(conn, version, name)
        count += 1

    return count


def _run_migrations(conn: DatabaseConnection, db_path: str, *, is_pg: bool) -> int:
    """Run the full check-then-apply sequence on an already-locked connection.

    Creates the schema_migrations tracking table if needed, discovers
    SQL migration files for the appropriate dialect, and applies any
    that haven't been run yet in version order. Callers are expected to
    invoke this only while already holding the appropriate migration
    lock (see :func:`_postgres_migration_lock` /
    :func:`_sqlite_file_lock`), so that concurrent callers targeting
    the same database are serialized instead of racing to double-apply
    a migration.

    Args:
        conn: Active, lock-protected database connection.
        db_path: Database file path or PostgreSQL URL.
        is_pg: Whether ``db_path`` is a PostgreSQL URL.

    Returns:
        Number of migrations applied.
    """
    _ensure_schema_migrations_table(conn)
    applied = _get_applied_versions(conn)
    migrations = _discover_migrations(is_pg)
    count = _apply_pending(conn, migrations, applied, db_path)

    if count == 0:
        logger.info("Database is up to date.")
    else:
        logger.info("Applied %d migration(s).", count)

    return count


def _migrate_postgres(db_path: str) -> int:
    """Run migrate() against a PostgreSQL target.

    Opens the connection first (the advisory lock is scoped to it),
    then locks and runs the migration sequence on that same connection.

    Args:
        db_path: PostgreSQL connection URL.

    Returns:
        Number of migrations applied.
    """
    conn = get_connection(db_path)
    try:
        with _postgres_migration_lock(conn):
            return _run_migrations(conn, db_path, is_pg=True)
    finally:
        conn.close()


def _migrate_sqlite(db_path: str) -> int:
    """Run migrate() against a SQLite target (file-backed or ``:memory:``).

    Issue #78: acquires :func:`_sqlite_file_lock` *before* opening the
    database connection, unlike the PostgreSQL path — see that lock's
    docstring for why connection setup itself must happen under the
    lock for SQLite.

    Args:
        db_path: SQLite database file path, or ``":memory:"``.

    Returns:
        Number of migrations applied.
    """
    with _sqlite_file_lock(db_path):
        conn = get_connection(db_path)
        try:
            return _run_migrations(conn, db_path, is_pg=False)
        finally:
            conn.close()


def migrate(db_path: str) -> int:
    """Apply all pending migrations to the database.

    Dispatches to a dialect-specific helper (:func:`_migrate_postgres`
    or :func:`_migrate_sqlite`) that acquires the appropriate
    cross-process lock — a PostgreSQL session-level advisory lock or a
    ``fcntl.flock`` on a SQLite sidecar file — around the full
    check-then-apply sequence, so concurrent callers targeting the same
    database are serialized instead of racing to double-apply a
    migration (Issue #78).

    Args:
        db_path: Database file path or PostgreSQL URL.

    Returns:
        Number of migrations applied.
    """
    if _is_postgres(db_path):
        return _migrate_postgres(db_path)
    return _migrate_sqlite(db_path)


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
