"""Tests for the inventory observation ledger (ADR 0007, P0.5, issue #134)."""

from __future__ import annotations

import datetime as dt
import os
from contextlib import closing
from typing import TYPE_CHECKING

import pytest
from pydantic import ValidationError

from grocery_butler.db import get_connection, init_db
from grocery_butler.household_items import HouseholdItemStore, ItemNotFoundError
from grocery_butler.inventory_ledger import (
    DEFAULT_FRESHNESS_DAYS,
    FRESHNESS_DAYS,
    IMPORT_NOTE,
    AlreadySupersededError,
    InventoryLedger,
    ObservationNotFoundError,
    freshness_days,
    import_inventory_rows,
    to_utc,
)
from grocery_butler.models import (
    HouseholdItem,
    IngredientCategory,
    InventoryItem,
    InventoryStatus,
    NewObservation,
    ObservationKind,
    ObservationSource,
    StockState,
)
from grocery_butler.pantry_manager import PantryManager

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

NOW = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.UTC)
ACTOR = "geoff"


class FakeClock:
    """A clock tests move by hand."""

    def __init__(self, now: dt.datetime = NOW) -> None:
        """Start at a fixed time."""
        self.now = now

    def __call__(self) -> dt.datetime:
        """Return the current fake time."""
        return self.now

    def advance(self, **delta: float) -> None:
        """Move the clock forward."""
        self.now += dt.timedelta(**delta)


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a migrated temporary database path."""
    path = str(tmp_path / "ledger.db")
    init_db(path)
    return path


@pytest.fixture()
def clock() -> FakeClock:
    """Return a clock fixed at NOW."""
    return FakeClock()


@pytest.fixture()
def ledger(db_path: str, clock: FakeClock) -> InventoryLedger:
    """Return a ledger driven by the fake clock."""
    return InventoryLedger(db_path, clock=clock)


@pytest.fixture()
def milk(db_path: str) -> HouseholdItem:
    """Return a dairy item (10-day freshness window)."""
    return HouseholdItemStore(db_path).resolve_or_create(
        "milk", actor=ACTOR, category=IngredientCategory.DAIRY
    )


def _human(item_id: int, state: StockState, **extra: object) -> NewObservation:
    """Build a human observation."""
    return NewObservation.model_validate(
        {
            "item_id": item_id,
            "state": state,
            "source": ObservationSource.HUMAN,
            "actor": ACTOR,
            **extra,
        }
    )


class TestNewObservationRules:
    """The model refuses observations the ADRs rule out."""

    def test_unknown_cannot_be_stated(self) -> None:
        """Unknown is derived, never observed."""
        with pytest.raises(ValidationError, match="unknown is derived"):
            _human(1, StockState.UNKNOWN)

    def test_human_confidence_defaults_to_one(self) -> None:
        """A person's statement is taken at full confidence."""
        assert _human(1, StockState.LOW).confidence == 1.0

    def test_photo_must_state_confidence(self) -> None:
        """A model inference always says how sure it is."""
        with pytest.raises(ValidationError, match="must state its confidence"):
            NewObservation(
                item_id=1,
                state=StockState.LOW,
                source=ObservationSource.PHOTO,
                actor="openclaw",
            )

    @pytest.mark.parametrize("confidence", [-0.1, 1.1])
    def test_confidence_must_be_between_zero_and_one(self, confidence: float) -> None:
        """Confidence is a probability."""
        with pytest.raises(ValidationError):
            _human(1, StockState.LOW, confidence=confidence)

    @pytest.mark.parametrize(
        "source", [ObservationSource.PHOTO, ObservationSource.SYSTEM]
    )
    def test_models_and_the_system_cannot_state_a_quantity(
        self, source: ObservationSource
    ) -> None:
        """ADR 0003: a quantity is stated or read, never estimated."""
        with pytest.raises(ValidationError, match="can't state a quantity"):
            NewObservation(
                item_id=1,
                state=StockState.ON_HAND,
                source=source,
                actor="openclaw",
                confidence=0.9,
                quantity=3,
                unit="can",
            )

    @pytest.mark.parametrize(
        "source",
        [ObservationSource.HUMAN, ObservationSource.RECEIPT, ObservationSource.ORDER],
    )
    def test_people_receipts_and_orders_can_state_a_quantity(
        self, source: ObservationSource
    ) -> None:
        """Authoritative sources may carry a count."""
        observation = NewObservation(
            item_id=1,
            state=StockState.ON_HAND,
            source=source,
            actor=ACTOR,
            quantity=3,
            unit="can",
        )

        assert observation.quantity == 3

    @pytest.mark.parametrize(
        "extra", [{"quantity": 3}, {"unit": "can"}], ids=["no-unit", "no-quantity"]
    )
    def test_quantity_and_unit_travel_together(self, extra: dict[str, object]) -> None:
        """A bare number or a bare unit means nothing."""
        with pytest.raises(ValidationError, match="given together"):
            _human(1, StockState.ON_HAND, **extra)

    def test_negative_quantity_is_rejected(self) -> None:
        """You can't have less than nothing."""
        with pytest.raises(ValidationError):
            _human(1, StockState.ON_HAND, quantity=-1, unit="can")

    def test_actor_is_required(self) -> None:
        """Every observation is attributable."""
        with pytest.raises(ValidationError):
            _human(1, StockState.LOW, actor="")

    def test_observed_at_needs_a_time_zone(self) -> None:
        """A naive time can't be compared safely."""
        with pytest.raises(ValidationError, match="time zone"):
            _human(1, StockState.LOW, observed_at=dt.datetime(2026, 11, 1, 9, 0))


