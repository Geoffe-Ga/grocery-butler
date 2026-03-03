"""Tests for grocery_butler.db.adapter module."""

from __future__ import annotations

import os
import sqlite3
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from grocery_butler.db.adapter import (
    _HAS_PSYCOPG2,
    CursorResult,
    DatabaseConnection,
    IntegrityError,
    SQLiteConnection,
    _inject_returning,
    _translate_placeholders,
    create_connection,
)


class TestCreateConnection:
    """Tests for the create_connection factory."""

    def test_returns_sqlite_connection_for_file_path(self, tmp_path: Path) -> None:
        """Test create_connection returns SQLiteConnection for a file path."""
        db_path = str(tmp_path / "test.db")
        conn = create_connection(db_path)
        try:
            assert isinstance(conn, SQLiteConnection)
        finally:
            conn.close()

    def test_returns_sqlite_connection_for_memory(self) -> None:
        """Test create_connection returns SQLiteConnection for :memory:."""
        conn = create_connection(":memory:")
        try:
            assert isinstance(conn, SQLiteConnection)
        finally:
            conn.close()

    def test_postgres_url_routes_to_postgres_backend(self) -> None:
        """Test create_connection routes postgres:// URLs to PostgreSQL backend."""
        # Without a real Postgres server, this will raise OperationalError
        # but it proves the routing works (not ImportError)
        if _HAS_PSYCOPG2:
            import psycopg2

            with pytest.raises(psycopg2.OperationalError):
                create_connection("postgresql://localhost:1/nonexistent")
        else:
            with pytest.raises(ImportError, match="psycopg2-binary"):
                create_connection("postgresql://localhost/test")

    def test_postgres_scheme_routes_to_postgres_backend(self) -> None:
        """Test create_connection routes postgres:// scheme to PostgreSQL backend."""
        if _HAS_PSYCOPG2:
            import psycopg2

            with pytest.raises(psycopg2.OperationalError):
                create_connection("postgres://localhost:1/nonexistent")
        else:
            with pytest.raises(ImportError, match="psycopg2-binary"):
                create_connection("postgres://localhost/test")


