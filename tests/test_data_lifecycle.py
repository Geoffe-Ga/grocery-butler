"""Tests for household data export, delete and restore (ADR 0013)."""

from __future__ import annotations

import copy
import datetime as dt
import json
import os
from contextlib import closing
from decimal import Decimal
from pathlib import Path
from typing import TYPE_CHECKING, Any
from urllib.parse import quote

import pytest

from grocery_butler.cli import main
from grocery_butler.constraints import HouseholdRules
from grocery_butler.data_lifecycle import (
    DELETE_PHRASE,
    EXPORT_FORMAT,
    RESTORE_PHRASE,
    SYSTEM_TABLES,
    BackupFormatError,
    ConfirmationError,
    HouseholdData,
    LifecycleError,
    _plain,
    _storable,
    dialect_of,
    quote_identifier,
)
from grocery_butler.db import get_connection, init_db
from grocery_butler.db.migrate import latest_version, migrate
from grocery_butler.household_items import HouseholdItemStore
from grocery_butler.inventory_ledger import InventoryLedger
from grocery_butler.members import MemberStore
from grocery_butler.models import (
    HardConstraintKind,
    NewObservation,
    ObservationSource,
    StockState,
)
from grocery_butler.observation_batches import ObservationBatches
from grocery_butler.replenishment import ReplenishmentNeeds

if TYPE_CHECKING:
    from collections.abc import Iterator

NOW = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.UTC)
FIXTURES = Path(__file__).parent / "fixtures"
ACTOR = "lifecycle-test"


def _clock() -> dt.datetime:
    """Return a fixed time."""
    return NOW


def _populate(db_path: str) -> None:
    """Give the household members, items, a merge, stock, a need and a rule."""
    MemberStore(db_path).add("geoff", "Geoff", created_by=ACTOR)
    items = HouseholdItemStore(db_path)
    oat = items.resolve_or_create("oat milk", actor=ACTOR)
    # The duplicate is created first-to-last so its merge pointer refers to a
    # lower id; "oatmilk" below points the other way, at a higher id.
    early = items.resolve_or_create("oatmilk", actor=ACTOR)
    late = items.resolve_or_create("oat drink", actor=ACTOR)
    items.merge(late.id, oat.id, actor=ACTOR)
    items.merge(early.id, oat.id, actor=ACTOR)
    ledger = InventoryLedger(db_path)
    first = ledger.record(
        NewObservation(
            item_id=oat.id,
            state=StockState.ON_HAND,
            source=ObservationSource.HUMAN,
            actor=ACTOR,
            quantity=1.5,
            unit="carton",
        )
    )
    ledger.correct(
        first.id,
        NewObservation(
            item_id=oat.id,
            state=StockState.LOW,
            source=ObservationSource.HUMAN,
            actor=ACTOR,
        ),
    )
    ReplenishmentNeeds(db_path).sync_item(oat.id)
    HouseholdRules(db_path).add_hard(
        HardConstraintKind.ALLERGEN_EXCLUSION, "sesame", actor=ACTOR
    )


def _without_time(document: dict[str, Any]) -> dict[str, Any]:
    """Return an export without the moment it was taken."""
    return {k: v for k, v in document.items() if k != "exported_at"}


