"""Household data lifecycle: export, delete and restore (ADR 0013).

The household owns its data, so it can take all of it away, remove all of
it, and get it back from a backup:

- :meth:`HouseholdData.export` reads every household table into one JSON
  document. The same document is the backup format.
- :meth:`HouseholdData.delete` removes every household row. It can't be
  undone, so it needs the confirmation phrase.
- :meth:`HouseholdData.restore` rebuilds the database from a backup: it
  recreates the schema the backup was taken at, loads the rows, then
  applies the migrations written since. A backup therefore stays
  restorable as the schema moves on.

Tables are discovered from the database, never listed by hand, so a new
migration's table is covered the day it lands.

Table and column names can't be bound as SQL parameters. Every name that
reaches a statement here goes through :func:`quote_identifier`, which
accepts only plain lowercase identifiers and refuses anything else.
"""

from __future__ import annotations

import datetime as dt
import json
import re
from contextlib import closing
from decimal import Decimal
from graphlib import TopologicalSorter
from typing import TYPE_CHECKING, Any

from grocery_butler.db import get_connection
from grocery_butler.db.migrate import latest_version, migrate
from grocery_butler.inventory_ledger import utc_now

if TYPE_CHECKING:
    from collections.abc import Callable, Mapping

    from grocery_butler.db.adapter import DatabaseConnection

EXPORT_FORMAT = 1
DELETE_PHRASE = "delete all household data"
RESTORE_PHRASE = "replace all household data"
_DELETE_ACTION = "delete all household data"
_RESTORE_ACTION = "replace all household data with a backup"

# Bookkeeping the migration runner owns. It describes the schema, not the
# household, and a restore rebuilds it by running the migrations.
SYSTEM_TABLES = frozenset({"schema_migrations"})

SQLITE = "sqlite"
POSTGRES = "postgres"

_IDENTIFIER_RE = re.compile(r"[a-z_][a-z0-9_]*")
_PRIMARY_KEY = "id"

_SQLITE_TABLES = (
    "SELECT name FROM sqlite_master"
    " WHERE type = 'table' AND name NOT LIKE 'sqlite_%' ORDER BY name"
)
_POSTGRES_TABLES = (
    "SELECT table_name AS name FROM information_schema.tables"
    " WHERE table_schema = current_schema() AND table_type = 'BASE TABLE'"
    " ORDER BY table_name"
)
_POSTGRES_FOREIGN_KEYS = (
    "SELECT kcu.table_name AS child, kcu.column_name AS child_column,"
    " ccu.table_name AS parent"
    " FROM information_schema.table_constraints tc"
    " JOIN information_schema.key_column_usage kcu"
    "   ON kcu.constraint_name = tc.constraint_name"
    "  AND kcu.table_schema = tc.table_schema"
    " JOIN information_schema.constraint_column_usage ccu"
    "   ON ccu.constraint_name = tc.constraint_name"
    "  AND ccu.table_schema = tc.table_schema"
    " WHERE tc.constraint_type = 'FOREIGN KEY'"
    "   AND tc.table_schema = current_schema()"
)
_SCHEMA_VERSION = "SELECT MAX(version) AS version FROM schema_migrations"
_POSTGRES_SET_SEQUENCE = "SELECT setval(pg_get_serial_sequence(?, ?), ?)"

# Statement shapes. ``{table}`` and the column lists are filled only with
# names that passed quote_identifier.
_SQLITE_FOREIGN_KEYS = "PRAGMA foreign_key_list({table})"
_READ_ROWS = "SELECT * FROM {table} ORDER BY 1"
_REMOVE_ROWS = "DELETE FROM {table}"
_ADD_ROW = "INSERT INTO {table} ({columns}) VALUES ({values}) RETURNING {first}"
_SET_COLUMN = "UPDATE {table} SET {column} = ? WHERE {key} = ?"
_SQLITE_DROP = "DROP TABLE IF EXISTS {table}"
_POSTGRES_DROP = "DROP TABLE IF EXISTS {table} CASCADE"


class LifecycleError(Exception):
    """Base class for export, delete and restore failures."""


class ConfirmationError(LifecycleError):
    """Raised when an irreversible action is asked for without its phrase."""

    def __init__(self, action: str, phrase: str) -> None:
        """Say which phrase the action needs.

        Args:
            action: What was asked for, e.g. ``delete``.
            phrase: The exact phrase that confirms it.
        """
        self.phrase = phrase
        super().__init__(
            f"Nothing was changed. To {action}, confirm with the exact phrase:"
            f' "{phrase}".'
        )


class BackupFormatError(LifecycleError, ValueError):
    """Raised when a backup document can't be restored as it stands."""


