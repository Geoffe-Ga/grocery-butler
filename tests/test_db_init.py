"""Tests for grocery_butler.db's run-once init_db guard (Issue #78).

Multiple gunicorn worker processes race to boot against the same
database, and every store constructor (``RecipeStore``, ``PantryManager``,
``ShoppingListStore``, ``OrderSubmissionStore``) also calls
``init_db(db_path)`` at construction time. Before the Issue #78 fix,
``init_db()`` unconditionally re-runs ``migrate()`` on every call --
wasteful at best, and a source of the concurrent first-boot race
reproduced in ``tests/test_migrate.py`` at worst.

These tests pin the run-once guard's contract:

* ``migrate()`` runs at most once per ``db_path`` within a process.
* The fast path (a ``db_path`` already initialized) opens no database
  connection at all.
* ``_reset_init_state()`` (a test-only hook) clears the registry so a
  ``db_path`` can be reinitialized.
* The ``:memory:`` keepalive connection is opened exactly once, and
  ``migrate()`` likewise runs exactly once for ``:memory:`` under the
  run-once guard.
"""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

import grocery_butler.db as db_module
import grocery_butler.db.migrate as migrate_module
from grocery_butler.db import init_db
from grocery_butler.order_submissions import OrderSubmissionStore
from grocery_butler.pantry_manager import PantryManager
from grocery_butler.recipe_store import RecipeStore
from grocery_butler.shopping_list_store import ShoppingListStore