def _raw_tables(db_path: str) -> set[str]:
    """List every table in a SQLite database straight from its catalogue."""
    with closing(get_connection(db_path)) as conn:
        rows = conn.execute(
            "SELECT name FROM sqlite_master WHERE type = 'table'"
            " AND name NOT LIKE 'sqlite_%'"
        ).fetchall()
    return {row["name"] for row in rows}


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a migrated, populated temporary database path."""
    path = str(tmp_path / "lifecycle.db")
    init_db(path)
    _populate(path)
    return path


class TestIdentifiers:
    """Names that reach a statement are plain identifiers or nothing."""

    @pytest.mark.parametrize("name", ["recipes", "household_items", "_x9"])
    def test_plain_names_are_quoted(self, name: str) -> None:
        """A catalogue name comes back in double quotes."""
        assert quote_identifier(name) == f'"{name}"'

    @pytest.mark.parametrize(
        "name",
        [
            'recipes"; DROP TABLE recipes; --',
            "recipes; DELETE FROM recipes",
            "Recipes",
            "re cipes",
            "9lives",
            "",
            "recipes\n",
        ],
    )
    def test_anything_else_is_refused(self, name: str) -> None:
        """A name with quotes, spaces, capitals or punctuation never passes."""
        with pytest.raises(BackupFormatError, match="not a table or column name"):
            quote_identifier(name)

    def test_non_text_is_refused(self) -> None:
        """A number where a name belongs is refused, not coerced."""
        bad: Any = 7
        with pytest.raises(BackupFormatError):
            quote_identifier(bad)

    @pytest.mark.parametrize(
        ("path", "dialect"),
        [
            ("mealbot.db", "sqlite"),
            ("postgresql://u@h/db", "postgres"),
            ("postgres://u@h/db", "postgres"),
        ],
    )
    def test_dialect_of(self, path: str, dialect: str) -> None:
        """The dialect follows the URL scheme."""
        assert dialect_of(path) == dialect


class TestValues:
    """Values cross between the database driver and JSON without loss."""

    @pytest.mark.parametrize(
        ("stored", "plain"),
        [
            (
                dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.UTC),
                "2026-11-26T12:00:00+00:00",
            ),
            (dt.date(2026, 11, 26), "2026-11-26"),
            (Decimal("1.50"), "1.50"),
            ("text", "text"),
            (None, None),
            (True, True),
            (3, 3),
            ({"a": 1}, {"a": 1}),
        ],
    )
    def test_plain(self, stored: object, plain: object) -> None:
        """Dates and decimals become text; everything else passes through."""
        assert _plain(stored) == plain

    @pytest.mark.parametrize(
        ("value", "storable"),
        [
            ({"b": 1, "a": [2]}, '{"b": 1, "a": [2]}'),
            ([1, "x"], '[1, "x"]'),
            ("text", "text"),
            (None, None),
            (7, 7),
        ],
    )
    def test_storable(self, value: object, storable: object) -> None:
        """JSON objects and arrays are bound as JSON text."""
        assert _storable(value) == storable


class TestExport:
    """An export holds every household table and nothing else."""

    def test_export_covers_every_household_table(self, db_path: str) -> None:
        """Every table in the database is exported, except bookkeeping."""
        document = HouseholdData(db_path, clock=_clock).export()

        assert set(document["tables"]) == _raw_tables(db_path) - SYSTEM_TABLES
        assert "schema_migrations" not in document["tables"]

    def test_export_includes_the_archived_retailer_tables(self, db_path: str) -> None:
        """The archived Safeway tables are household data too."""
        tables = HouseholdData(db_path).export()["tables"]

        assert "archived_product_mapping" in tables
        assert "archived_order_submissions" in tables

    def test_export_header(self, db_path: str) -> None:
        """The header says how to read the document and when it was taken."""
        document = HouseholdData(db_path, clock=_clock).export()

        assert document["format"] == EXPORT_FORMAT
        assert document["dialect"] == "sqlite"
        assert document["schema_version"] == latest_version()
        assert document["exported_at"] == "2026-11-26T12:00:00+00:00"

    def test_export_carries_the_rows(self, db_path: str) -> None:
        """Rows come out whole, in id order."""
        tables = HouseholdData(db_path).export()["tables"]

        assert [m["member_key"] for m in tables["household_members"]] == ["geoff"]
        ids = [row["id"] for row in tables["household_items"]]
        assert ids == sorted(ids)
        merged = [r for r in tables["household_items"] if r["merged_into_id"]]
        assert len(merged) == 2
        assert [r["value"] for r in tables["hard_constraints"]] == ["sesame"]
        assert len(tables["inventory_observations"]) == 2

    def test_export_is_json(self, db_path: str) -> None:
        """The document survives a trip through JSON text unchanged."""
        document = HouseholdData(db_path, clock=_clock).export()

        assert json.loads(json.dumps(document)) == document

    def test_export_is_repeatable(self, db_path: str) -> None:
        """Two exports of an unchanged database are identical."""
        data = HouseholdData(db_path, clock=_clock)

        assert data.export() == data.export()

    def test_tables_and_schema_version(self, db_path: str) -> None:
        """The catalogue helpers agree with the export."""
        data = HouseholdData(db_path)

        assert data.tables() == sorted(data.export()["tables"])
        assert data.schema_version() == latest_version()


class TestUnmigrated:
    """A database with no recorded migrations can't be exported."""

    def test_no_recorded_migrations_is_an_error(self, db_path: str) -> None:
        """The version is never guessed."""
        with closing(get_connection(db_path)) as conn:
            conn.execute("DELETE FROM schema_migrations")
            conn.commit()

        with pytest.raises(LifecycleError, match="records no migrations"):
            HouseholdData(db_path).export()
        with pytest.raises(LifecycleError, match="records no migrations"):
            HouseholdData(db_path).schema_version()