class TestRecordAndSnapshot:
    """The snapshot is the latest valid observation."""

    def test_item_with_no_observations_is_unknown(
        self, ledger: InventoryLedger, milk: HouseholdItem
    ) -> None:
        """No evidence means unknown, not on hand."""
        snapshot = ledger.snapshot(milk.id)

        assert snapshot.state is StockState.UNKNOWN
        assert snapshot.stated_state is None
        assert snapshot.observation is None
        assert snapshot.stale is False
        assert snapshot.age_days is None
        assert snapshot.fresh_for_days == 10

    def test_recorded_observation_becomes_the_snapshot(
        self, ledger: InventoryLedger, milk: HouseholdItem
    ) -> None:
        """The stored row carries everything needed to explain the state."""
        stored = ledger.record(
            _human(milk.id, StockState.LOW, notes="about a cup left")
        )

        snapshot = ledger.snapshot(milk.id)

        assert stored.kind is ObservationKind.OBSERVATION
        assert stored.observed_at == NOW
        assert stored.recorded_at == NOW
        assert stored.superseded_by_id is None
        assert snapshot.state is StockState.LOW
        assert snapshot.stated_state is StockState.LOW
        assert snapshot.age_days == 0
        assert snapshot.observation is not None
        assert snapshot.observation.id == stored.id
        assert snapshot.observation.actor == ACTOR
        assert snapshot.observation.source is ObservationSource.HUMAN
        assert snapshot.observation.confidence == 1.0
        assert snapshot.observation.notes == "about a cup left"

    def test_latest_observed_time_wins_not_insertion_order(
        self, ledger: InventoryLedger, milk: HouseholdItem
    ) -> None:
        """A late-arriving older observation doesn't override a newer one."""
        ledger.record(_human(milk.id, StockState.OUT))
        ledger.record(
            _human(milk.id, StockState.ON_HAND, observed_at=NOW - dt.timedelta(days=2))
        )

        assert ledger.snapshot(milk.id).state is StockState.OUT

    def test_same_instant_ties_break_by_insertion(
        self, ledger: InventoryLedger, milk: HouseholdItem
    ) -> None:
        """Two observations at one instant resolve to the later row."""
        ledger.record(_human(milk.id, StockState.OUT))
        ledger.record(_human(milk.id, StockState.LOW))

        assert ledger.snapshot(milk.id).state is StockState.LOW

    def test_quantity_and_evidence_are_stored(
        self, ledger: InventoryLedger, milk: HouseholdItem
    ) -> None:
        """A receipt's count and reference survive the round trip."""
        stored = ledger.record(
            NewObservation(
                item_id=milk.id,
                state=StockState.ON_HAND,
                source=ObservationSource.RECEIPT,
                actor=ACTOR,
                quantity=2,
                unit="gal",
                evidence_ref="receipt:2026-11-25",
            )
        )

        assert stored.quantity == 2
        assert stored.unit == "gal"
        assert stored.evidence_ref == "receipt:2026-11-25"
        assert stored.confidence is None

    def test_observed_at_is_stored_in_utc(
        self, ledger: InventoryLedger, milk: HouseholdItem
    ) -> None:
        """A local time is normalized, so ordering is consistent."""
        pacific = dt.timezone(dt.timedelta(hours=-8))
        local = dt.datetime(2026, 11, 26, 1, 0, tzinfo=pacific)

        stored = ledger.record(_human(milk.id, StockState.LOW, observed_at=local))

        assert stored.observed_at == local
        assert stored.observed_at.utcoffset() == dt.timedelta(0)

    def test_observation_for_unknown_item_raises(self, ledger: InventoryLedger) -> None:
        """An observation needs a household item."""
        with pytest.raises(ItemNotFoundError):
            ledger.record(_human(99999, StockState.LOW))

    def test_snapshot_of_unknown_item_raises(self, ledger: InventoryLedger) -> None:
        """A snapshot needs a household item."""
        with pytest.raises(ItemNotFoundError):
            ledger.snapshot(99999)

    def test_get_returns_none_for_an_unknown_observation(
        self, ledger: InventoryLedger
    ) -> None:
        """A missing observation is not an error for get."""
        assert ledger.get(99999) is None


