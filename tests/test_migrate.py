"""Tests for grocery_butler.db.migrate module."""

from __future__ import annotations

import importlib
from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

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

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a temporary database path for test isolation."""
    return str(tmp_path / "test_migrate.db")


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
