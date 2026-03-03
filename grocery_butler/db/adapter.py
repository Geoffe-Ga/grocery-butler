"""Database adapter layer for multi-backend support.

Provides a common interface for SQLite and PostgreSQL so that business
logic can use ``?`` placeholders and ``cursor.lastrowid`` regardless of
the underlying database engine.

Usage::

    from grocery_butler.db.adapter import create_connection, IntegrityError

    conn = create_connection("sqlite:///path/to/db.sqlite")
    result = conn.execute("SELECT * FROM recipes WHERE id = ?", (1,))
    row = result.fetchone()    # dict-like row access via row["column"]
    conn.close()
"""

from __future__ import annotations

import re
import sqlite3
from typing import TYPE_CHECKING, Any, Protocol, runtime_checkable

if TYPE_CHECKING:
    from collections.abc import Sequence

# Unified integrity error that works with Python's ``except`` clause.
# ``except IntegrityError:`` catches both SQLite and PostgreSQL violations.
try:
    import psycopg2
    import psycopg2.extras

    IntegrityError: tuple[type[Exception], ...] | type[Exception] = (
        sqlite3.IntegrityError,
        psycopg2.IntegrityError,
    )
    _HAS_PSYCOPG2 = True
except ImportError:
    IntegrityError = sqlite3.IntegrityError
    _HAS_PSYCOPG2 = False


# ------------------------------------------------------------------
# Protocols (the public interface)
# ------------------------------------------------------------------


class DictRow(Protocol):
    """Protocol for dict-like row access.

    Both ``sqlite3.Row`` and ``psycopg2.extras.RealDictRow`` support
    ``row["column_name"]`` access, so business logic can use either.
    """

    def __getitem__(self, key: str) -> Any:
        """Get a column value by name.

        Args:
            key: Column name.

        Returns:
            The column value.
        """
        ...  # pragma: no cover

    def keys(self) -> Any:
        """Return column names.

        Returns:
            Iterable of column name strings.
        """
        ...  # pragma: no cover


@runtime_checkable
class CursorResult(Protocol):
    """Protocol for cursor/result objects returned by execute().

    Provides access to ``lastrowid``, ``rowcount``, ``fetchone``,
    and ``fetchall`` — the subset of cursor methods used by
    business logic.
    """

    @property
    def lastrowid(self) -> int | None:
        """Return the row ID of the last INSERT, or None.

        Returns:
            Last inserted row ID.
        """
        ...  # pragma: no cover

    @property
    def rowcount(self) -> int:
        """Return the number of rows affected by the last operation.

        Returns:
            Number of affected rows.
        """
        ...  # pragma: no cover

    def fetchone(self) -> Any | None:
        """Fetch the next row, or None if exhausted.

        Returns:
            A dict-like row or None.
        """
        ...  # pragma: no cover

    def fetchall(self) -> list[Any]:
        """Fetch all remaining rows.

        Returns:
            List of dict-like rows.
        """
        ...  # pragma: no cover


@runtime_checkable
class DatabaseConnection(Protocol):
    """Protocol for database connection objects.

    Wraps engine-specific connections behind a common interface
    supporting ``execute``, ``executescript``, ``commit``, and ``close``.
    """

    def execute(self, sql: str, params: Sequence[Any] = ()) -> CursorResult:
        """Execute a SQL statement with optional parameters.

        Business logic uses ``?`` placeholders for all backends; the
        adapter translates to ``%s`` for PostgreSQL.

        Args:
            sql: SQL statement with ``?`` placeholders.
            params: Parameter values.

        Returns:
            A CursorResult for reading results.
        """
        ...  # pragma: no cover

    def executescript(self, sql: str) -> None:
        """Execute multiple SQL statements separated by semicolons.

        Used for schema initialization (DDL).

        Args:
            sql: Multi-statement SQL string.
        """
        ...  # pragma: no cover

    def commit(self) -> None:
        """Commit the current transaction."""
        ...  # pragma: no cover

    def close(self) -> None:
        """Close the connection."""
        ...  # pragma: no cover


# ------------------------------------------------------------------
# SQLite backend
# ------------------------------------------------------------------


class SQLiteCursorResult:
    """Wraps a ``sqlite3.Cursor`` to satisfy :class:`CursorResult`."""

    def __init__(self, cursor: sqlite3.Cursor) -> None:
        """Initialize with an underlying SQLite cursor.

        Args:
            cursor: The sqlite3.Cursor to wrap.
        """
        self._cursor = cursor

    @property
    def lastrowid(self) -> int | None:
        """Return the row ID of the last INSERT.

        Returns:
            Last inserted row ID, or None.
        """
        return self._cursor.lastrowid

    @property
    def rowcount(self) -> int:
        """Return the number of rows affected.

        Returns:
            Number of affected rows.
        """
        return self._cursor.rowcount

    def fetchone(self) -> Any | None:
        """Fetch the next row.

        Returns:
            A sqlite3.Row or None.
        """
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        """Fetch all remaining rows.

        Returns:
            List of sqlite3.Row objects.
        """
        return self._cursor.fetchall()