class TestFreshness:
    """Old state turns unknown; it never silently stays on hand."""

    def test_state_holds_through_the_last_day_of_its_window(
        self, ledger: InventoryLedger, milk: HouseholdItem, clock: FakeClock
    ) -> None:
        """Exactly at the window's edge the state still counts."""
        ledger.record(_human(milk.id, StockState.ON_HAND))
        clock.advance(days=10)

        snapshot = ledger.snapshot(milk.id)

        assert snapshot.state is StockState.ON_HAND
        assert snapshot.stale is False
        assert snapshot.age_days == 10

    def test_state_becomes_unknown_once_past_its_window(
        self, ledger: InventoryLedger, milk: HouseholdItem, clock: FakeClock
    ) -> None:
        """Past the window the snapshot says unknown, and what was last said."""
        stored = ledger.record(_human(milk.id, StockState.ON_HAND))
        clock.advance(days=10, seconds=1)

        snapshot = ledger.snapshot(milk.id)

        assert snapshot.state is StockState.UNKNOWN
        assert snapshot.stale is True
        assert snapshot.stated_state is StockState.ON_HAND
        assert snapshot.observation is not None
        assert snapshot.observation.id == stored.id

    def test_a_new_observation_makes_the_item_fresh_again(
        self, ledger: InventoryLedger, milk: HouseholdItem, clock: FakeClock
    ) -> None:
        """Staleness is about the latest evidence only."""
        ledger.record(_human(milk.id, StockState.ON_HAND))
        clock.advance(days=30)

        ledger.record(_human(milk.id, StockState.LOW))

        assert ledger.snapshot(milk.id).state is StockState.LOW

    def test_every_category_has_a_window(self) -> None:
        """A new category without a window would crash snapshots."""
        assert set(FRESHNESS_DAYS) == set(IngredientCategory)
        assert all(days > 0 for days in FRESHNESS_DAYS.values())

    def test_item_without_a_category_uses_the_default_window(self) -> None:
        """No category, default window."""
        item = HouseholdItem(id=1, name="thing", display_name="Thing")

        assert freshness_days(item) == DEFAULT_FRESHNESS_DAYS == 30

    def test_dry_goods_stay_fresh_longer_than_produce(self) -> None:
        """The windows follow how fast things actually run out."""
        dry = HouseholdItem(
            id=1,
            name="rice",
            display_name="Rice",
            category=IngredientCategory.PANTRY_DRY,
        )
        produce = HouseholdItem(
            id=2, name="kale", display_name="Kale", category=IngredientCategory.PRODUCE
        )

        assert (freshness_days(dry), freshness_days(produce)) == (60, 7)