class TestSQLiteConnection:
    """Tests for the SQLiteConnection wrapper."""

    def test_execute_select(self, tmp_path: Path) -> None:
        """Test execute with a simple SELECT."""
        conn = create_connection(str(tmp_path / "test.db"))
        try:
            conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT);")
            conn.execute("INSERT INTO t (name) VALUES (?)", ("alice",))
            conn.commit()
            result = conn.execute("SELECT name FROM t WHERE id = ?", (1,))
            row = result.fetchone()
            assert row is not None
            assert row["name"] == "alice"
        finally:
            conn.close()

    def test_execute_no_params(self, tmp_path: Path) -> None:
        """Test execute without parameters."""
        conn = create_connection(str(tmp_path / "test.db"))
        try:
            conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY);")
            result = conn.execute("SELECT COUNT(*) as cnt FROM t")
            row = result.fetchone()
            assert row is not None
            assert row["cnt"] == 0
        finally:
            conn.close()

    def test_lastrowid(self, tmp_path: Path) -> None:
        """Test lastrowid after INSERT."""
        conn = create_connection(str(tmp_path / "test.db"))
        try:
            conn.executescript(
                "CREATE TABLE t (id INTEGER PRIMARY KEY AUTOINCREMENT, name TEXT);"
            )
            result = conn.execute("INSERT INTO t (name) VALUES (?)", ("bob",))
            assert result.lastrowid == 1
            result2 = conn.execute("INSERT INTO t (name) VALUES (?)", ("carol",))
            assert result2.lastrowid == 2
        finally:
            conn.close()

    def test_rowcount(self, tmp_path: Path) -> None:
        """Test rowcount after UPDATE."""
        conn = create_connection(str(tmp_path / "test.db"))
        try:
            conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT);")
            conn.execute("INSERT INTO t (val) VALUES (?)", ("a",))
            conn.execute("INSERT INTO t (val) VALUES (?)", ("b",))
            conn.commit()
            result = conn.execute("UPDATE t SET val = ?", ("x",))
            assert result.rowcount == 2
        finally:
            conn.close()

    def test_fetchall(self, tmp_path: Path) -> None:
        """Test fetchall returns all rows."""
        conn = create_connection(str(tmp_path / "test.db"))
        try:
            conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT);")
            conn.execute("INSERT INTO t (name) VALUES (?)", ("a",))
            conn.execute("INSERT INTO t (name) VALUES (?)", ("b",))
            conn.commit()
            result = conn.execute("SELECT name FROM t ORDER BY name")
            rows = result.fetchall()
            assert len(rows) == 2
            assert rows[0]["name"] == "a"
            assert rows[1]["name"] == "b"
        finally:
            conn.close()

    def test_fetchone_returns_none_on_empty(self, tmp_path: Path) -> None:
        """Test fetchone returns None when no rows match."""
        conn = create_connection(str(tmp_path / "test.db"))
        try:
            conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT);")
            result = conn.execute("SELECT * FROM t WHERE id = ?", (999,))
            assert result.fetchone() is None
        finally:
            conn.close()

    def test_dict_row_access(self, tmp_path: Path) -> None:
        """Test that rows support dict-like column access."""
        conn = create_connection(str(tmp_path / "test.db"))
        try:
            conn.executescript(
                "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT, value REAL);"
            )
            conn.execute("INSERT INTO t (name, value) VALUES (?, ?)", ("test", 3.14))
            conn.commit()
            result = conn.execute("SELECT * FROM t")
            row = result.fetchone()
            assert row is not None
            assert row["name"] == "test"
            assert row["value"] == pytest.approx(3.14)
            assert dict(row) == {"id": 1, "name": "test", "value": 3.14}
        finally:
            conn.close()

    def test_executescript(self, tmp_path: Path) -> None:
        """Test executescript runs multi-statement SQL."""
        conn = create_connection(str(tmp_path / "test.db"))
        try:
            conn.executescript(
                "CREATE TABLE a (id INTEGER PRIMARY KEY);"
                "CREATE TABLE b (id INTEGER PRIMARY KEY);"
            )
            # Both tables should exist
            result = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='table' ORDER BY name"
            )
            tables = [r["name"] for r in result.fetchall()]
            assert "a" in tables
            assert "b" in tables
        finally:
            conn.close()

    def test_commit(self, tmp_path: Path) -> None:
        """Test commit persists data across connections."""
        db_path = str(tmp_path / "test.db")
        conn = create_connection(db_path)
        try:
            conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY, val TEXT);")
            conn.execute("INSERT INTO t (val) VALUES (?)", ("persisted",))
            conn.commit()
        finally:
            conn.close()

        conn2 = create_connection(db_path)
        try:
            result = conn2.execute("SELECT val FROM t")
            row = result.fetchone()
            assert row is not None
            assert row["val"] == "persisted"
        finally:
            conn2.close()

    def test_raw_property(self, tmp_path: Path) -> None:
        """Test the raw property exposes the underlying sqlite3.Connection."""
        conn = create_connection(str(tmp_path / "test.db"))
        try:
            assert isinstance(conn, SQLiteConnection)
            assert isinstance(conn.raw, sqlite3.Connection)
        finally:
            conn.close()

    def test_wal_mode_enabled(self, tmp_path: Path) -> None:
        """Test WAL journal mode is enabled on creation."""
        conn = create_connection(str(tmp_path / "test.db"))
        try:
            assert isinstance(conn, SQLiteConnection)
            result = conn.raw.execute("PRAGMA journal_mode").fetchone()
            assert result[0] == "wal"
        finally:
            conn.close()

    def test_foreign_keys_enabled(self, tmp_path: Path) -> None:
        """Test foreign key enforcement is enabled."""
        conn = create_connection(str(tmp_path / "test.db"))
        try:
            assert isinstance(conn, SQLiteConnection)
            result = conn.raw.execute("PRAGMA foreign_keys").fetchone()
            assert result[0] == 1
        finally:
            conn.close()


class TestIntegrityError:
    """Tests for the unified IntegrityError."""

    def test_integrity_error_catches_sqlite3(self) -> None:
        """Test IntegrityError includes sqlite3.IntegrityError."""
        if isinstance(IntegrityError, tuple):
            assert sqlite3.IntegrityError in IntegrityError
        else:
            assert IntegrityError is sqlite3.IntegrityError

    def test_integrity_error_catchable(self, tmp_path: Path) -> None:
        """Test IntegrityError can catch UNIQUE constraint violations."""
        conn = create_connection(str(tmp_path / "test.db"))
        try:
            conn.executescript(
                "CREATE TABLE t (id INTEGER PRIMARY KEY, name TEXT UNIQUE);"
            )
            conn.execute("INSERT INTO t (name) VALUES (?)", ("dup",))
            conn.commit()
            with pytest.raises(IntegrityError):
                conn.execute("INSERT INTO t (name) VALUES (?)", ("dup",))
        finally:
            conn.close()

    @pytest.mark.skipif(not _HAS_PSYCOPG2, reason="psycopg2 not installed")
    def test_integrity_error_includes_psycopg2(self) -> None:
        """Test IntegrityError includes psycopg2.IntegrityError when available."""
        import psycopg2

        assert isinstance(IntegrityError, tuple)
        assert psycopg2.IntegrityError in IntegrityError