def quote_identifier(name: str) -> str:
    """Quote a table or column name for use in a statement.

    Args:
        name: A name read from the database catalogue or a backup.

    Returns:
        The name in double quotes.

    Raises:
        BackupFormatError: If the name is not a plain lowercase identifier.
    """
    if not isinstance(name, str) or not _IDENTIFIER_RE.fullmatch(name):
        msg = f"{name!r} is not a table or column name this database could hold."
        raise BackupFormatError(msg)
    return f'"{name}"'


def dialect_of(db_path: str) -> str:
    """Name the database dialect behind a path or URL.

    Args:
        db_path: Database file path or PostgreSQL URL.

    Returns:
        ``postgres`` or ``sqlite``.
    """
    if db_path.startswith(("postgresql://", "postgres://")):
        return POSTGRES
    return SQLITE


def _plain(value: Any) -> Any:
    """Turn one stored value into something JSON can carry.

    Args:
        value: A value as the database driver returned it.

    Returns:
        The value, with dates as ISO 8601 text and decimals as text.
    """
    if isinstance(value, dt.datetime | dt.date):
        return value.isoformat()
    if isinstance(value, Decimal):
        return str(value)
    return value


def _storable(value: Any) -> Any:
    """Turn one backup value into something the driver can bind.

    Args:
        value: A value from a backup document.

    Returns:
        The value, with JSON objects and arrays as JSON text.
    """
    if isinstance(value, dict | list):
        return json.dumps(value)
    return value


def _read_version(conn: DatabaseConnection) -> int:
    """Read the newest migration a database has applied.

    Args:
        conn: An open connection.

    Returns:
        The schema version.

    Raises:
        LifecycleError: If no migration has been recorded.
    """
    row = conn.execute(_SCHEMA_VERSION).fetchone()
    version = row["version"] if row is not None else None
    if version is None:
        msg = "This database records no migrations. Run the migrations first."
        raise LifecycleError(msg)
    return int(version)


def _checked_version(document: Mapping[str, Any]) -> int:
    """Read and check a backup's schema version.

    Args:
        document: The backup document.

    Returns:
        The schema version the backup was taken at.

    Raises:
        BackupFormatError: If it is missing, not a whole number, or newer
            than this code's migrations.
    """
    version = document.get("schema_version")
    if isinstance(version, bool) or not isinstance(version, int) or version < 1:
        msg = "The backup doesn't say which schema version it was taken at."
        raise BackupFormatError(msg)
    newest = latest_version()
    if version > newest:
        msg = (
            f"The backup was taken at schema version {version}, but this copy of"
            f" Grocery Butler only knows versions up to {newest}. Restore it with"
            " a newer release."
        )
        raise BackupFormatError(msg)
    return version


def _checked_tables(document: Mapping[str, Any]) -> dict[str, list[dict[str, Any]]]:
    """Read and check a backup's tables.

    Args:
        document: The backup document.

    Returns:
        Rows by table name.

    Raises:
        BackupFormatError: If the tables are not lists of row objects, or a
            table or column name is not a plain identifier.
    """
    tables = document.get("tables")
    if not isinstance(tables, dict) or not tables:
        msg = "The backup holds no tables."
        raise BackupFormatError(msg)
    for table, rows in tables.items():
        quote_identifier(table)
        if not isinstance(rows, list) or not all(isinstance(r, dict) for r in rows):
            msg = f"The backup's {table} table is not a list of rows."
            raise BackupFormatError(msg)
        for row in rows:
            for column in row:
                quote_identifier(column)
    return tables