class TestCorrections:
    """History is kept; superseded rows stop counting."""

    def test_correction_replaces_the_state_and_keeps_the_original(
        self, ledger: InventoryLedger, milk: HouseholdItem
    ) -> None:
        """The wrong row stays, marked with what replaced it."""
        wrong = ledger.record(_human(milk.id, StockState.OUT))

        fixed = ledger.correct(wrong.id, _human(milk.id, StockState.ON_HAND))

        history = ledger.history(milk.id)
        assert ledger.snapshot(milk.id).state is StockState.ON_HAND
        assert [row.id for row in history] == [wrong.id, fixed.id]
        assert history[0].superseded_by_id == fixed.id
        assert history[0].state is StockState.OUT
        assert fixed.supersedes_id == wrong.id

    def test_a_backdated_correction_still_beats_what_it_corrects(
        self, ledger: InventoryLedger, milk: HouseholdItem
    ) -> None:
        """Superseding is by link, not by timestamp."""
        wrong = ledger.record(_human(milk.id, StockState.OUT))

        ledger.correct(
            wrong.id,
            _human(milk.id, StockState.LOW, observed_at=NOW - dt.timedelta(hours=1)),
        )

        assert ledger.snapshot(milk.id).state is StockState.LOW

    def test_retraction_falls_back_to_the_previous_valid_observation(
        self, ledger: InventoryLedger, milk: HouseholdItem, clock: FakeClock
    ) -> None:
        """Withdrawing the latest row reveals the one before it."""
        ledger.record(_human(milk.id, StockState.ON_HAND))
        clock.advance(hours=1)
        mistake = ledger.record(_human(milk.id, StockState.OUT))

        retraction = ledger.retract(mistake.id, actor="jess", notes="wrong shelf")

        assert ledger.snapshot(milk.id).state is StockState.ON_HAND
        assert retraction.kind is ObservationKind.RETRACTION
        assert retraction.state is None
        assert retraction.actor == "jess"
        assert retraction.notes == "wrong shelf"
        assert retraction.supersedes_id == mistake.id

    def test_retracting_the_only_observation_leaves_unknown(
        self, ledger: InventoryLedger, milk: HouseholdItem
    ) -> None:
        """With nothing valid left, we don't know."""
        only = ledger.record(_human(milk.id, StockState.OUT))

        ledger.retract(only.id, actor=ACTOR)

        snapshot = ledger.snapshot(milk.id)
        assert snapshot.state is StockState.UNKNOWN
        assert snapshot.observation is None
        assert len(ledger.history(milk.id)) == 2

    def test_an_already_corrected_row_cannot_be_corrected_again(
        self, ledger: InventoryLedger, milk: HouseholdItem
    ) -> None:
        """Corrections chain; they don't fork."""
        wrong = ledger.record(_human(milk.id, StockState.OUT))
        fixed = ledger.correct(wrong.id, _human(milk.id, StockState.LOW))

        with pytest.raises(AlreadySupersededError, match=str(fixed.id)):
            ledger.correct(wrong.id, _human(milk.id, StockState.ON_HAND))
        with pytest.raises(AlreadySupersededError):
            ledger.retract(wrong.id, actor=ACTOR)

    def test_a_correction_can_itself_be_corrected(
        self, ledger: InventoryLedger, milk: HouseholdItem
    ) -> None:
        """The latest link in the chain is the one that counts."""
        first = ledger.record(_human(milk.id, StockState.OUT))
        second = ledger.correct(first.id, _human(milk.id, StockState.LOW))

        ledger.correct(second.id, _human(milk.id, StockState.ON_HAND))

        assert ledger.snapshot(milk.id).state is StockState.ON_HAND

    def test_correcting_or_retracting_a_missing_row_raises(
        self, ledger: InventoryLedger, milk: HouseholdItem
    ) -> None:
        """You can only replace what exists."""
        with pytest.raises(ObservationNotFoundError, match="99999"):
            ledger.correct(99999, _human(milk.id, StockState.LOW))
        with pytest.raises(ObservationNotFoundError):
            ledger.retract(99999, actor=ACTOR)

    def test_correction_can_move_an_observation_to_the_right_item(
        self, ledger: InventoryLedger, milk: HouseholdItem, db_path: str
    ) -> None:
        """A sighting attached to the wrong item is fixed on both sides."""
        oat = HouseholdItemStore(db_path).resolve_or_create("oat milk", actor=ACTOR)
        wrong = ledger.record(_human(milk.id, StockState.OUT))

        ledger.correct(wrong.id, _human(oat.id, StockState.OUT))

        assert ledger.snapshot(milk.id).state is StockState.UNKNOWN
        assert ledger.snapshot(oat.id).state is StockState.OUT

    def test_ledger_rows_are_never_updated_or_deleted(
        self, ledger: InventoryLedger, milk: HouseholdItem, db_path: str
    ) -> None:
        """Corrections and retractions only ever add rows."""
        first = ledger.record(_human(milk.id, StockState.OUT))
        with closing(get_connection(db_path)) as conn:
            before = dict(
                conn.execute(
                    "SELECT * FROM inventory_observations WHERE id = ?", (first.id,)
                ).fetchall()[0]
            )

        second = ledger.correct(first.id, _human(milk.id, StockState.LOW))
        ledger.retract(second.id, actor=ACTOR)

        with closing(get_connection(db_path)) as conn:
            rows = conn.execute(
                "SELECT * FROM inventory_observations ORDER BY id"
            ).fetchall()
        assert len(rows) == 3
        assert dict(rows[0]) == before