class TestDelete:
    """Delete removes every household row, and only when confirmed."""

    @pytest.mark.parametrize(
        "phrase", ["", "yes", "DELETE ALL HOUSEHOLD DATA", RESTORE_PHRASE]
    )
    def test_wrong_phrase_deletes_nothing(self, db_path: str, phrase: str) -> None:
        """Anything but the exact phrase is refused and changes nothing."""
        data = HouseholdData(db_path, clock=_clock)
        before = data.export()

        with pytest.raises(ConfirmationError, match=DELETE_PHRASE) as raised:
            data.delete(phrase)

        assert raised.value.phrase == DELETE_PHRASE
        assert "Nothing was changed" in str(raised.value)
        assert data.export() == before

    def test_delete_empties_every_table(self, db_path: str) -> None:
        """Every household table is empty afterwards, seeded rows included."""
        data = HouseholdData(db_path)

        data.delete(DELETE_PHRASE)

        assert all(rows == [] for rows in data.export()["tables"].values())

    def test_delete_reports_what_it_removed(self, db_path: str) -> None:
        """The counts match what the export held."""
        data = HouseholdData(db_path)
        expected = {t: len(rows) for t, rows in data.export()["tables"].items()}

        removed = data.delete(DELETE_PHRASE)

        assert removed == expected
        assert removed["household_members"] == 1

    def test_delete_keeps_the_schema(self, db_path: str) -> None:
        """The tables and the migration record stay, so the app still runs."""
        data = HouseholdData(db_path)
        tables = data.tables()

        data.delete(DELETE_PHRASE)

        assert data.tables() == tables
        assert data.schema_version() == latest_version()
        assert migrate(db_path) == 0
        MemberStore(db_path).add("jess", "Jess", created_by=ACTOR)

    def test_delete_twice_is_harmless(self, db_path: str) -> None:
        """Deleting an already empty household removes nothing."""
        data = HouseholdData(db_path)
        data.delete(DELETE_PHRASE)

        assert set(data.delete(DELETE_PHRASE).values()) == {0}