class TestProtocols:
    """Tests for protocol conformance."""

    def test_sqlite_connection_is_database_connection(self, tmp_path: Path) -> None:
        """Test SQLiteConnection satisfies DatabaseConnection protocol."""
        conn = create_connection(str(tmp_path / "test.db"))
        try:
            assert isinstance(conn, DatabaseConnection)
        finally:
            conn.close()

    def test_sqlite_cursor_result_is_cursor_result(self, tmp_path: Path) -> None:
        """Test SQLiteCursorResult satisfies CursorResult protocol."""
        conn = create_connection(str(tmp_path / "test.db"))
        try:
            conn.executescript("CREATE TABLE t (id INTEGER PRIMARY KEY);")
            result = conn.execute("SELECT * FROM t")
            assert isinstance(result, CursorResult)
        finally:
            conn.close()


class TestTranslatePlaceholders:
    """Tests for the _translate_placeholders helper."""

    def test_converts_question_marks(self) -> None:
        """Test ? placeholders are converted to %s."""
        sql = "SELECT * FROM t WHERE id = ? AND name = ?"
        assert _translate_placeholders(sql) == (
            "SELECT * FROM t WHERE id = %s AND name = %s"
        )

    def test_no_placeholders_unchanged(self) -> None:
        """Test SQL without placeholders is unchanged."""
        sql = "SELECT COUNT(*) FROM t"
        assert _translate_placeholders(sql) == sql

    def test_preserves_question_mark_inside_string_literal(self) -> None:
        """Test ? inside single-quoted SQL strings is not replaced."""
        sql = "INSERT INTO t (note) VALUES ('what?') WHERE id = ?"
        assert _translate_placeholders(sql) == (
            "INSERT INTO t (note) VALUES ('what?') WHERE id = %s"
        )

    def test_multiple_literals_with_bare_placeholders(self) -> None:
        """Test mixed string literals and bare ? placeholders."""
        sql = "SELECT * FROM t WHERE note = 'why?' AND id = ? AND tag = 'ok?'"
        assert _translate_placeholders(sql) == (
            "SELECT * FROM t WHERE note = 'why?' AND id = %s AND tag = 'ok?'"
        )


class TestInjectReturning:
    """Tests for the _inject_returning helper."""

    def test_adds_returning_to_insert(self) -> None:
        """Test RETURNING id is added to INSERT statements."""
        sql = "INSERT INTO t (name) VALUES (%s)"
        result, injected = _inject_returning(sql)
        assert result == "INSERT INTO t (name) VALUES (%s) RETURNING id"
        assert injected is True

    def test_skips_insert_with_existing_returning(self) -> None:
        """Test RETURNING is not added when already present."""
        sql = "INSERT INTO t (name) VALUES (%s) RETURNING *"
        result, injected = _inject_returning(sql)
        assert result == sql
        assert injected is False

    def test_skips_non_insert(self) -> None:
        """Test non-INSERT statements are unchanged."""
        sql = "SELECT * FROM t"
        result, injected = _inject_returning(sql)
        assert result == sql
        assert injected is False

    def test_strips_trailing_semicolon(self) -> None:
        """Test trailing semicolons are stripped before RETURNING."""
        sql = "INSERT INTO t (name) VALUES (%s);"
        result, injected = _inject_returning(sql)
        assert result == "INSERT INTO t (name) VALUES (%s) RETURNING id"
        assert injected is True

    def test_case_insensitive_insert(self) -> None:
        """Test INSERT detection is case-insensitive."""
        sql = "insert into t (name) values (%s)"
        result, injected = _inject_returning(sql)
        assert result == "insert into t (name) values (%s) RETURNING id"
        assert injected is True


# ------------------------------------------------------------------
# PostgreSQL integration tests (require a running Postgres instance)
# ------------------------------------------------------------------

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "")
_skip_no_pg = pytest.mark.skipif(
    not _TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — no Postgres server available",
)