class TestMergedItems:
    """Observations follow an item through a merge and back."""

    def test_observations_of_a_merged_item_count_for_the_survivor(
        self, ledger: InventoryLedger, db_path: str, clock: FakeClock
    ) -> None:
        """Both items' evidence forms one history."""
        items = HouseholdItemStore(db_path)
        tomato = items.resolve_or_create("tomato", actor=ACTOR)
        tomatoes = items.resolve_or_create("tomatoes", actor=ACTOR)
        ledger.record(_human(tomato.id, StockState.ON_HAND))
        clock.advance(hours=1)
        ledger.record(_human(tomatoes.id, StockState.OUT))

        items.merge(tomatoes.id, tomato.id, actor=ACTOR)

        assert ledger.snapshot(tomato.id).state is StockState.OUT
        assert ledger.snapshot(tomatoes.id).item_id == tomato.id
        assert len(ledger.history(tomato.id)) == 2

    def test_unmerge_separates_the_histories_again(
        self, ledger: InventoryLedger, db_path: str, clock: FakeClock
    ) -> None:
        """Nothing was rewritten, so the split is exact."""
        items = HouseholdItemStore(db_path)
        tomato = items.resolve_or_create("tomato", actor=ACTOR)
        tomatoes = items.resolve_or_create("tomatoes", actor=ACTOR)
        ledger.record(_human(tomato.id, StockState.ON_HAND))
        clock.advance(hours=1)
        ledger.record(_human(tomatoes.id, StockState.OUT))
        items.merge(tomatoes.id, tomato.id, actor=ACTOR)

        items.unmerge(tomatoes.id, actor=ACTOR)

        assert ledger.snapshot(tomato.id).state is StockState.ON_HAND
        assert ledger.snapshot(tomatoes.id).state is StockState.OUT

    def test_snapshots_lists_active_items_only(
        self, ledger: InventoryLedger, db_path: str
    ) -> None:
        """A merged item has no snapshot of its own in the list."""
        items = HouseholdItemStore(db_path)
        tomato = items.resolve_or_create("tomato", actor=ACTOR)
        tomatoes = items.resolve_or_create("tomatoes", actor=ACTOR)
        ledger.record(_human(tomato.id, StockState.LOW))
        items.merge(tomatoes.id, tomato.id, actor=ACTOR)

        by_item = {snapshot.item_id: snapshot for snapshot in ledger.snapshots()}

        assert tomatoes.id not in by_item
        assert by_item[tomato.id].state is StockState.LOW
        assert {s.item_id for s in ledger.snapshots()} == {
            item.id for item in items.list_items()
        }