class SQLiteConnection:
    """Wraps a ``sqlite3.Connection`` to satisfy :class:`DatabaseConnection`.

    Configures WAL mode, foreign keys, and ``sqlite3.Row`` factory
    automatically on creation.
    """

    def __init__(self, raw: sqlite3.Connection) -> None:
        """Initialize with a raw sqlite3 connection.

        Args:
            raw: The underlying sqlite3.Connection.
        """
        self._conn = raw

    @property
    def raw(self) -> sqlite3.Connection:
        """Access the underlying sqlite3.Connection.

        Returns:
            The wrapped sqlite3.Connection.
        """
        return self._conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> SQLiteCursorResult:
        """Execute a SQL statement with ``?`` placeholders.

        Args:
            sql: SQL string with ``?`` parameter placeholders.
            params: Parameter values.

        Returns:
            A SQLiteCursorResult wrapping the cursor.
        """
        cursor = self._conn.execute(sql, params)
        return SQLiteCursorResult(cursor)

    def executescript(self, sql: str) -> None:
        """Execute a multi-statement SQL script.

        Args:
            sql: Multi-statement SQL string.
        """
        self._conn.executescript(sql)

    def commit(self) -> None:
        """Commit the current transaction."""
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()


# ------------------------------------------------------------------
# Connection factory
# ------------------------------------------------------------------


def _create_sqlite_connection(db_path: str) -> SQLiteConnection:
    """Create a configured SQLite connection.

    Enables WAL journal mode, foreign key enforcement, and sets
    the row factory to ``sqlite3.Row`` for dict-like access.

    Args:
        db_path: File path, or ``:memory:`` for in-memory databases.

    Returns:
        A configured SQLiteConnection.
    """
    if db_path == ":memory:":
        raw = sqlite3.connect("file::memory:?cache=shared", uri=True)
    else:
        raw = sqlite3.connect(db_path)
    raw.execute("PRAGMA journal_mode=WAL")
    raw.execute("PRAGMA foreign_keys=ON")
    raw.row_factory = sqlite3.Row
    return SQLiteConnection(raw)


def create_connection(db_url: str) -> DatabaseConnection:
    """Create a database connection from a URL or path.

    Routes to the appropriate backend based on the URL scheme:
    - ``postgresql://`` or ``postgres://`` -> PostgreSQL
    - Anything else -> SQLite (treated as a file path)

    Args:
        db_url: Database URL or SQLite file path.

    Returns:
        A DatabaseConnection for the appropriate backend.

    Raises:
        ImportError: If psycopg2 is not installed for PostgreSQL URLs.
    """
    if db_url.startswith(("postgresql://", "postgres://")):
        return _create_postgres_connection(db_url)
    return _create_sqlite_connection(db_url)


def _create_postgres_connection(db_url: str) -> PostgresConnection:
    """Create a configured PostgreSQL connection.

    Uses ``RealDictCursor`` so rows behave like dicts, matching
    the ``sqlite3.Row`` dict-access pattern.

    Args:
        db_url: PostgreSQL connection URL.

    Returns:
        A configured PostgresConnection.

    Raises:
        ImportError: If psycopg2 is not installed.
    """
    if not _HAS_PSYCOPG2:
        raise ImportError(
            "PostgreSQL support requires psycopg2-binary. "
            "Install it with: pip install psycopg2-binary"
        )
    raw = psycopg2.connect(db_url)
    return PostgresConnection(raw)


# ------------------------------------------------------------------
# PostgreSQL backend
# ------------------------------------------------------------------

_INSERT_RETURNING_RE: re.Pattern[str] = re.compile(r"^\s*INSERT\s+", re.IGNORECASE)

# Matches a ``?`` placeholder that is NOT inside a single-quoted string.
# Group 1 captures quoted strings (to skip them); group 2 captures bare ``?``.
_PLACEHOLDER_RE: re.Pattern[str] = re.compile(r"('(?:[^'\\]|\\.)*')|\?")


def _translate_placeholders(sql: str) -> str:
    """Convert SQLite ``?`` placeholders to PostgreSQL ``%s``.

    Only replaces ``?`` that appear outside of single-quoted SQL string
    literals.  A ``?`` inside a literal (e.g. ``'what?'``) is left as-is.

    Args:
        sql: SQL string with ``?`` placeholders.

    Returns:
        SQL string with ``%s`` placeholders.
    """

    def _replace(match: re.Match[str]) -> str:
        # Group 1 matched a quoted string — return it unchanged.
        if match.group(1) is not None:
            return match.group(0)
        # Bare ``?`` — replace with ``%s``.
        return "%s"

    return _PLACEHOLDER_RE.sub(_replace, sql)