@_skip_no_pg
class TestPostgresConnection:
    """Integration tests for the PostgresConnection wrapper.

    Requires a running PostgreSQL instance. Set TEST_DATABASE_URL
    to run: ``TEST_DATABASE_URL=postgresql://user:pass@localhost/test``.
    """

    def _setup_table(self) -> None:
        """Create a test table, dropping it first if it exists."""
        conn = create_connection(_TEST_DB_URL)
        try:
            conn.executescript(
                "DROP TABLE IF EXISTS adapter_test;"
                "CREATE TABLE adapter_test ("
                "  id SERIAL PRIMARY KEY,"
                "  name TEXT UNIQUE NOT NULL,"
                "  value DOUBLE PRECISION"
                ");"
            )
        finally:
            conn.close()

    def test_execute_insert_and_select(self) -> None:
        """Test INSERT and SELECT on PostgreSQL."""
        self._setup_table()
        conn = create_connection(_TEST_DB_URL)
        try:
            conn.execute("INSERT INTO adapter_test (name) VALUES (?)", ("pg_test",))
            conn.commit()
            result = conn.execute(
                "SELECT name FROM adapter_test WHERE name = ?", ("pg_test",)
            )
            row = result.fetchone()
            assert row is not None
            assert row["name"] == "pg_test"
        finally:
            conn.close()

    def test_lastrowid(self) -> None:
        """Test lastrowid returns the SERIAL id from RETURNING."""
        self._setup_table()
        conn = create_connection(_TEST_DB_URL)
        try:
            r1 = conn.execute("INSERT INTO adapter_test (name) VALUES (?)", ("row1",))
            assert r1.lastrowid is not None
            assert r1.lastrowid >= 1
            r2 = conn.execute("INSERT INTO adapter_test (name) VALUES (?)", ("row2",))
            assert r2.lastrowid is not None
            assert r2.lastrowid > r1.lastrowid
            conn.commit()
        finally:
            conn.close()

    def test_rowcount(self) -> None:
        """Test rowcount after UPDATE on PostgreSQL."""
        self._setup_table()
        conn = create_connection(_TEST_DB_URL)
        try:
            conn.execute("INSERT INTO adapter_test (name) VALUES (?)", ("a",))
            conn.execute("INSERT INTO adapter_test (name) VALUES (?)", ("b",))
            conn.commit()
            result = conn.execute("UPDATE adapter_test SET value = ?", (42.0,))
            assert result.rowcount == 2
            conn.commit()
        finally:
            conn.close()

    def test_fetchall(self) -> None:
        """Test fetchall on PostgreSQL."""
        self._setup_table()
        conn = create_connection(_TEST_DB_URL)
        try:
            conn.execute("INSERT INTO adapter_test (name) VALUES (?)", ("x",))
            conn.execute("INSERT INTO adapter_test (name) VALUES (?)", ("y",))
            conn.commit()
            result = conn.execute("SELECT name FROM adapter_test ORDER BY name")
            rows = result.fetchall()
            assert len(rows) == 2
            assert rows[0]["name"] == "x"
            assert rows[1]["name"] == "y"
        finally:
            conn.close()

    def test_dict_row_access(self) -> None:
        """Test dict-like row access on PostgreSQL."""
        self._setup_table()
        conn = create_connection(_TEST_DB_URL)
        try:
            conn.execute(
                "INSERT INTO adapter_test (name, value) VALUES (?, ?)",
                ("pi", 3.14),
            )
            conn.commit()
            result = conn.execute("SELECT * FROM adapter_test WHERE name = ?", ("pi",))
            row = result.fetchone()
            assert row is not None
            assert row["name"] == "pi"
            assert row["value"] == pytest.approx(3.14)
        finally:
            conn.close()

    def test_integrity_error(self) -> None:
        """Test IntegrityError on duplicate UNIQUE violation in Postgres."""
        self._setup_table()
        conn = create_connection(_TEST_DB_URL)
        try:
            conn.execute("INSERT INTO adapter_test (name) VALUES (?)", ("dup",))
            conn.commit()
            with pytest.raises(IntegrityError):
                conn.execute("INSERT INTO adapter_test (name) VALUES (?)", ("dup",))
        finally:
            conn.close()

    def test_pragma_silently_skipped(self) -> None:
        """Test PRAGMA statements are silently skipped on Postgres."""
        conn = create_connection(_TEST_DB_URL)
        try:
            result = conn.execute("PRAGMA journal_mode=WAL")
            # Should return an empty cursor, not raise
            assert result.fetchone() is None
        finally:
            conn.close()