class TestPantryManagerWritesTheLedger:
    """Every status change through the old path is also an observation."""

    @pytest.fixture()
    def manager(self, db_path: str) -> PantryManager:
        """Return a manager with one tracked item."""
        manager = PantryManager(db_path)
        manager.add_item(
            InventoryItem(
                ingredient="eggs",
                display_name="Eggs",
                category=IngredientCategory.DAIRY,
                status=InventoryStatus.ON_HAND,
            )
        )
        return manager

    def _states(self, db_path: str, name: str) -> list[StockState | None]:
        """Return the stated states in an item's ledger history."""
        item = HouseholdItemStore(db_path).resolve(name)
        assert item is not None
        return [row.state for row in InventoryLedger(db_path).history(item.id)]

    def test_add_item_records_its_starting_state(
        self, manager: PantryManager, db_path: str
    ) -> None:
        """Tracking an item is the first observation of it."""
        assert self._states(db_path, "eggs") == [StockState.ON_HAND]

    def test_update_status_appends_an_observation(
        self, manager: PantryManager, db_path: str
    ) -> None:
        """A status change is evidence, attributed and sourced."""
        manager.update_status("Eggs", InventoryStatus.LOW)

        item = HouseholdItemStore(db_path).resolve("eggs")
        assert item is not None
        latest = InventoryLedger(db_path).history(item.id)[-1]
        assert latest.state is StockState.LOW
        assert latest.source is ObservationSource.HUMAN
        assert latest.actor == "system"

    def test_update_status_of_an_untracked_item_records_nothing(
        self, manager: PantryManager, db_path: str
    ) -> None:
        """No inventory row, no observation."""
        manager.update_status("caviar", InventoryStatus.OUT)

        assert HouseholdItemStore(db_path).resolve("caviar") is None

    def test_row_that_was_never_linked_to_an_item_records_nothing(
        self, db_path: str
    ) -> None:
        """A legacy row without an item id still updates, without a ledger row."""
        with closing(get_connection(db_path)) as conn:
            conn.execute(
                "INSERT INTO household_inventory (ingredient, display_name, status)"
                " VALUES (?, ?, ?)",
                ("orphan", "Orphan", "on_hand"),
            )
            conn.commit()
        manager = PantryManager(db_path)

        manager.update_status("orphan", InventoryStatus.OUT)

        updated = manager.get_item("orphan")
        assert updated is not None
        assert updated.status is InventoryStatus.OUT
        assert HouseholdItemStore(db_path).resolve("orphan") is None

    def test_mark_restocked_records_on_hand(
        self, manager: PantryManager, db_path: str
    ) -> None:
        """Restocking is an on-hand observation."""
        manager.update_status("eggs", InventoryStatus.OUT)

        manager.mark_restocked(["EGGS", "not tracked"])

        assert self._states(db_path, "eggs")[-1] is StockState.ON_HAND
        assert len(self._states(db_path, "eggs")) == 3

    def test_clear_restock_queue_records_each_cleared_item(
        self, manager: PantryManager, db_path: str
    ) -> None:
        """Only items that were low or out get a new observation."""
        manager.add_item(InventoryItem(ingredient="rice", display_name="Rice"))
        manager.update_status("eggs", InventoryStatus.LOW)

        manager.clear_restock_queue()

        assert self._states(db_path, "eggs") == [
            StockState.ON_HAND,
            StockState.LOW,
            StockState.ON_HAND,
        ]
        assert self._states(db_path, "rice") == [StockState.ON_HAND]

    def test_update_quantity_records_the_count_with_the_current_state(
        self, manager: PantryManager, db_path: str
    ) -> None:
        """A stated count is a human observation of the current state."""
        manager.update_status("eggs", InventoryStatus.LOW)

        manager.update_quantity("eggs", 2, "each")

        item = HouseholdItemStore(db_path).resolve("eggs")
        assert item is not None
        latest = InventoryLedger(db_path).history(item.id)[-1]
        assert (latest.state, latest.quantity, latest.unit) == (
            StockState.LOW,
            2,
            "each",
        )


