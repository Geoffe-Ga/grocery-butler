"""Tests for grocery_butler.db.migrate module."""

from __future__ import annotations

from typing import TYPE_CHECKING
from unittest.mock import MagicMock, patch

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from grocery_butler.db import get_connection
from grocery_butler.db.migrate import (
    _discover_migrations,
    _ensure_schema_migrations_table,
    _get_applied_versions,
    _record_migration,
    main,
    migrate,
)

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
            "INSERT INTO schema_migrations (version, name) VALUES (?, ?)",
            (5, "test_mig"),
        )
        mock_conn.commit.assert_called_once()


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
        from grocery_butler.db.migrate import MIGRATIONS_DIR

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
            pytest.raises(Exception),  # noqa: B017
        ):
            migrate(db_path)


# ---------------------------------------------------------------------------
# CLI (main)
# ---------------------------------------------------------------------------


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