class TestRestore:
    """Restore rebuilds the household from a backup."""

    def test_export_delete_restore_round_trip(self, db_path: str) -> None:
        """Export, delete, restore: the household is back as it was."""
        data = HouseholdData(db_path, clock=_clock)
        backup = json.loads(json.dumps(data.export()))
        data.delete(DELETE_PHRASE)

        loaded = data.restore(backup, RESTORE_PHRASE)

        assert data.export() == backup
        assert loaded == {t: len(rows) for t, rows in backup["tables"].items()}

    def test_restore_replaces_what_is_there(self, db_path: str) -> None:
        """Rows added after the backup are gone after the restore."""
        data = HouseholdData(db_path, clock=_clock)
        backup = data.export()
        MemberStore(db_path).add("jess", "Jess", created_by=ACTOR)

        data.restore(backup, RESTORE_PHRASE)

        assert [m.key for m in MemberStore(db_path).list_members()] == ["geoff"]

    def test_restore_into_a_brand_new_database(
        self, db_path: str, tmp_path: Path
    ) -> None:
        """A backup restores into a database that has never been migrated."""
        backup = HouseholdData(db_path, clock=_clock).export()
        fresh = str(tmp_path / "fresh.db")

        HouseholdData(fresh, clock=_clock).restore(backup, RESTORE_PHRASE)

        assert HouseholdData(fresh, clock=_clock).export() == backup

    def test_the_restored_household_keeps_working(self, db_path: str) -> None:
        """New rows get fresh ids and the stores read what was restored."""
        data = HouseholdData(db_path)
        backup = data.export()
        data.delete(DELETE_PHRASE)
        data.restore(backup, RESTORE_PHRASE)

        items = HouseholdItemStore(db_path)
        oat = items.resolve("oat drink")
        new = items.resolve_or_create("rye bread", actor=ACTOR)

        assert oat is not None
        assert oat.name == "oat milk"
        assert new.id > max(row["id"] for row in backup["tables"]["household_items"])
        assert ReplenishmentNeeds(db_path).open_need_for_item(oat.id) is not None

    @pytest.mark.parametrize("phrase", ["", "yes", DELETE_PHRASE])
    def test_wrong_phrase_restores_nothing(self, db_path: str, phrase: str) -> None:
        """Without the exact phrase the database is untouched."""
        data = HouseholdData(db_path, clock=_clock)
        backup = data.export()
        MemberStore(db_path).add("jess", "Jess", created_by=ACTOR)
        before = data.export()

        with pytest.raises(ConfirmationError, match=RESTORE_PHRASE):
            data.restore(backup, phrase)

        assert data.export() == before

    @pytest.mark.parametrize(
        ("change", "message"),
        [
            ({"format": 99}, "not a Grocery Butler backup"),
            ({"dialect": "postgres"}, "same kind of database"),
            ({"schema_version": None}, "which schema version"),
            ({"schema_version": True}, "which schema version"),
            ({"schema_version": 0}, "which schema version"),
            ({"schema_version": 10_000}, "newer release"),
            ({"tables": {}}, "holds no tables"),
            ({"tables": []}, "holds no tables"),
            ({"tables": {"recipes": "rows"}}, "not a list of rows"),
            ({"tables": {"recipes": ["row"]}}, "not a list of rows"),
            ({"tables": {"x; DROP TABLE recipes": []}}, "not a table or column"),
            ({"tables": {"recipes": [{'id" = 1; --': 1}]}}, "not a table or column"),
        ],
    )
    def test_a_bad_backup_changes_nothing(
        self, db_path: str, change: dict[str, Any], message: str
    ) -> None:
        """A document that fails the check is refused before any change."""
        data = HouseholdData(db_path, clock=_clock)
        before = data.export()
        backup = {**copy.deepcopy(before), **change}

        with pytest.raises(BackupFormatError, match=message):
            data.restore(backup, RESTORE_PHRASE)

        assert data.export() == before

    def test_not_a_document_at_all(self, db_path: str) -> None:
        """A list where the document belongs is refused."""
        bad: Any = ["not", "a", "backup"]

        with pytest.raises(BackupFormatError, match="not a Grocery Butler backup"):
            HouseholdData(db_path).restore(bad, RESTORE_PHRASE)

    def test_tables_that_dont_match_the_version_are_refused(self, db_path: str) -> None:
        """A backup missing a table its version has is not loaded."""
        data = HouseholdData(db_path)
        backup = data.export()
        del backup["tables"]["household_members"]

        with pytest.raises(BackupFormatError, match="household_members"):
            data.restore(backup, RESTORE_PHRASE)