class TestImport:
    """Migration 011's hook imports rows written before the ledger existed."""

    def test_imports_each_linked_row_once(self, db_path: str) -> None:
        """A row becomes one system observation dated at its last change."""
        items = HouseholdItemStore(db_path)
        item = items.resolve_or_create("legacy milk", actor=ACTOR)
        with closing(get_connection(db_path)) as conn:
            conn.execute(
                "INSERT INTO household_inventory"
                " (ingredient, display_name, status, last_status_change, item_id)"
                " VALUES (?, ?, ?, ?, ?)",
                ("legacy milk", "Legacy Milk", "low", "2026-11-01 08:30:00", item.id),
            )
            conn.commit()

        import_inventory_rows(db_path)
        import_inventory_rows(db_path)

        history = InventoryLedger(db_path).history(item.id)
        assert len(history) == 1
        assert history[0].state is StockState.LOW
        assert history[0].source is ObservationSource.SYSTEM
        assert history[0].actor == "migration"
        assert history[0].notes == IMPORT_NOTE
        assert history[0].observed_at == dt.datetime(2026, 11, 1, 8, 30, tzinfo=dt.UTC)

    def test_item_with_ledger_history_is_not_imported_again(self, db_path: str) -> None:
        """Real observations are never shadowed by an import."""
        manager = PantryManager(db_path)
        manager.add_item(InventoryItem(ingredient="eggs", display_name="Eggs"))

        import_inventory_rows(db_path)

        item = HouseholdItemStore(db_path).resolve("eggs")
        assert item is not None
        assert len(InventoryLedger(db_path).history(item.id)) == 1


class TestToUtc:
    """Legacy timestamps come in several shapes."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("2026-11-01 08:30:00", dt.datetime(2026, 11, 1, 8, 30, tzinfo=dt.UTC)),
            (
                "2026-11-01T08:30:00+00:00",
                dt.datetime(2026, 11, 1, 8, 30, tzinfo=dt.UTC),
            ),
            (
                "2026-11-01T00:30:00-08:00",
                dt.datetime(2026, 11, 1, 8, 30, tzinfo=dt.UTC),
            ),
            (
                dt.datetime(2026, 11, 1, 8, 30),
                dt.datetime(2026, 11, 1, 8, 30, tzinfo=dt.UTC),
            ),
            (None, None),
            ("not a time", None),
            (12345, None),
        ],
    )
    def test_reads_strings_and_datetimes_as_utc(
        self, raw: object, expected: dt.datetime | None
    ) -> None:
        """Naive values are UTC; unusable values are None."""
        assert to_utc(raw) == expected


_PG_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.mark.skipif(not _PG_URL, reason="TEST_DATABASE_URL not set")
class TestPostgres:
    """The ledger behaves the same on Postgres."""

    @pytest.fixture()
    def pg_item(self) -> Iterator[HouseholdItem]:
        """Yield a fresh item on the Postgres test database, then remove it."""
        init_db(_PG_URL)
        self._clean()
        yield HouseholdItemStore(_PG_URL).resolve_or_create(
            "pgl milk", actor=ACTOR, category=IngredientCategory.DAIRY
        )
        self._clean()

    def _clean(self) -> None:
        """Delete this suite's item and its ledger rows."""
        with closing(get_connection(_PG_URL)) as conn:
            ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM household_items WHERE name = ?", ("pgl milk",)
                ).fetchall()
            ]
            for item_id in ids:
                conn.execute(
                    "UPDATE inventory_observations SET supersedes_id = NULL"
                    " WHERE item_id = ?",
                    (item_id,),
                )
                for statement in (
                    "DELETE FROM inventory_observations WHERE item_id = ?",
                    "DELETE FROM household_item_aliases WHERE item_id = ?",
                    "DELETE FROM household_item_events WHERE item_id = ?",
                    "DELETE FROM household_items WHERE id = ?",
                ):
                    conn.execute(statement, (item_id,))
            conn.commit()

    def test_record_correct_retract_and_staleness(self, pg_item: HouseholdItem) -> None:
        """The whole lifecycle works through the Postgres dialect."""
        clock = FakeClock()
        ledger = InventoryLedger(_PG_URL, clock=clock)

        first = ledger.record(_human(pg_item.id, StockState.OUT))
        fixed = ledger.correct(first.id, _human(pg_item.id, StockState.LOW))
        after_fix = ledger.snapshot(pg_item.id).state
        ledger.retract(fixed.id, actor=ACTOR)
        after_retract = ledger.snapshot(pg_item.id).state
        ledger.record(_human(pg_item.id, StockState.ON_HAND))
        clock.advance(days=11)
        stale = ledger.snapshot(pg_item.id)

        assert after_fix is StockState.LOW
        assert after_retract is StockState.UNKNOWN
        assert stale.state is StockState.UNKNOWN
        assert stale.stated_state is StockState.ON_HAND
        assert len(ledger.history(pg_item.id)) == 4