class HouseholdData:
    """Everything the household has stored, as one unit.

    Args:
        db_path: Database file path or PostgreSQL URL.
        clock: Returns the current time; injectable for tests.
    """

    def __init__(
        self, db_path: str, clock: Callable[[], dt.datetime] = utc_now
    ) -> None:
        """Bind to a database.

        Args:
            db_path: Database file path or PostgreSQL URL.
            clock: Returns the current time.
        """
        self._db_path = db_path
        self._clock = clock
        self._dialect = dialect_of(db_path)

    # ------------------------------------------------------------------
    # Catalogue
    # ------------------------------------------------------------------

    def _all_tables(self, conn: DatabaseConnection) -> list[str]:
        """List every table in the database, bookkeeping included.

        Args:
            conn: An open connection.

        Returns:
            Table names in alphabetical order.
        """
        sql = _POSTGRES_TABLES if self._dialect == POSTGRES else _SQLITE_TABLES
        return [row["name"] for row in conn.execute(sql).fetchall()]

    def _household_tables(self, conn: DatabaseConnection) -> list[str]:
        """List the tables that hold household data.

        Args:
            conn: An open connection.

        Returns:
            Table names in alphabetical order, without bookkeeping tables.
        """
        return [t for t in self._all_tables(conn) if t not in SYSTEM_TABLES]

    def _foreign_keys(
        self, conn: DatabaseConnection, tables: list[str]
    ) -> list[tuple[str, str, str]]:
        """Read every foreign key among the given tables.

        Args:
            conn: An open connection.
            tables: The tables to inspect.

        Returns:
            ``(child table, child column, parent table)`` triples.
        """
        if self._dialect == POSTGRES:
            rows = conn.execute(_POSTGRES_FOREIGN_KEYS).fetchall()
            return [(r["child"], r["child_column"], r["parent"]) for r in rows]
        found: list[tuple[str, str, str]] = []
        for table in tables:
            sql = _SQLITE_FOREIGN_KEYS.format(table=quote_identifier(table))
            found.extend(
                (table, row["from"], row["table"])
                for row in conn.execute(sql).fetchall()
            )
        return found

    def _load_order(
        self, conn: DatabaseConnection, tables: list[str]
    ) -> tuple[list[str], dict[str, list[str]]]:
        """Order tables so each comes after the tables it refers to.

        Args:
            conn: An open connection.
            tables: The tables to order.

        Returns:
            The ordered tables, and for each table the columns that refer
            back to the same table.
        """
        known = set(tables)
        parents: dict[str, set[str]] = {table: set() for table in tables}
        self_refs: dict[str, list[str]] = {}
        for child, column, parent in self._foreign_keys(conn, tables):
            if child not in known or parent not in known:
                continue
            if child == parent:
                self_refs.setdefault(child, []).append(column)
            else:
                parents[child].add(parent)
        return list(TopologicalSorter(parents).static_order()), self_refs

    def tables(self) -> list[str]:
        """List the tables that hold household data.

        Returns:
            Table names in alphabetical order.
        """
        with closing(get_connection(self._db_path)) as conn:
            return self._household_tables(conn)

    def schema_version(self) -> int:
        """Return the newest migration the database has applied.

        Returns:
            The schema version.
        """
        with closing(get_connection(self._db_path)) as conn:
            return _read_version(conn)

    # ------------------------------------------------------------------
    # Export
    # ------------------------------------------------------------------

    def export(self) -> dict[str, Any]:
        """Read every household table into one JSON-ready document.

        Returns:
            The export: format and schema versions, the dialect, when it
            was taken, and every row of every household table.
        """
        with closing(get_connection(self._db_path)) as conn:
            tables = {
                table: [
                    {key: _plain(value) for key, value in dict(row).items()}
                    for row in conn.execute(
                        _READ_ROWS.format(table=quote_identifier(table))
                    ).fetchall()
                ]
                for table in self._household_tables(conn)
            }
            version = _read_version(conn)
        return {
            "format": EXPORT_FORMAT,
            "dialect": self._dialect,
            "schema_version": version,
            "exported_at": self._clock().isoformat(),
            "tables": tables,
        }

    # ------------------------------------------------------------------
    # Delete
    # ------------------------------------------------------------------

    def _remove_rows(self, conn: DatabaseConnection) -> dict[str, int]:
        """Delete every household row on an open transaction.

        Args:
            conn: An open connection. The caller commits.

        Returns:
            How many rows each table lost.
        """
        tables = self._household_tables(conn)
        order, _ = self._load_order(conn, tables)
        removed: dict[str, int] = {}
        for table in reversed(order):
            sql = _REMOVE_ROWS.format(table=quote_identifier(table))
            removed[table] = conn.execute(sql).rowcount
        return removed

    def delete(self, confirm: str) -> dict[str, int]:
        """Remove every household row. This can't be undone.

        The tables stay, empty, so the application keeps running. All the
        rows go in one transaction: either the household is gone or
        nothing changed.

        Args:
            confirm: Must equal :data:`DELETE_PHRASE`.

        Returns:
            How many rows each table lost.

        Raises:
            ConfirmationError: If the phrase is wrong. Nothing is deleted.
        """
        if confirm != DELETE_PHRASE:
            raise ConfirmationError(_DELETE_ACTION, DELETE_PHRASE)
        with closing(get_connection(self._db_path)) as conn:
            removed = self._remove_rows(conn)
            conn.commit()
        return removed

    # ------------------------------------------------------------------
    # Restore
    # ------------------------------------------------------------------

    def _checked(self, document: Mapping[str, Any]) -> tuple[int, dict[str, Any]]:
        """Check a backup before anything is touched.

        Args:
            document: The backup document.

        Returns:
            The backup's schema version and its tables.

        Raises:
            BackupFormatError: If the document is not a backup this
                database can restore.
        """
        if not isinstance(document, dict) or document.get("format") != EXPORT_FORMAT:
            msg = f"This is not a Grocery Butler backup in format {EXPORT_FORMAT}."
            raise BackupFormatError(msg)
        if document.get("dialect") != self._dialect:
            msg = (
                f"The backup was taken from a {document.get('dialect')} database"
                f" and this one is {self._dialect}. Restore it into the same kind"
                " of database."
            )
            raise BackupFormatError(msg)
        return _checked_version(document), _checked_tables(document)

    def _drop_everything(self) -> None:
        """Drop every table, bookkeeping included."""
        template = _POSTGRES_DROP if self._dialect == POSTGRES else _SQLITE_DROP
        with closing(get_connection(self._db_path)) as conn:
            tables = self._all_tables(conn)
            order, _ = self._load_order(conn, tables)
            for table in reversed(order):
                conn.execute(template.format(table=quote_identifier(table)))
            conn.commit()

    def _insert_rows(
        self,
        conn: DatabaseConnection,
        table: str,
        rows: list[dict[str, Any]],
        self_refs: list[str],
    ) -> None:
        """Insert one table's rows.

        A column that refers back to its own table is loaded in a second
        pass, so a row may point at a row that comes after it.

        Args:
            conn: An open connection. The caller commits.
            table: The table to fill.
            rows: The rows from the backup.
            self_refs: Columns that refer to this same table.
        """
        quoted = quote_identifier(table)
        for row in rows:
            columns = list(row)
            sql = _ADD_ROW.format(
                table=quoted,
                columns=", ".join(quote_identifier(c) for c in columns),
                values=", ".join("?" for _ in columns),
                first=quote_identifier(columns[0]),
            )
            values = [
                None if column in self_refs else _storable(row[column])
                for column in columns
            ]
            conn.execute(sql, values).fetchall()
        for column in self_refs:
            sql = _SET_COLUMN.format(
                table=quoted,
                column=quote_identifier(column),
                key=quote_identifier(_PRIMARY_KEY),
            )
            for row in rows:
                if row.get(column) is not None:
                    conn.execute(sql, (row[column], row[_PRIMARY_KEY]))

    def _advance_sequence(
        self, conn: DatabaseConnection, table: str, rows: list[dict[str, Any]]
    ) -> None:
        """Move a Postgres id sequence past the ids just loaded.

        Rows are inserted with their own ids, which doesn't advance the
        sequence; without this the next new row would collide.

        Args:
            conn: An open connection.
            table: The table just loaded.
            rows: Its rows.
        """
        ids = [row[_PRIMARY_KEY] for row in rows if type(row.get(_PRIMARY_KEY)) is int]
        if self._dialect == POSTGRES and ids:
            conn.execute(
                _POSTGRES_SET_SEQUENCE, (table, _PRIMARY_KEY, max(ids))
            ).fetchall()

    def _load(self, tables: dict[str, list[dict[str, Any]]]) -> dict[str, int]:
        """Replace every household row with the backup's, in one transaction.

        Args:
            tables: Rows by table name, already checked.

        Returns:
            How many rows each table received.

        Raises:
            BackupFormatError: If the backup's tables are not the tables
                its schema version has.
        """
        with closing(get_connection(self._db_path)) as conn:
            present = self._household_tables(conn)
            if set(present) != set(tables):
                odd = sorted(set(present) ^ set(tables))
                msg = (
                    "The backup's tables don't match its schema version"
                    f" (differing: {', '.join(odd)}). The database was left"
                    " empty at that version."
                )
                raise BackupFormatError(msg)
            order, self_refs = self._load_order(conn, present)
            # Not redundant: the migrations just run seeded rows (default
            # staples and preferences, and items made from them). They go, so
            # the backup's rows are the only rows.
            self._remove_rows(conn)
            for table in order:
                self._insert_rows(conn, table, tables[table], self_refs.get(table, []))
                self._advance_sequence(conn, table, tables[table])
            conn.commit()
        return {table: len(rows) for table, rows in tables.items()}

    def restore(self, document: Mapping[str, Any], confirm: str) -> dict[str, int]:
        """Rebuild the database from a backup, replacing what is there.

        The backup is checked first; a document that fails the check
        changes nothing. Then the database is rebuilt at the backup's
        schema version, its rows are loaded, and the migrations written
        since are applied on top, exactly as they would have been had the
        data stayed in place.

        The rebuild is several steps, not one transaction. Take an export
        first, and stop the application while it runs.

        Args:
            document: A document :meth:`export` produced.
            confirm: Must equal :data:`RESTORE_PHRASE`.

        Returns:
            How many rows each table received.

        Raises:
            ConfirmationError: If the phrase is wrong. Nothing is changed.
            BackupFormatError: If the document can't be restored here.
        """
        if confirm != RESTORE_PHRASE:
            raise ConfirmationError(_RESTORE_ACTION, RESTORE_PHRASE)
        version, tables = self._checked(document)
        self._drop_everything()
        migrate(self._db_path, target=version)
        loaded = self._load(tables)
        migrate(self._db_path)
        return loaded
