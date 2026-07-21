"""Database initialization and connection utilities for MealBot.

Issue #78: ``init_db()`` is called from every store constructor
(``RecipeStore``, ``PantryManager``, ``ShoppingListStore``,
``OrderSubmissionStore``) as well as at process-startup entry points
(``cli.py``, ``bot.py``, ``safeway_pipeline.py``, ``app.py``). Without
a run-once guard, each of those calls re-ran the full migration
check-then-apply sequence, which is wasteful and — under concurrent
gunicorn workers or concurrent in-process store construction — a
source of the migration race reproduced in ``tests/test_migrate.py``.
``init_db()`` now runs ``migrate()`` at most once per ``db_path`` per
process: a lock-free fast path short-circuits already-initialized
paths (opening no connection at all), and a ``threading.Lock``-guarded
slow path handles the first call for a given path. The per-process
guard is deliberately layered on top of, not instead of,
``migrate()``'s own cross-process lock (see
:mod:`grocery_butler.db.migrate`): the two together cover both
concurrent threads within one process and concurrent gunicorn worker
processes.
"""

from __future__ import annotations

import threading
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

# Issue #78 run-once guard: serializes the slow (first-call) path of
# init_db() for a given process, and the set of db_paths that have
# already completed a successful migrate() call in this process.
_init_lock = threading.Lock()
_initialized_paths: set[str] = set()

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
    migrations in version order.

    Issue #78: runs ``migrate()`` at most once per ``db_path`` per
    process. A lock-free fast path returns immediately (opening no
    connection) if ``db_path`` was already initialized. Otherwise, a
    ``threading.Lock``-guarded slow path double-checks membership,
    opens the ``:memory:`` keepalive connection on first use, and only
    records ``db_path`` as initialized *after* ``migrate()`` succeeds —
    a failed migration must not poison the cache and silently skip
    retry on the next call.

    For ``:memory:`` databases, opens a keepalive connection so that
    the shared in-memory database persists across connections.

    Args:
        db_path: Path to the database file or a database URL.
    """
    if db_path in _initialized_paths:
        return

    global _memory_keepalive
    with _init_lock:
        if db_path in _initialized_paths:
            return

        if db_path == ":memory:" and _memory_keepalive is None:
            _memory_keepalive = get_connection(db_path)

        from grocery_butler.db.migrate import migrate

        migrate(db_path)

        _initialized_paths.add(db_path)


def _reset_init_state() -> None:
    """Reset the run-once init registry and close the memory keepalive.

    Test-only hook (Issue #78). Module-level run-once state (
    ``_initialized_paths`` and ``_memory_keepalive``) would otherwise
    persist for the lifetime of the Python process, including across
    tests in the same pytest session, letting one test's ``init_db()``
    call silently short-circuit an unrelated later test's call to the
    same path. See ``tests/conftest.py``'s autouse fixture, which calls
    this before and after every test.
    """
    global _memory_keepalive
    _initialized_paths.clear()
    if _memory_keepalive is not None:
        _memory_keepalive.close()
        _memory_keepalive = None