class TestCli:
    """The ``data`` command: export, delete and restore from a terminal."""

    def _run(self, *argv: str) -> None:
        """Run the CLI and require a clean exit."""
        with pytest.raises(SystemExit) as raised:
            main(["data", *argv])
        assert raised.value.code == 0

    def _fail(self, *argv: str) -> None:
        """Run the CLI and require a failing exit."""
        with pytest.raises(SystemExit) as raised:
            main(["data", *argv])
        assert raised.value.code == 1

    def test_export_prints_the_document(
        self, db_path: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Without --output the export goes to standard output."""
        self._run("export", "--database", db_path)

        document = json.loads(capsys.readouterr().out)
        assert _without_time(document) == _without_time(HouseholdData(db_path).export())

    def test_export_to_a_private_file(
        self, db_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """--output writes a file only its owner can read."""
        target = tmp_path / "household.json"

        self._run("export", "--database", db_path, "--output", str(target))

        assert target.stat().st_mode & 0o777 == 0o600
        assert json.loads(target.read_text())["format"] == EXPORT_FORMAT
        assert str(target) in capsys.readouterr().out

    def test_export_tightens_a_file_that_already_exists(
        self, db_path: str, tmp_path: Path
    ) -> None:
        """A readable file already at the path ends up private too."""
        target = tmp_path / "household.json"
        target.write_text("old")
        target.chmod(0o644)

        self._run("export", "--database", db_path, "--output", str(target))

        assert target.stat().st_mode & 0o777 == 0o600
        assert json.loads(target.read_text())["format"] == EXPORT_FORMAT

    def test_database_comes_from_the_environment(
        self,
        db_path: str,
        capsys: pytest.CaptureFixture[str],
        monkeypatch: pytest.MonkeyPatch,
    ) -> None:
        """DATABASE_URL wins over DATABASE_PATH, as it does for the app."""
        monkeypatch.setenv("DATABASE_URL", db_path)
        monkeypatch.setenv("DATABASE_PATH", "/nonexistent/elsewhere.db")

        self._run("export")

        members = json.loads(capsys.readouterr().out)["tables"]["household_members"]
        assert [m["member_key"] for m in members] == ["geoff"]

    def test_delete_without_the_phrase_explains_and_keeps_everything(
        self, db_path: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A bare delete tells the person the phrase and deletes nothing."""
        self._fail("delete", "--database", db_path)

        assert DELETE_PHRASE in capsys.readouterr().err
        assert len(MemberStore(db_path).list_members()) == 1

    def test_delete_with_the_phrase(
        self, db_path: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The confirmed delete empties the household and says so."""
        self._run("delete", "--database", db_path, "--confirm", DELETE_PHRASE)

        assert "The household's data is gone." in capsys.readouterr().out
        assert MemberStore(db_path).list_members() == []

    def test_export_delete_restore(
        self, db_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """The three commands together bring the household back."""
        target = str(tmp_path / "household.json")
        before = _without_time(HouseholdData(db_path).export())
        self._run("export", "--database", db_path, "--output", target)
        self._run("delete", "--database", db_path, "--confirm", DELETE_PHRASE)

        self._run("restore", target, "--database", db_path, "--confirm", RESTORE_PHRASE)

        assert "Restored" in capsys.readouterr().out
        assert _without_time(HouseholdData(db_path).export()) == before

    def test_restore_without_the_phrase(
        self, db_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A bare restore tells the person the phrase and changes nothing."""
        target = str(tmp_path / "household.json")
        self._run("export", "--database", db_path, "--output", target)

        self._fail("restore", target, "--database", db_path)

        assert RESTORE_PHRASE in capsys.readouterr().err
        assert len(MemberStore(db_path).list_members()) == 1

    def test_restore_needs_a_file(
        self, db_path: str, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Restore with no file named says what is missing."""
        self._fail("restore", "--database", db_path, "--confirm", RESTORE_PHRASE)

        assert "Name the export file" in capsys.readouterr().err

    def test_restore_from_a_missing_file(
        self, db_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """A path that isn't there is reported, not raised."""
        missing = str(tmp_path / "nope.json")

        self._fail(
            "restore", missing, "--database", db_path, "--confirm", RESTORE_PHRASE
        )

        assert f"{missing}: No such file or directory." in capsys.readouterr().err

    def test_restore_from_a_file_that_is_not_json(
        self, db_path: str, tmp_path: Path, capsys: pytest.CaptureFixture[str]
    ) -> None:
        """Garbage in the file is reported and nothing changes."""
        garbage = tmp_path / "garbage.json"
        garbage.write_text("not json")

        self._fail(
            "restore", str(garbage), "--database", db_path, "--confirm", RESTORE_PHRASE
        )

        assert "is not a JSON export" in capsys.readouterr().err
        assert len(MemberStore(db_path).list_members()) == 1


class TestMigrateTarget:
    """The runner can stop at a version, for restoring older backups."""

    def test_target_stops_at_that_version(self, tmp_path: Path) -> None:
        """Migrating to version 9 leaves later tables uncreated."""
        path = str(tmp_path / "partial.db")

        applied = migrate(path, target=9)

        assert applied == 9
        assert HouseholdData(path).schema_version() == 9
        assert "household_items" not in _raw_tables(path)

    def test_the_rest_apply_later(self, tmp_path: Path) -> None:
        """A second run without a target finishes the job."""
        path = str(tmp_path / "partial.db")
        migrate(path, target=9)

        applied = migrate(path)

        assert applied == latest_version() - 9
        assert HouseholdData(path).schema_version() == latest_version()


class TestFixtureBackup:
    """A backup committed at schema version 17 must restore forever.

    This is the restore drill CI runs: the fixture stays as it was written,
    and every later migration has to carry its data forward.
    """

    def _fixture(self, name: str) -> dict[str, Any]:
        """Load a committed backup."""
        document: dict[str, Any] = json.loads((FIXTURES / name).read_text())
        return document

    def test_fixture_restores_through_later_migrations(self, tmp_path: Path) -> None:
        """The version 17 backup restores into today's schema."""
        backup = self._fixture("backup_v17_sqlite.json")
        path = str(tmp_path / "restored.db")

        loaded = HouseholdData(path).restore(backup, RESTORE_PHRASE)

        assert backup["schema_version"] == 17
        assert loaded["household_members"] == 1
        assert HouseholdData(path).schema_version() == latest_version()
        assert [m.key for m in MemberStore(path).list_members()] == ["geoff"]
        oat = HouseholdItemStore(path).resolve("oat drink")
        assert oat is not None
        assert oat.name == "oat milk"
        snapshot = InventoryLedger(path).snapshot(oat.id)
        assert snapshot.state is StockState.LOW
        rules = HouseholdRules(path).list_hard()
        assert [rule.value for rule in rules] == ["sesame"]
        assert ObservationBatches(path).list_batches() == []


_PG_URL = os.environ.get("TEST_DATABASE_URL", "")
_PG_SCHEMA = "lifecycle_test"


@pytest.mark.skipif(not _PG_URL, reason="TEST_DATABASE_URL not set")
class TestPostgres:
    """The lifecycle works on Postgres, in a schema of its own.

    Delete and restore touch every table, so these tests run in a private
    schema and can't disturb the other Postgres suites.
    """

    @pytest.fixture()
    def pg_url(self) -> Iterator[str]:
        """Create the private schema, migrate it, and drop it afterwards."""
        self._reset_schema(create=True)
        url = f"{_PG_URL}?options={quote(f'-csearch_path={_PG_SCHEMA}')}"
        init_db(url)
        yield url
        self._reset_schema(create=False)

    def _reset_schema(self, *, create: bool) -> None:
        """Drop the private schema and optionally create it again."""
        with closing(get_connection(_PG_URL)) as conn:
            conn.execute("DROP SCHEMA IF EXISTS lifecycle_test CASCADE")
            if create:
                conn.execute("CREATE SCHEMA lifecycle_test")
            conn.commit()

    def test_export_delete_restore_round_trip(self, pg_url: str) -> None:
        """Export, delete, restore returns the household on Postgres."""
        _populate(pg_url)
        data = HouseholdData(pg_url, clock=_clock)
        backup = json.loads(json.dumps(data.export()))

        removed = data.delete(DELETE_PHRASE)
        emptied = data.export()
        data.restore(backup, RESTORE_PHRASE)

        assert backup["dialect"] == "postgres"
        assert removed["household_members"] == 1
        assert all(rows == [] for rows in emptied["tables"].values())
        assert _without_time(data.export()) == _without_time(backup)

    def test_new_rows_get_fresh_ids_after_a_restore(self, pg_url: str) -> None:
        """The id sequences move past the restored ids."""
        _populate(pg_url)
        data = HouseholdData(pg_url)
        backup = data.export()
        data.restore(backup, RESTORE_PHRASE)

        new = HouseholdItemStore(pg_url).resolve_or_create("rye bread", actor=ACTOR)

        assert new.id > max(row["id"] for row in backup["tables"]["household_items"])

    def test_fixture_restores_through_later_migrations(self, pg_url: str) -> None:
        """The committed Postgres backup restores into today's schema."""
        backup = json.loads((FIXTURES / "backup_v17_postgres.json").read_text())

        HouseholdData(pg_url).restore(backup, RESTORE_PHRASE)

        assert HouseholdData(pg_url).schema_version() == latest_version()
        assert [m.key for m in MemberStore(pg_url).list_members()] == ["geoff"]
        oat = HouseholdItemStore(pg_url).resolve("oat drink")
        assert oat is not None
        assert InventoryLedger(pg_url).snapshot(oat.id).state is StockState.LOW

    def test_a_sqlite_backup_is_refused(self, pg_url: str) -> None:
        """A backup from the other dialect is refused before any change."""
        backup = json.loads((FIXTURES / "backup_v17_sqlite.json").read_text())
        data = HouseholdData(pg_url, clock=_clock)
        before = data.export()

        with pytest.raises(BackupFormatError, match="same kind of database"):
            data.restore(backup, RESTORE_PHRASE)

        assert data.export() == before
