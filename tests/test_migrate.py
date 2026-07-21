"""Tests for grocery_butler.db.migrate module."""

from __future__ import annotations

import contextlib
import fcntl
import importlib
import threading
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

import grocery_butler.db.migrate as migrate_module

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence
    from pathlib import Path

    from grocery_butler.db.adapter import DatabaseConnection

from grocery_butler.db import get_connection
from grocery_butler.db.migrate import (
    MIGRATIONS_DIR,
    _discover_hook,
    _discover_migrations,
    _ensure_schema_migrations_table,
    _get_applied_versions,
    _record_migration,
    _run_python_hook,
    main,
    migrate,
)
from grocery_butler.models import Unit
from grocery_butler.recipe_store import RecipeStore

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a temporary database path for test isolation."""
    return str(tmp_path / "test_migrate.db")


# ---------------------------------------------------------------------------
# Concurrency test helpers (Issue #78)
# ---------------------------------------------------------------------------


def _run_concurrently(action: Callable[[], None], count: int) -> list[BaseException]:
    """Run ``action`` in ``count`` threads and collect any exceptions raised.

    Args:
        action: Zero-argument callable invoked once per worker thread.
        count: Number of worker threads to spawn.

    Returns:
        Exceptions raised by worker threads, in the order they were
        caught. Empty if every worker completed without raising.
    """
    errors: list[BaseException] = []
    errors_lock = threading.Lock()

    def _worker() -> None:
        try:
            action()
        except Exception as exc:
            with errors_lock:
                errors.append(exc)

    threads = [threading.Thread(target=_worker) for _ in range(count)]
    for thread in threads:
        thread.start()
    for thread in threads:
        thread.join(timeout=15)
    return errors


def _synchronizing_wrapper(
    original: Callable[[DatabaseConnection], set[int]],
    barrier: threading.Barrier,
    timeout: float = 2.0,
) -> Callable[[DatabaseConnection], set[int]]:
    """Wrap ``_get_applied_versions`` so concurrent callers rendezvous first.

    Forces every concurrent ``migrate()`` invocation that reaches the
    check-then-apply read to arrive at (approximately) the same moment,
    making the "every caller observes zero applied migrations" race
    deterministic instead of depending on raw thread-scheduling luck.

    If ``migrate()`` becomes properly serialized (the Issue #78 fix),
    only one caller at a time ever reaches this point; ``timeout`` lets
    that lone caller proceed after a brief wait instead of blocking
    forever for parties that will never arrive. Once the barrier trips
    or breaks, later calls resolve immediately.

    Args:
        original: The real ``_get_applied_versions`` to delegate to.
        barrier: Shared barrier all concurrent callers rendezvous on.
        timeout: Max seconds to wait for other parties before giving up.

    Returns:
        A wrapped callable with the same signature as ``original``.
    """

    def _wrapped(conn: DatabaseConnection) -> set[int]:
        with contextlib.suppress(threading.BrokenBarrierError):
            barrier.wait(timeout=timeout)
        return original(conn)

    return _wrapped


# ---------------------------------------------------------------------------
# _ensure_schema_migrations_table
# ---------------------------------------------------------------------------


class TestEnsureSchemaMigrationsTable:
    """Tests for _ensure_schema_migrations_table."""

    def test_creates_table(self, db_path: str) -> None:
        """Test that the schema_migrations table is created."""
        conn = get_connection(db_path)
        try:
            _ensure_schema_migrations_table(conn)
            cursor = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='schema_migrations'"
            )
            row = cursor.fetchone()
            assert row is not None
            assert row["name"] == "schema_migrations"
        finally:
            conn.close()

    def test_idempotent(self, db_path: str) -> None:
        """Test calling twice does not raise."""
        conn = get_connection(db_path)
        try:
            _ensure_schema_migrations_table(conn)
            _ensure_schema_migrations_table(conn)
            cursor = conn.execute(
                "SELECT COUNT(*) as cnt FROM sqlite_master "
                "WHERE type='table' AND name='schema_migrations'"
            )
            assert cursor.fetchone()["cnt"] == 1
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# _get_applied_versions
# ---------------------------------------------------------------------------


class TestGetAppliedVersions:
    """Tests for _get_applied_versions."""

    def test_empty_when_no_migrations(self, db_path: str) -> None:
        """Test returns empty set on fresh database."""
        conn = get_connection(db_path)
        try:
            _ensure_schema_migrations_table(conn)
            result = _get_applied_versions(conn)
            assert result == set()
        finally:
            conn.close()

    def test_returns_applied_versions(self, db_path: str) -> None:
        """Test returns set of applied version numbers."""
        conn = get_connection(db_path)
        try:
            _ensure_schema_migrations_table(conn)
            conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (1, "initial_schema"),
            )
            conn.execute(
                "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
                (2, "seed_data"),
            )
            conn.commit()
            result = _get_applied_versions(conn)
            assert result == {1, 2}
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# _discover_migrations
# ---------------------------------------------------------------------------


class TestDiscoverMigrations:
    """Tests for _discover_migrations."""

    def test_discovers_sqlite_migrations(self) -> None:
        """Test discovers SQLite migration files."""
        migrations = _discover_migrations(is_pg=False)
        assert len(migrations) >= 3
        versions = [v for v, _, _ in migrations]
        assert 1 in versions
        assert 2 in versions
        assert 3 in versions

    def test_discovers_pg_migrations(self) -> None:
        """Test discovers PostgreSQL migration files."""
        migrations = _discover_migrations(is_pg=True)
        assert len(migrations) >= 3
        versions = [v for v, _, _ in migrations]
        assert 1 in versions
        assert 2 in versions
        assert 3 in versions

    def test_sorted_by_version(self) -> None:
        """Test migrations are returned in version order."""
        migrations = _discover_migrations(is_pg=False)
        versions = [v for v, _, _ in migrations]
        assert versions == sorted(versions)

    def test_sqlite_excludes_pg_files(self) -> None:
        """Test SQLite discovery does not include _pg.sql files."""
        migrations = _discover_migrations(is_pg=False)
        for _, _, path in migrations:
            assert not path.name.endswith("_pg.sql")

    def test_pg_only_includes_pg_files(self) -> None:
        """Test PostgreSQL discovery only includes _pg.sql files."""
        migrations = _discover_migrations(is_pg=True)
        for _, _, path in migrations:
            assert path.name.endswith("_pg.sql")

    @pytest.mark.parametrize("is_pg", [False, True])
    def test_no_duplicate_migration_versions(self, is_pg: bool) -> None:
        """Test every migration version number maps to exactly one file.

        ``schema_migrations`` tracks applied migrations by
        ``version INTEGER PRIMARY KEY``, so two files sharing a number
        would silently skip whichever sorts second (``migrate`` treats
        the version as already applied) and violate the primary key if
        both ever ran. Regression guard for the merge collision between
        ``005_shopping_lists`` and the order-submissions ledger
        migration (renumbered to 006, Issue #61 / PR #107).
        """
        migrations = _discover_migrations(is_pg=is_pg)
        versions = [v for v, _, _ in migrations]
        duplicates = {v for v in versions if versions.count(v) > 1}
        assert not duplicates, f"duplicate migration versions: {sorted(duplicates)}"


# ---------------------------------------------------------------------------
# _record_migration
# ---------------------------------------------------------------------------


class TestRecordMigration:
    """Tests for _record_migration."""

    def test_records_version(self, db_path: str) -> None:
        """Test that a version is recorded in schema_migrations."""
        conn = get_connection(db_path)
        try:
            _ensure_schema_migrations_table(conn)
            _record_migration(conn, 1, "initial_schema")
            applied = _get_applied_versions(conn)
            assert 1 in applied
        finally:
            conn.close()

    def test_records_name(self, db_path: str) -> None:
        """Test that the migration name is stored."""
        conn = get_connection(db_path)
        try:
            _ensure_schema_migrations_table(conn)
            _record_migration(conn, 42, "my_migration")
            cursor = conn.execute(
                "SELECT name FROM schema_migrations WHERE version = ?",
                (42,),
            )
            row = cursor.fetchone()
            assert row is not None
            assert row["name"] == "my_migration"
        finally:
            conn.close()

    def test_uses_placeholder_compatible_with_adapter(self) -> None:
        """Test _record_migration passes ? placeholders to conn.execute.

        The adapter layer translates ? to %s for PostgreSQL, so the
        migration runner must use ? placeholders consistently.
        """
        mock_conn = MagicMock()
        _record_migration(mock_conn, 5, "test_mig")
        mock_conn.execute.assert_called_once_with(
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?) "
            "RETURNING version",
            (5, "test_mig"),
        )
        mock_conn.execute.return_value.fetchall.assert_called_once()
        mock_conn.commit.assert_called_once()


# ---------------------------------------------------------------------------
# _discover_hook
# ---------------------------------------------------------------------------


class TestDiscoverHook:
    """Tests for _discover_hook."""

    def test_no_sibling_file_returns_none(self) -> None:
        """Test versions without a matching migrations/*.py file return None."""
        result = _discover_hook(1, "initial_schema")
        assert result is None

    def test_mismatched_name_returns_none(self) -> None:
        """Test a version/name pair with no matching file returns None.

        Version 3 has a hook file, but only under the "normalize_unit_enum"
        name; an unrelated name for the same version must not resolve.
        """
        result = _discover_hook(3, "some_other_name")
        assert result is None

    def test_sibling_file_present_resolves_migrate_callable(self) -> None:
        """Test _discover_hook resolves the module's migrate callable.

        Version 3 has a sibling ``003_normalize_unit_enum.py`` file
        alongside its SQL migration. _discover_hook must import that
        module by convention and return its ``migrate`` attribute.
        """
        result = _discover_hook(3, "normalize_unit_enum")

        assert callable(result)
        module = importlib.import_module(
            "grocery_butler.db.migrations.003_normalize_unit_enum"
        )
        assert result is module.migrate


# ---------------------------------------------------------------------------
# _run_python_hook
# ---------------------------------------------------------------------------


class TestRunPythonHook:
    """Tests for _run_python_hook."""

    def test_no_hook_is_noop(self, db_path: str) -> None:
        """Test a version with no discovered hook does nothing and does not raise."""
        with patch(
            "grocery_butler.db.migrate._discover_hook", return_value=None
        ) as mock_discover:
            _run_python_hook(1, "initial_schema", db_path)

        mock_discover.assert_called_once_with(1, "initial_schema")

    def test_discovered_hook_invoked_once_with_db_path(self, db_path: str) -> None:
        """Test a discovered hook is invoked exactly once with db_path."""
        mock_hook = MagicMock()

        with patch("grocery_butler.db.migrate._discover_hook", return_value=mock_hook):
            _run_python_hook(3, "normalize_unit_enum", db_path)

        mock_hook.assert_called_once_with(db_path)

    def test_hook_error_propagates(self, db_path: str) -> None:
        """Test exceptions raised by a discovered hook propagate to the caller."""
        mock_hook = MagicMock(side_effect=RuntimeError("boom"))

        with (
            patch("grocery_butler.db.migrate._discover_hook", return_value=mock_hook),
            pytest.raises(RuntimeError, match="boom"),
        ):
            _run_python_hook(3, "normalize_unit_enum", db_path)


# ---------------------------------------------------------------------------
# migrate (integration)
# ---------------------------------------------------------------------------


class TestMigrate:
    """Integration tests for the migrate function."""

    def test_applies_all_migrations(self, db_path: str) -> None:
        """Test migrate applies all pending migrations."""
        count = migrate(db_path)
        assert count >= 3

    def test_creates_tables(self, db_path: str) -> None:
        """Test migrate creates expected database tables."""
        migrate(db_path)
        conn = get_connection(db_path)
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = [row["name"] for row in cursor.fetchall()]
        finally:
            conn.close()

        assert "recipes" in tables
        assert "pantry_staples" in tables
        assert "schema_migrations" in tables

    def test_seeds_data(self, db_path: str) -> None:
        """Test migrate seeds pantry staples and preferences."""
        migrate(db_path)
        conn = get_connection(db_path)
        try:
            cursor = conn.execute("SELECT COUNT(*) as cnt FROM pantry_staples")
            assert cursor.fetchone()["cnt"] == 10

            cursor = conn.execute("SELECT COUNT(*) as cnt FROM preferences")
            assert cursor.fetchone()["cnt"] == 2
        finally:
            conn.close()

    def test_idempotent(self, db_path: str) -> None:
        """Test running migrate twice applies nothing the second time."""
        first = migrate(db_path)
        assert first >= 3
        second = migrate(db_path)
        assert second == 0

    def test_returns_zero_when_up_to_date(self, db_path: str) -> None:
        """Test returns 0 when no pending migrations."""
        migrate(db_path)
        assert migrate(db_path) == 0

    def test_bad_sql_raises(self, tmp_path: Path) -> None:
        """Test that invalid SQL in a migration file raises an error."""
        # Create a temp migrations dir with a broken migration
        fake_dir = tmp_path / "migrations"
        fake_dir.mkdir()
        (fake_dir / "__init__.py").write_text("")
        (fake_dir / "001_bad.sql").write_text("CREATE TABL broken_syntax;")

        db_path = str(tmp_path / "bad.db")
        with (
            patch.object(
                type(MIGRATIONS_DIR),
                "iterdir",
                return_value=iter(sorted(fake_dir.iterdir())),
            ),
            pytest.raises(Exception, match=r".+"),
        ):
            migrate(db_path)

    def test_python_hook_runs_after_sql_and_before_record(self, tmp_path: Path) -> None:
        """Test the per-migration order is: executescript -> hook -> record.

        Uses a temp migrations dir (same ``iterdir`` patch pattern as
        ``test_bad_sql_raises``) with a single SQL migration that creates
        a marker table. ``_run_python_hook`` and ``_record_migration`` are
        replaced with recording fakes: the fake hook asserts the marker
        table already exists (proving the SQL ran first) and the fake
        record simply logs its call, so the final event order proves the
        hook ran strictly between the SQL apply and the record step.
        """
        fake_dir = tmp_path / "migrations"
        fake_dir.mkdir()
        (fake_dir / "__init__.py").write_text("")
        (fake_dir / "001_marker.sql").write_text(
            "CREATE TABLE marker_table (id INTEGER);"
        )

        db_path = str(tmp_path / "order.db")
        events: list[str] = []

        def _fake_hook(version: int, name: str, path: str) -> None:
            conn = get_connection(path)
            try:
                cursor = conn.execute(
                    "SELECT name FROM sqlite_master "
                    "WHERE type='table' AND name='marker_table'"
                )
                assert cursor.fetchone() is not None, (
                    "hook ran before the SQL migration was applied"
                )
            finally:
                conn.close()
            events.append("hook")

        def _fake_record(conn: object, version: int, name: str) -> None:
            events.append("record")

        with (
            patch.object(
                type(MIGRATIONS_DIR),
                "iterdir",
                return_value=iter(sorted(fake_dir.iterdir())),
            ),
            patch("grocery_butler.db.migrate._run_python_hook", side_effect=_fake_hook),
            patch(
                "grocery_butler.db.migrate._record_migration",
                side_effect=_fake_record,
            ),
        ):
            migrate(db_path)

        assert events == ["hook", "record"]

    def test_convention_hook_normalizes_denormalized_unit_end_to_end(
        self, db_path: str
    ) -> None:
        """Test the real 003 convention hook fires and normalizes data.

        Runs the real (unmocked) migrate() end-to-end. All migrations
        apply on a fresh database first. Version 3's schema_migrations
        record is then deleted and a denormalized ``unit`` value ("cups")
        is seeded into recipe_ingredients, simulating "003 pending, data
        present." Re-running migrate() must rediscover and re-run the
        003 hook (via _discover_hook's file-convention lookup, not a
        mock), normalizing "cups" to the canonical Unit.CUP value.
        """
        migrate(db_path)

        conn = get_connection(db_path)
        try:
            conn.execute("DELETE FROM schema_migrations WHERE version = ?", (3,))
            conn.commit()

            cursor = conn.execute(
                "INSERT INTO recipes (name, display_name) VALUES (?, ?)",
                ("hook_e2e_recipe", "Hook E2E Recipe"),
            )
            recipe_id = cursor.lastrowid
            assert recipe_id is not None
            conn.commit()

            conn.execute(
                "INSERT INTO recipe_ingredients "
                "(recipe_id, ingredient, quantity, unit, category, "
                "quantity_per_serving) VALUES (?, ?, ?, ?, ?, ?)",
                (recipe_id, "flour", 2.0, "cups", "pantry_dry", 0.5),
            )
            conn.commit()
        finally:
            conn.close()

        count = migrate(db_path)
        assert count == 1

        conn = get_connection(db_path)
        try:
            row = conn.execute(
                "SELECT unit FROM recipe_ingredients WHERE recipe_id = ?",
                (recipe_id,),
            ).fetchone()
        finally:
            conn.close()

        assert row is not None
        assert row["unit"] == Unit.CUP.value


# ---------------------------------------------------------------------------
# Concurrent first-boot race reproduction (Issue #78)
#
# grocery-butler runs behind multiple gunicorn worker processes that can
# each hit a fresh database at roughly the same instant. Every store
# constructor also calls migrate() (via init_db) unconditionally, so even
# single-process multi-threaded callers can race. Both tests below use
# ``_synchronizing_wrapper`` to force concurrent callers to observe "zero
# applied migrations" simultaneously, deterministically reproducing the
# check-then-apply race instead of depending on raw thread timing.
# ---------------------------------------------------------------------------


class TestConcurrentFirstBootRace:
    """Reproduction tests for concurrent first-boot migration races."""

    def test_concurrent_first_boot_applies_each_migration_exactly_once(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test N concurrent migrate() calls on one fresh DB apply cleanly.

        Reproduces the gunicorn multi-worker race from Issue #78: several
        workers boot against the same fresh database and each calls
        migrate() before any of them has recorded a single applied
        version. On current (unguarded) code this causes a duplicate-apply
        failure -- typically an IntegrityError from the second writer to
        record the same version, or a "database is locked" error from
        concurrent unserialized writers. Once migrate() takes a
        cross-process lock around its check-then-apply sequence (Issue
        #78 fix), every worker is serialized and this passes cleanly with
        each migration recorded exactly once.
        """
        num_workers = 8
        barrier = threading.Barrier(num_workers)
        original = migrate_module._get_applied_versions
        monkeypatch.setattr(
            migrate_module,
            "_get_applied_versions",
            _synchronizing_wrapper(original, barrier),
        )

        errors = _run_concurrently(lambda: migrate(db_path), num_workers)

        assert not errors, (
            f"migrate() raised in {len(errors)}/{num_workers} worker "
            f"thread(s) under concurrent first boot: {errors!r}"
        )

        conn = get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        finally:
            conn.close()

        applied_versions = [row["version"] for row in rows]
        expected_versions = sorted(v for v, _, _ in _discover_migrations(is_pg=False))

        assert applied_versions == expected_versions, (
            "each migration must be recorded exactly once, in version order"
        )


class TestConcurrentStoreConstructionRace:
    """Reproduction tests for races triggered via concurrent store construction."""

    def test_concurrent_store_construction_applies_migrations_once(
        self, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test N concurrent RecipeStore(...) constructions apply cleanly.

        Every store constructor calls init_db() -> migrate() at
        construction time (Issue #78: "migration runner ... runs inside
        every store constructor"). Several gunicorn workers constructing a
        store against the same fresh database concurrently must not race.
        Uses the same barrier-synchronized ``_get_applied_versions``
        technique as
        ``test_concurrent_first_boot_applies_each_migration_exactly_once``.
        Once Issue #78's run-once ``init_db`` guard lands, only the first
        construction ever reaches migrate() at all, so later barrier waits
        harmlessly time out via ``_synchronizing_wrapper`` and this passes.
        """
        num_workers = 8
        barrier = threading.Barrier(num_workers)
        original = migrate_module._get_applied_versions
        monkeypatch.setattr(
            migrate_module,
            "_get_applied_versions",
            _synchronizing_wrapper(original, barrier),
        )

        errors = _run_concurrently(lambda: RecipeStore(db_path), num_workers)

        assert not errors, (
            f"RecipeStore(...) raised in {len(errors)}/{num_workers} worker "
            f"thread(s) under concurrent construction: {errors!r}"
        )

        conn = get_connection(db_path)
        try:
            rows = conn.execute(
                "SELECT version FROM schema_migrations ORDER BY version"
            ).fetchall()
        finally:
            conn.close()

        applied_versions = [row["version"] for row in rows]
        expected_versions = sorted(v for v, _, _ in _discover_migrations(is_pg=False))

        assert applied_versions == expected_versions, (
            "each migration must be recorded exactly once, in version order"
        )


# ---------------------------------------------------------------------------
# CLI (main)
# ---------------------------------------------------------------------------


class TestMigration007ProductMappingStockSize:
    """Tests for migration 007 (product_mapping size/stock columns).

    Issue #71: ``ProductSearchService`` used to rehydrate cached rows
    with a hardcoded ``size=""`` and a default ``in_stock=True`` because
    the ``product_mapping`` table never stored the real values. This
    migration adds the two columns so the fix has somewhere to persist
    them.
    """

    def test_migration_007_adds_product_mapping_columns(self, db_path: str) -> None:
        """Test migrate() adds safeway_product_size/safeway_in_stock columns.

        Running migrate() a second time afterwards must apply zero
        additional migrations (idempotency).
        """
        migrate(db_path)

        conn = get_connection(db_path)
        try:
            cursor = conn.execute("PRAGMA table_info(product_mapping)")
            columns = {row["name"] for row in cursor.fetchall()}
        finally:
            conn.close()

        assert "safeway_product_size" in columns
        assert "safeway_in_stock" in columns

        assert migrate(db_path) == 0


class TestMigration008PendingActionsResolver:
    """Tests for migration 008 (pending_actions.resolver column, issue #75).

    W1: confirm/deny routes must record which caller resolved a staged
    action. This nullable column is the audit trail for that caller
    (rows resolved by the system, e.g. TTL expiry, leave it NULL).
    """

    def test_migration_008_adds_resolver_column(self, db_path: str) -> None:
        """Test migrate() adds a nullable resolver column to pending_actions.

        Running migrate() a second time afterwards must apply zero
        additional migrations (idempotency).
        """
        migrate(db_path)

        conn = get_connection(db_path)
        try:
            cursor = conn.execute("PRAGMA table_info(pending_actions)")
            columns = {row["name"] for row in cursor.fetchall()}
        finally:
            conn.close()

        assert "resolver" in columns
        assert migrate(db_path) == 0


class TestCLI:
    """Tests for the CLI entry point."""

    def test_main_with_db_path(self, db_path: str) -> None:
        """Test main() accepts a db_path argument."""
        result = main([db_path])
        assert result == 0

    def test_main_uses_env_var(self, tmp_path: Path) -> None:
        """Test main() falls back to DATABASE_URL env var."""
        db_file = str(tmp_path / "env_test.db")
        with patch.dict("os.environ", {"DATABASE_URL": db_file}):
            result = main([])
            assert result == 0

        conn = get_connection(db_file)
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='schema_migrations'"
            )
            assert cursor.fetchone() is not None
        finally:
            conn.close()


# ---------------------------------------------------------------------------
# Migration lock tests (Issue #78)
#
# migrate() must hold a lock around its check-then-apply sequence so that
# concurrent callers cannot both observe "nothing applied yet" and both
# attempt to apply the same migration. PostgreSQL gets a session-level
# advisory lock on the SAME connection used for the migration work;
# SQLite (file-backed) gets a cross-process fcntl.flock on a sidecar file;
# ":memory:" needs neither (Layer 1's in-process lock already covers it).
#
# The PostgreSQL tests mock the connection layer entirely (no real
# PostgreSQL server) so the exact SQL issued can be inspected directly,
# mirroring the fake-connection pattern in
# tests/test_order_submissions.py::TestTryRecordAttemptPostgresAdvisoryLock.
# ---------------------------------------------------------------------------


class _FakeCursorResult:
    """Stub CursorResult reporting an empty result set."""

    def fetchone(self) -> None:
        """Return None; no test here reads a single result row.

        Returns:
            None, always.
        """
        return None

    def fetchall(self) -> list[object]:
        """Return an empty list; no pending versions/rows to report.

        Returns:
            An empty list, always.
        """
        return []


class _RecordingConnection:
    """Fake DatabaseConnection recording every statement, in call order."""

    def __init__(self) -> None:
        """Initialize with an empty ordered call log."""
        self.calls: list[tuple[str, tuple[object, ...]]] = []

    def execute(self, sql: str, params: Sequence[object] = ()) -> _FakeCursorResult:
        """Record the statement and params, returning an empty result.

        Args:
            sql: The SQL statement executed.
            params: The parameters bound to the statement.

        Returns:
            A canned empty-result stub.
        """
        self.calls.append((sql, tuple(params)))
        return _FakeCursorResult()

    def executescript(self, sql: str) -> None:
        """Record the script as a call with no params.

        Args:
            sql: The multi-statement SQL script executed.
        """
        self.calls.append((sql, ()))

    def commit(self) -> None:
        """No-op commit."""

    def close(self) -> None:
        """No-op close."""


class TestPostgresMigrateAdvisoryLock:
    """Tests for the PostgreSQL advisory-lock branch of migrate()'s lock."""

    def test_postgres_migrate_takes_advisory_lock_before_apply(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test migrate() locks a PostgreSQL connection before touching schema.

        No real PostgreSQL server is involved: ``get_connection`` is
        mocked to return a recording fake so the exact SQL issued by
        migrate() can be inspected. Migration discovery is mocked to
        report no pending migrations so this test isolates the lock
        sequencing from migration-application concerns entirely. A
        ``pg_advisory_lock`` statement must be executed on the connection
        strictly before the first ``schema_migrations``-touching
        statement (table creation / applied-versions read).
        """
        conn = _RecordingConnection()
        monkeypatch.setattr(migrate_module, "get_connection", lambda _path: conn)
        monkeypatch.setattr(migrate_module, "_discover_migrations", lambda is_pg: [])

        migrate_module.migrate("postgresql://example/db")

        lock_index = next(
            i
            for i, (sql, _params) in enumerate(conn.calls)
            if "pg_advisory_lock" in sql and "unlock" not in sql.lower()
        )
        schema_migrations_index = next(
            i
            for i, (sql, _params) in enumerate(conn.calls)
            if "schema_migrations" in sql
        )

        assert lock_index < schema_migrations_index, (
            "advisory lock must be acquired before touching schema_migrations"
        )

    def test_postgres_migrate_releases_advisory_lock_even_on_failure(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test migrate() releases its advisory lock even when apply fails.

        ``_get_applied_versions`` (invoked inside the locked
        check-then-apply section) is forced to raise, simulating a
        failure partway through migrate(). The advisory lock taken on
        the connection must still be released via ``pg_advisory_unlock``
        despite the failure.
        """
        conn = _RecordingConnection()
        monkeypatch.setattr(migrate_module, "get_connection", lambda _path: conn)

        def _boom(_conn: object) -> set[int]:
            raise RuntimeError("boom")

        monkeypatch.setattr(migrate_module, "_get_applied_versions", _boom)

        with pytest.raises(RuntimeError, match="boom"):
            migrate_module.migrate("postgresql://example/db")

        unlock_calls = [
            sql for sql, _params in conn.calls if "pg_advisory_unlock" in sql
        ]

        assert unlock_calls, "advisory lock must be released even when migrate() fails"


class TestSQLiteMigrateFileLock:
    """Tests for the SQLite file-lock branch of migrate()'s lock."""

    def test_sqlite_migrate_acquires_and_releases_file_lock(
        self, tmp_path: Path, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test migrate() takes an exclusive flock around a file-backed DB.

        ``fcntl.flock`` is monkeypatched (on the shared ``fcntl`` module,
        so it intercepts calls regardless of how migrate.py imports it)
        to record every call. A real migrate() run against a fresh
        on-disk SQLite database must acquire ``LOCK_EX`` and later
        release it with ``LOCK_UN``.
        """
        flock_calls: list[int] = []

        def _fake_flock(_fd: int, operation: int) -> None:
            flock_calls.append(operation)

        monkeypatch.setattr(fcntl, "flock", _fake_flock)

        db_path = str(tmp_path / "sqlite_lock.db")
        migrate_module.migrate(db_path)

        assert fcntl.LOCK_EX in flock_calls, "expected an exclusive lock acquisition"
        ex_index = flock_calls.index(fcntl.LOCK_EX)
        un_index = next(
            i
            for i, op in enumerate(flock_calls)
            if op == fcntl.LOCK_UN and i > ex_index
        )

        assert un_index > ex_index, (
            "the exclusive lock must be released after acquisition"
        )

    def test_memory_migrate_skips_file_lock(
        self, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test migrate(":memory:") never touches fcntl.flock.

        ``":memory:"`` is process-local (Layer 1's in-process
        ``_init_lock`` already covers it), so it needs no cross-process
        file lock. Note: because no flock call is issued anywhere in the
        codebase yet, this negative assertion also holds true before the
        Issue #78 fix lands -- it is a regression guard for the fixed
        behavior rather than a reproduction of a current bug.
        """
        flock_calls: list[int] = []

        def _fake_flock(_fd: int, operation: int) -> None:
            flock_calls.append(operation)

        monkeypatch.setattr(fcntl, "flock", _fake_flock)

        migrate_module.migrate(":memory:")

        assert flock_calls == []


class TestRecordMigrationDuplicateTolerance:
    """Tests for _record_migration's duplicate-version tolerance (Issue #78).

    Under the fixed migrate(), a caller can lose a lock-acquisition race
    against another process and re-read "not yet applied" for a version
    that a concurrent migrate() call is in the process of recording.
    ``_record_migration`` must treat a duplicate-version IntegrityError as
    "already applied" rather than propagating it.
    """

    def test_record_migration_tolerates_duplicate_version(self, db_path: str) -> None:
        """Test recording the same version twice does not raise.

        Exactly one row for the version must exist afterwards -- the
        second call is a no-op, not a second insert.
        """
        conn = get_connection(db_path)
        try:
            _ensure_schema_migrations_table(conn)
            _record_migration(conn, 1, "x")

            _record_migration(conn, 1, "x")

            applied = _get_applied_versions(conn)
            assert applied == {1}

            cursor = conn.execute(
                "SELECT COUNT(*) as cnt FROM schema_migrations WHERE version = ?",
                (1,),
            )
            assert cursor.fetchone()["cnt"] == 1
        finally:
            conn.close()