def _inject_returning(sql: str) -> tuple[str, bool]:
    """Add ``RETURNING id`` to an INSERT if not already present.

    This allows the adapter to emulate ``cursor.lastrowid`` for
    PostgreSQL, which doesn't natively support it.

    Args:
        sql: SQL string (already translated to ``%s`` placeholders).

    Returns:
        Tuple of (modified SQL, whether RETURNING was injected).
    """
    if _INSERT_RETURNING_RE.match(sql) and "RETURNING" not in sql.upper():
        return sql.rstrip().rstrip(";") + " RETURNING id", True
    return sql, False


class PostgresCursorResult:
    """Wraps a ``psycopg2`` cursor to satisfy :class:`CursorResult`.

    Emulates ``lastrowid`` by reading from ``RETURNING id`` results
    injected by the adapter.
    """

    def __init__(
        self,
        cursor: Any,
        returning_injected: bool = False,
        *,
        noop: bool = False,
    ) -> None:
        """Initialize with an underlying psycopg2 cursor.

        Args:
            cursor: The psycopg2 cursor.
            returning_injected: Whether RETURNING id was added to the SQL.
            noop: If True, no query was executed (e.g. skipped PRAGMA).
        """
        self._cursor = cursor
        self._lastrowid: int | None = None
        self._noop = noop
        if returning_injected and not noop:
            row = cursor.fetchone()
            if row is not None:
                self._lastrowid = row["id"]

    @property
    def lastrowid(self) -> int | None:
        """Return the row ID from RETURNING id, or None.

        Returns:
            Last inserted row ID, or None.
        """
        return self._lastrowid

    @property
    def rowcount(self) -> int:
        """Return the number of rows affected.

        Returns:
            Number of affected rows.
        """
        return int(self._cursor.rowcount)

    def fetchone(self) -> Any | None:
        """Fetch the next row.

        Returns:
            A RealDictRow or None.
        """
        if self._noop:
            return None
        return self._cursor.fetchone()

    def fetchall(self) -> list[Any]:
        """Fetch all remaining rows.

        Returns:
            List of RealDictRow objects.
        """
        if self._noop:
            return []
        return list(self._cursor.fetchall())


class PostgresConnection:
    """Wraps a ``psycopg2`` connection to satisfy :class:`DatabaseConnection`.

    Translates ``?`` placeholders to ``%s``, injects ``RETURNING id``
    for INSERTs, and skips SQLite-specific PRAGMAs.
    """

    def __init__(self, raw: Any) -> None:
        """Initialize with a raw psycopg2 connection.

        Args:
            raw: The underlying psycopg2 connection.
        """
        self._conn = raw

    @property
    def raw(self) -> Any:
        """Access the underlying psycopg2 connection.

        Returns:
            The wrapped psycopg2 connection.
        """
        return self._conn

    def execute(self, sql: str, params: Sequence[Any] = ()) -> PostgresCursorResult:
        """Execute a SQL statement, translating SQLite dialect to PostgreSQL.

        Converts ``?`` -> ``%s`` and injects ``RETURNING id`` for INSERTs.
        Silently skips ``PRAGMA`` statements (SQLite-specific).

        Args:
            sql: SQL string with ``?`` parameter placeholders.
            params: Parameter values.

        Returns:
            A PostgresCursorResult wrapping the cursor.
        """
        stripped = sql.strip()
        if stripped.upper().startswith("PRAGMA"):
            cursor = self._conn.cursor(
                cursor_factory=psycopg2.extras.RealDictCursor,
            )
            return PostgresCursorResult(cursor, noop=True)

        translated = _translate_placeholders(stripped)
        translated, returning_injected = _inject_returning(translated)

        cursor = self._conn.cursor(
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        cursor.execute(translated, params or None)
        return PostgresCursorResult(cursor, returning_injected)

    def executescript(self, sql: str) -> None:
        """Execute a multi-statement SQL script.

        Unlike SQLite's ``executescript()``, psycopg2 handles
        multi-statement strings in a single ``execute()`` call.

        Args:
            sql: Multi-statement SQL string.
        """
        cursor = self._conn.cursor()
        cursor.execute(sql)
        self._conn.commit()

    def commit(self) -> None:
        """Commit the current transaction."""
        self._conn.commit()

    def close(self) -> None:
        """Close the underlying connection."""
        self._conn.close()