if TYPE_CHECKING:
    from pathlib import Path

    from grocery_butler.db.adapter import DatabaseConnection

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a temporary database path for test isolation."""
    return str(tmp_path / "test_db_init.db")


# ---------------------------------------------------------------------------
# init_db() runs migrate() at most once per db_path
# ---------------------------------------------------------------------------


class TestInitDbRunsOnce:
    """Tests that init_db() invokes migrate() at most once per db_path."""

    def test_init_db_runs_migrate_only_once_per_path(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test 5 repeated init_db(path) calls invoke migrate() exactly once.

        Guards Issue #78's core defect: init_db() currently has no
        run-once registry, so it re-runs the full migration check-then-
        apply sequence on every call -- wasteful at best and a source of
        the concurrent-boot race at worst.
        """
        mock_migrate = MagicMock(return_value=0)
        monkeypatch.setattr(migrate_module, "migrate", mock_migrate)

        for _ in range(5):
            init_db(db_path)

        mock_migrate.assert_called_once_with(db_path)

    def test_store_construction_after_init_db_does_not_invoke_migration_runner(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test constructing 4 stores after init_db() never re-invokes migrate().

        This is Issue #78 acceptance criterion #1: once a process has
        initialized a db_path, every store built against that same path
        (``RecipeStore``, ``PantryManager``, ``ShoppingListStore``,
        ``OrderSubmissionStore`` -- each of which calls ``init_db()``
        itself in its constructor) must not trigger another migration
        run.
        """
        init_db(db_path)

        mock_migrate = MagicMock(return_value=0)
        monkeypatch.setattr(migrate_module, "migrate", mock_migrate)

        RecipeStore(db_path)
        PantryManager(db_path)
        ShoppingListStore(db_path)
        OrderSubmissionStore(db_path)

        mock_migrate.assert_not_called()

    def test_init_db_fast_path_opens_no_connection(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test a second init_db(path) call opens no database connection.

        After the first (real) init_db() call, every symbol that could
        open a connection -- ``grocery_butler.db.get_connection`` (used
        directly by init_db for the ``:memory:`` keepalive) and
        ``grocery_butler.db.migrate.get_connection`` (used internally by
        migrate()) -- is replaced with a function that raises. The
        second call must take the run-once fast path and touch neither.
        """
        init_db(db_path)

        def _raise(_path: str) -> None:
            raise AssertionError("init_db fast path must not open a connection")

        monkeypatch.setattr(db_module, "get_connection", _raise)
        monkeypatch.setattr(migrate_module, "get_connection", _raise)

        init_db(db_path)


# ---------------------------------------------------------------------------
# Failed migrations must not poison the run-once cache
# ---------------------------------------------------------------------------


class TestInitDbFailurePropagation:
    """Tests that a failed migrate() does not poison the run-once cache."""

    def test_failed_migration_does_not_poison_run_once_cache(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test a raising migrate() leaves init_db(path) retryable.

        Pins the documented Issue #78 contract that ``db_path`` is
        recorded as initialized only *after* ``migrate()`` succeeds: if
        the first ``init_db(path)`` call fails, the failure must
        propagate, the path must not enter the run-once registry, and a
        second ``init_db(path)`` call must re-invoke ``migrate()``
        rather than silently short-circuiting on a broken schema.
        """
        calls: list[str] = []

        def _flaky_migrate(path: str) -> int:
            calls.append(path)
            if len(calls) == 1:
                raise RuntimeError("simulated migration failure")
            return 0

        monkeypatch.setattr(migrate_module, "migrate", _flaky_migrate)

        with pytest.raises(RuntimeError, match="simulated migration failure"):
            init_db(db_path)

        assert db_path not in db_module._initialized_paths, (
            "a failed migrate() must not mark the path as initialized"
        )

        init_db(db_path)

        assert calls == [db_path, db_path], (
            "the second init_db() call must re-invoke migrate()"
        )
        assert db_path in db_module._initialized_paths


# ---------------------------------------------------------------------------
# _reset_init_state() test hook
# ---------------------------------------------------------------------------


class TestResetInitState:
    """Tests for the ``_reset_init_state()`` test hook."""

    def test_reset_init_state_forces_reinit(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test _reset_init_state() clears the run-once registry.

        After a reset, a previously-initialized db_path is treated as
        brand new again: the next init_db() call must re-invoke
        migrate().
        """
        calls: list[str] = []

        def _fake_migrate(path: str) -> int:
            calls.append(path)
            return 0

        monkeypatch.setattr(migrate_module, "migrate", _fake_migrate)

        init_db(db_path)
        db_module._reset_init_state()
        init_db(db_path)

        assert calls == [db_path, db_path]


# ---------------------------------------------------------------------------
# :memory: keepalive + run-once semantics
# ---------------------------------------------------------------------------


class TestMemoryKeepalive:
    """Tests for :memory: run-once semantics (keepalive + migrate)."""

    def test_memory_keepalive_opened_once(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test repeated init_db(":memory:") opens the keepalive once.

        Forces a clean starting state for the shared
        ``_memory_keepalive`` global (module-level state that would
        otherwise leak between tests), then asserts two properties
        across two ``init_db(":memory:")`` calls: the keepalive
        connection is opened exactly once (already true on current
        code -- pre-existing behavior, not itself part of Issue #78),
        and ``migrate()`` is invoked exactly once (the Issue #78
        run-once guard; currently invoked on every call). Finally
        checks the schema is actually usable afterwards.
        """
        monkeypatch.setattr(db_module, "_memory_keepalive", None)

        real_get_connection = db_module.get_connection
        connection_calls: list[str] = []

        def _counting_get_connection(path: str) -> DatabaseConnection:
            connection_calls.append(path)
            return real_get_connection(path)

        monkeypatch.setattr(db_module, "get_connection", _counting_get_connection)

        real_migrate = migrate_module.migrate
        migrate_calls: list[str] = []

        def _counting_migrate(path: str) -> int:
            migrate_calls.append(path)
            return real_migrate(path)

        monkeypatch.setattr(migrate_module, "migrate", _counting_migrate)

        init_db(":memory:")
        init_db(":memory:")

        assert connection_calls == [":memory:"], "keepalive must open exactly once"
        assert migrate_calls == [":memory:"], "migrate() must run exactly once"

        conn = real_get_connection(":memory:")
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' AND name='recipes'"
            )
            assert cursor.fetchone() is not None
        finally:
            conn.close()
