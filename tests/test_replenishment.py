"""Tests for explicit replenishment needs (ADR 0008, P0.6, issue #135)."""

from __future__ import annotations

import datetime as dt
import os
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from grocery_butler.db import get_connection, init_db
from grocery_butler.household_items import HouseholdItemStore, ItemNotFoundError
from grocery_butler.inventory_ledger import InventoryLedger
from grocery_butler.models import (
    OPEN_NEED_STATUSES,
    HouseholdItem,
    IngredientCategory,
    InventoryItem,
    InventoryStatus,
    NeedReasonKind,
    NeedStatus,
    NewObservation,
    ObservationSource,
    StockState,
)
from grocery_butler.pantry_manager import PantryManager
from grocery_butler.replenishment import (
    ALLOWED_MOVES,
    INVENTORY_SOURCE,
    SYNC_ACTOR,
    InvalidNeedMoveError,
    NeedNotFoundError,
    ReplenishmentNeeds,
)

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
    path = str(tmp_path / "needs.db")
    init_db(path)
    return path


@pytest.fixture()
def clock() -> FakeClock:
    """Return a clock fixed at NOW."""
    return FakeClock()


@pytest.fixture()
def needs(db_path: str, clock: FakeClock) -> ReplenishmentNeeds:
    """Return a needs service driven by the fake clock."""
    return ReplenishmentNeeds(db_path, clock=clock)


@pytest.fixture()
def ledger(db_path: str, clock: FakeClock) -> InventoryLedger:
    """Return a ledger sharing the fake clock."""
    return InventoryLedger(db_path, clock=clock)


@pytest.fixture()
def milk(db_path: str) -> HouseholdItem:
    """Return a dairy item."""
    return HouseholdItemStore(db_path).resolve_or_create(
        "milk", actor=ACTOR, category=IngredientCategory.DAIRY
    )


def _observe(ledger: InventoryLedger, item_id: int, state: StockState) -> None:
    """Record a human observation of an item's state."""
    ledger.record(
        NewObservation(
            item_id=item_id,
            state=state,
            source=ObservationSource.HUMAN,
            actor=ACTOR,
        )
    )


def _kinds(need_reasons: list[object]) -> list[str]:
    """Return reason kinds as plain strings."""
    return [str(getattr(reason, "kind", "")) for reason in need_reasons]


class TestAddReason:
    """Reasons create a need, or join the item's open one."""

    def test_first_reason_creates_a_suggested_need(
        self, needs: ReplenishmentNeeds, milk: HouseholdItem
    ) -> None:
        """Something noticed the item; a person hasn't decided yet."""
        need = needs.add_reason(
            milk.id,
            NeedReasonKind.LOW_STOCK,
            source_key=INVENTORY_SOURCE,
            actor=ACTOR,
            detail={"observation_id": 7},
        )

        assert need.status is NeedStatus.SUGGESTED
        assert need.item_id == milk.id
        assert need.is_open is True
        assert need.created_at == NOW
        assert len(need.reasons) == 1
        reason = need.reasons[0]
        assert reason.kind is NeedReasonKind.LOW_STOCK
        assert reason.source_key == INVENTORY_SOURCE
        assert reason.detail == {"observation_id": 7}
        assert reason.active is True

    def test_manual_request_starts_approved(
        self, needs: ReplenishmentNeeds, milk: HouseholdItem
    ) -> None:
        """A person asking for something has already decided."""
        need = needs.add_reason(
            milk.id,
            NeedReasonKind.MANUAL_REQUEST,
            source_key="manual:geoff",
            actor=ACTOR,
        )

        assert need.status is NeedStatus.APPROVED
        events = needs.history(need.id)
        assert [(e.from_status, e.to_status) for e in events] == [
            (None, NeedStatus.APPROVED)
        ]
        assert events[0].actor == ACTOR

    def test_second_reason_joins_the_same_need(
        self, needs: ReplenishmentNeeds, milk: HouseholdItem
    ) -> None:
        """One item, two reasons, one need: the backlog's acceptance test."""
        first = needs.add_reason(
            milk.id, NeedReasonKind.LOW_STOCK, source_key=INVENTORY_SOURCE, actor=ACTOR
        )

        second = needs.add_reason(
            milk.id,
            NeedReasonKind.RECIPE_DEMAND,
            source_key="recipe:12",
            actor=ACTOR,
            detail={"recipe": "Pancakes", "quantity": 2, "unit": "cup"},
        )

        assert second.id == first.id
        assert _kinds(list(second.reasons)) == ["low_stock", "recipe_demand"]
        assert len(needs.list_needs()) == 1

    def test_manual_request_on_a_suggested_need_does_not_approve_it(
        self, needs: ReplenishmentNeeds, milk: HouseholdItem
    ) -> None:
        """Only a need's first reason decides where it starts."""
        needs.add_reason(
            milk.id, NeedReasonKind.LOW_STOCK, source_key=INVENTORY_SOURCE, actor=ACTOR
        )

        need = needs.add_reason(
            milk.id, NeedReasonKind.MANUAL_REQUEST, source_key="manual:x", actor=ACTOR
        )

        assert need.status is NeedStatus.SUGGESTED

    def test_same_reason_from_the_same_source_is_not_repeated(
        self, needs: ReplenishmentNeeds, milk: HouseholdItem
    ) -> None:
        """Retries and repeated syncs are safe; the detail is refreshed."""
        needs.add_reason(
            milk.id,
            NeedReasonKind.RECIPE_DEMAND,
            source_key="recipe:12",
            actor=ACTOR,
            detail={"quantity": 1},
        )

        need = needs.add_reason(
            milk.id,
            NeedReasonKind.RECIPE_DEMAND,
            source_key="recipe:12",
            actor=ACTOR,
            detail={"quantity": 2},
        )

        assert len(need.reasons) == 1
        assert need.reasons[0].detail == {"quantity": 2}

    def test_same_kind_from_two_sources_is_two_reasons(
        self, needs: ReplenishmentNeeds, milk: HouseholdItem
    ) -> None:
        """Two recipes wanting milk are both visible."""
        needs.add_reason(
            milk.id, NeedReasonKind.RECIPE_DEMAND, source_key="recipe:12", actor=ACTOR
        )

        need = needs.add_reason(
            milk.id, NeedReasonKind.RECIPE_DEMAND, source_key="recipe:40", actor=ACTOR
        )

        assert [r.source_key for r in need.reasons] == ["recipe:12", "recipe:40"]

    def test_reason_for_a_merged_item_lands_on_the_survivor(
        self, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """Needs follow the canonical item."""
        items = HouseholdItemStore(db_path)
        tomato = items.resolve_or_create("tomato", actor=ACTOR)
        tomatoes = items.resolve_or_create("tomatoes", actor=ACTOR)
        items.merge(tomatoes.id, tomato.id, actor=ACTOR)

        need = needs.add_reason(
            tomatoes.id, NeedReasonKind.OUT, source_key=INVENTORY_SOURCE, actor=ACTOR
        )

        assert need.item_id == tomato.id

    def test_reason_for_an_unknown_item_raises(self, needs: ReplenishmentNeeds) -> None:
        """A need is about a household item."""
        with pytest.raises(ItemNotFoundError):
            needs.add_reason(
                99999, NeedReasonKind.OUT, source_key=INVENTORY_SOURCE, actor=ACTOR
            )

    def test_a_closed_need_is_not_reused(
        self, needs: ReplenishmentNeeds, milk: HouseholdItem
    ) -> None:
        """After a need is received, the next reason starts a new one."""
        first = needs.add_reason(
            milk.id, NeedReasonKind.MANUAL_REQUEST, source_key="manual:x", actor=ACTOR
        )
        needs.transition(first.id, NeedStatus.PLANNED, actor=ACTOR)
        needs.transition(first.id, NeedStatus.ORDERED, actor=ACTOR)
        needs.transition(first.id, NeedStatus.RECEIVED, actor=ACTOR)

        second = needs.add_reason(
            milk.id, NeedReasonKind.LOW_STOCK, source_key=INVENTORY_SOURCE, actor=ACTOR
        )

        assert second.id != first.id
        assert second.status is NeedStatus.SUGGESTED


class TestTransitions:
    """Only the moves in ADR 0008's table are allowed."""

    def _need_in(
        self, needs: ReplenishmentNeeds, item_id: int, status: NeedStatus
    ) -> int:
        """Create a need and walk it to a status along allowed moves."""
        paths: dict[NeedStatus, list[NeedStatus]] = {
            NeedStatus.SUGGESTED: [],
            NeedStatus.APPROVED: [NeedStatus.APPROVED],
            NeedStatus.DEFERRED: [NeedStatus.DEFERRED],
            NeedStatus.DISMISSED: [NeedStatus.DISMISSED],
            NeedStatus.PLANNED: [NeedStatus.APPROVED, NeedStatus.PLANNED],
            NeedStatus.ORDERED: [
                NeedStatus.APPROVED,
                NeedStatus.PLANNED,
                NeedStatus.ORDERED,
            ],
            NeedStatus.RECEIVED: [
                NeedStatus.APPROVED,
                NeedStatus.PLANNED,
                NeedStatus.ORDERED,
                NeedStatus.RECEIVED,
            ],
            NeedStatus.CANCELLED: [NeedStatus.CANCELLED],
        }
        need = needs.add_reason(
            item_id, NeedReasonKind.LOW_STOCK, source_key=INVENTORY_SOURCE, actor=ACTOR
        )
        for step in paths[status]:
            needs.transition(need.id, step, actor=ACTOR)
        return need.id

    @pytest.mark.parametrize(
        ("start", "end"),
        [(start, end) for start, ends in ALLOWED_MOVES.items() for end in sorted(ends)],
    )
    def test_every_allowed_move_succeeds_and_is_recorded(
        self,
        needs: ReplenishmentNeeds,
        milk: HouseholdItem,
        start: NeedStatus,
        end: NeedStatus,
    ) -> None:
        """Each move in the table works and leaves an event with its actor."""
        need_id = self._need_in(needs, milk.id, start)

        moved = needs.transition(need_id, end, actor="jess", note="because")

        assert moved.status is end
        last = needs.history(need_id)[-1]
        assert (last.from_status, last.to_status) == (start, end)
        assert (last.actor, last.note) == ("jess", "because")

    @pytest.mark.parametrize(
        ("start", "end"),
        [
            (start, end)
            for start in NeedStatus
            for end in NeedStatus
            if end not in ALLOWED_MOVES.get(start, frozenset())
        ],
    )
    def test_every_other_move_is_refused_and_changes_nothing(
        self,
        needs: ReplenishmentNeeds,
        milk: HouseholdItem,
        start: NeedStatus,
        end: NeedStatus,
    ) -> None:
        """A move outside the table raises and leaves no trace."""
        need_id = self._need_in(needs, milk.id, start)
        events_before = len(needs.history(need_id))

        with pytest.raises(InvalidNeedMoveError, match=f"{start.value}.*{end.value}"):
            needs.transition(need_id, end, actor=ACTOR)

        after = needs.get(need_id)
        assert after is not None
        assert after.status is start
        assert len(needs.history(need_id)) == events_before

    def test_the_table_matches_the_adr(self) -> None:
        """Closed statuses have no way out; open ones match ADR 0008."""
        assert set(ALLOWED_MOVES) == set(OPEN_NEED_STATUSES)
        assert ALLOWED_MOVES[NeedStatus.SUGGESTED] == {
            NeedStatus.APPROVED,
            NeedStatus.DEFERRED,
            NeedStatus.DISMISSED,
            NeedStatus.CANCELLED,
        }
        assert ALLOWED_MOVES[NeedStatus.ORDERED] == {
            NeedStatus.RECEIVED,
            NeedStatus.APPROVED,
            NeedStatus.CANCELLED,
        }

    def test_transition_of_a_missing_need_raises(
        self, needs: ReplenishmentNeeds
    ) -> None:
        """You can only move a need that exists."""
        with pytest.raises(NeedNotFoundError, match="99999"):
            needs.transition(99999, NeedStatus.APPROVED, actor=ACTOR)

    def test_defer_stores_its_date_and_leaving_deferred_clears_it(
        self, needs: ReplenishmentNeeds, milk: HouseholdItem
    ) -> None:
        """The snooze date lives only while the need is deferred."""
        need_id = self._need_in(needs, milk.id, NeedStatus.SUGGESTED)
        until = NOW + dt.timedelta(days=3)

        deferred = needs.transition(
            need_id, NeedStatus.DEFERRED, actor=ACTOR, deferred_until=until
        )
        approved = needs.transition(need_id, NeedStatus.APPROVED, actor=ACTOR)

        assert deferred.deferred_until == until
        assert approved.deferred_until is None

    def test_a_date_on_any_other_move_is_refused(
        self, needs: ReplenishmentNeeds, milk: HouseholdItem
    ) -> None:
        """Only a deferral has an end date."""
        need_id = self._need_in(needs, milk.id, NeedStatus.SUGGESTED)

        with pytest.raises(ValueError, match="only a deferred need"):
            needs.transition(
                need_id,
                NeedStatus.APPROVED,
                actor=ACTOR,
                deferred_until=NOW + dt.timedelta(days=1),
            )

    def test_updated_at_moves_with_the_clock(
        self, needs: ReplenishmentNeeds, milk: HouseholdItem, clock: FakeClock
    ) -> None:
        """A need records when it last changed."""
        need_id = self._need_in(needs, milk.id, NeedStatus.SUGGESTED)
        clock.advance(hours=2)

        moved = needs.transition(need_id, NeedStatus.APPROVED, actor=ACTOR)

        assert moved.created_at == NOW
        assert moved.updated_at == NOW + dt.timedelta(hours=2)


class TestWithdrawReason:
    """A reason that stops holding is kept, inactive."""

    def test_withdrawing_the_last_reason_cancels_a_suggested_need(
        self, needs: ReplenishmentNeeds, milk: HouseholdItem
    ) -> None:
        """Nobody decided anything, and nothing wants it any more."""
        need = needs.add_reason(
            milk.id, NeedReasonKind.LOW_STOCK, source_key=INVENTORY_SOURCE, actor=ACTOR
        )

        needs.withdraw_reason(
            milk.id, NeedReasonKind.LOW_STOCK, source_key=INVENTORY_SOURCE, actor=ACTOR
        )

        after = needs.get(need.id)
        assert after is not None
        assert after.status is NeedStatus.CANCELLED
        assert [r.active for r in after.reasons] == [False]
        assert after.active_reasons == []

    def test_other_reasons_keep_a_suggested_need_alive(
        self, needs: ReplenishmentNeeds, milk: HouseholdItem
    ) -> None:
        """A recipe still wants it."""
        need = needs.add_reason(
            milk.id, NeedReasonKind.LOW_STOCK, source_key=INVENTORY_SOURCE, actor=ACTOR
        )
        needs.add_reason(
            milk.id, NeedReasonKind.RECIPE_DEMAND, source_key="recipe:12", actor=ACTOR
        )

        needs.withdraw_reason(
            milk.id, NeedReasonKind.LOW_STOCK, source_key=INVENTORY_SOURCE, actor=ACTOR
        )

        after = needs.get(need.id)
        assert after is not None
        assert after.status is NeedStatus.SUGGESTED
        assert _kinds(list(after.active_reasons)) == ["recipe_demand"]

    @pytest.mark.parametrize(
        "decided",
        [[NeedStatus.APPROVED], [NeedStatus.APPROVED, NeedStatus.PLANNED]],
        ids=["approved", "planned"],
    )
    def test_a_need_a_person_decided_on_is_never_auto_cancelled(
        self,
        needs: ReplenishmentNeeds,
        milk: HouseholdItem,
        decided: list[NeedStatus],
    ) -> None:
        """The reason goes; the person's decision stays."""
        need = needs.add_reason(
            milk.id, NeedReasonKind.LOW_STOCK, source_key=INVENTORY_SOURCE, actor=ACTOR
        )
        for step in decided:
            needs.transition(need.id, step, actor=ACTOR)

        needs.withdraw_reason(
            milk.id, NeedReasonKind.LOW_STOCK, source_key=INVENTORY_SOURCE, actor=ACTOR
        )

        after = needs.get(need.id)
        assert after is not None
        assert after.status is decided[-1]

    def test_withdrawing_a_reason_that_is_not_there_does_nothing(
        self, needs: ReplenishmentNeeds, milk: HouseholdItem
    ) -> None:
        """No need, or no such reason: nothing happens."""
        needs.withdraw_reason(
            milk.id, NeedReasonKind.OUT, source_key=INVENTORY_SOURCE, actor=ACTOR
        )
        need = needs.add_reason(
            milk.id, NeedReasonKind.LOW_STOCK, source_key=INVENTORY_SOURCE, actor=ACTOR
        )

        needs.withdraw_reason(
            milk.id, NeedReasonKind.OUT, source_key=INVENTORY_SOURCE, actor=ACTOR
        )

        after = needs.get(need.id)
        assert after is not None
        assert after.status is NeedStatus.SUGGESTED

    def test_a_withdrawn_reason_can_come_back(
        self, needs: ReplenishmentNeeds, milk: HouseholdItem
    ) -> None:
        """Re-adding a withdrawn reason reactivates the same row."""
        needs.add_reason(
            milk.id, NeedReasonKind.RECIPE_DEMAND, source_key="recipe:12", actor=ACTOR
        )
        needs.add_reason(
            milk.id, NeedReasonKind.LOW_STOCK, source_key=INVENTORY_SOURCE, actor=ACTOR
        )
        needs.withdraw_reason(
            milk.id, NeedReasonKind.LOW_STOCK, source_key=INVENTORY_SOURCE, actor=ACTOR
        )

        need = needs.add_reason(
            milk.id, NeedReasonKind.LOW_STOCK, source_key=INVENTORY_SOURCE, actor=ACTOR
        )

        assert len(need.reasons) == 2
        assert all(r.active for r in need.reasons)


class TestSyncFromInventory:
    """Inventory raises and withdraws suggestions; it never decides."""

    def test_low_item_gets_a_suggested_need(
        self, needs: ReplenishmentNeeds, ledger: InventoryLedger, milk: HouseholdItem
    ) -> None:
        """Low stock is a reason, attributed to the sync."""
        _observe(ledger, milk.id, StockState.LOW)

        need = needs.sync_item(milk.id)

        assert need is not None
        assert need.status is NeedStatus.SUGGESTED
        assert _kinds(list(need.active_reasons)) == ["low_stock"]
        assert need.reasons[0].source_key == INVENTORY_SOURCE
        assert needs.history(need.id)[0].actor == SYNC_ACTOR
        observation = ledger.history(milk.id)[-1]
        assert need.reasons[0].detail == {"observation_id": observation.id}

    def test_low_then_out_swaps_the_reason_on_one_need(
        self, needs: ReplenishmentNeeds, ledger: InventoryLedger, milk: HouseholdItem
    ) -> None:
        """The need survives; its reason follows the state."""
        _observe(ledger, milk.id, StockState.LOW)
        first = needs.sync_item(milk.id)
        _observe(ledger, milk.id, StockState.OUT)

        second = needs.sync_item(milk.id)

        assert first is not None
        assert second is not None
        assert second.id == first.id
        assert _kinds(list(second.active_reasons)) == ["out"]

    def test_back_on_hand_cancels_an_undecided_suggestion(
        self, needs: ReplenishmentNeeds, ledger: InventoryLedger, milk: HouseholdItem
    ) -> None:
        """Nobody acted, and the problem went away."""
        _observe(ledger, milk.id, StockState.OUT)
        need = needs.sync_item(milk.id)
        _observe(ledger, milk.id, StockState.ON_HAND)

        needs.sync_item(milk.id)

        assert need is not None
        after = needs.get(need.id)
        assert after is not None
        assert after.status is NeedStatus.CANCELLED

    def test_on_hand_never_marks_an_ordered_need_received(
        self, needs: ReplenishmentNeeds, ledger: InventoryLedger, milk: HouseholdItem
    ) -> None:
        """Received is a statement, not an inference."""
        _observe(ledger, milk.id, StockState.OUT)
        need = needs.sync_item(milk.id)
        assert need is not None
        for step in (NeedStatus.APPROVED, NeedStatus.PLANNED, NeedStatus.ORDERED):
            needs.transition(need.id, step, actor=ACTOR)
        _observe(ledger, milk.id, StockState.ON_HAND)

        needs.sync_item(milk.id)

        after = needs.get(need.id)
        assert after is not None
        assert after.status is NeedStatus.ORDERED

    def test_unknown_state_changes_nothing(
        self,
        needs: ReplenishmentNeeds,
        ledger: InventoryLedger,
        milk: HouseholdItem,
        clock: FakeClock,
    ) -> None:
        """Stale evidence neither raises nor clears a need."""
        _observe(ledger, milk.id, StockState.LOW)
        need = needs.sync_item(milk.id)
        clock.advance(days=30)

        needs.sync_item(milk.id)

        assert need is not None
        after = needs.get(need.id)
        assert after is not None
        assert after.status is NeedStatus.SUGGESTED
        assert _kinds(list(after.active_reasons)) == ["low_stock"]

    def test_item_never_observed_gets_no_need(
        self, needs: ReplenishmentNeeds, milk: HouseholdItem
    ) -> None:
        """No evidence, no suggestion."""
        assert needs.sync_item(milk.id) is None
        assert needs.list_needs() == []

    def test_sync_is_idempotent(
        self, needs: ReplenishmentNeeds, ledger: InventoryLedger, milk: HouseholdItem
    ) -> None:
        """Running it again adds nothing."""
        _observe(ledger, milk.id, StockState.LOW)
        first = needs.sync_item(milk.id)

        second = needs.sync_item(milk.id)

        assert first is not None
        assert second is not None
        assert second.id == first.id
        assert len(second.reasons) == 1
        assert len(needs.history(second.id)) == 1

    def test_dismissal_holds_until_a_new_observation(
        self,
        needs: ReplenishmentNeeds,
        ledger: InventoryLedger,
        milk: HouseholdItem,
        clock: FakeClock,
    ) -> None:
        """The same evidence doesn't nag; new evidence may."""
        _observe(ledger, milk.id, StockState.LOW)
        need = needs.sync_item(milk.id)
        assert need is not None
        clock.advance(minutes=5)
        needs.transition(need.id, NeedStatus.DISMISSED, actor=ACTOR)

        assert needs.sync_item(milk.id) is None

        clock.advance(days=1)
        _observe(ledger, milk.id, StockState.OUT)
        again = needs.sync_item(milk.id)

        assert again is not None
        assert again.id != need.id
        assert again.status is NeedStatus.SUGGESTED

    def test_snooze_ends_on_its_date(
        self,
        needs: ReplenishmentNeeds,
        ledger: InventoryLedger,
        milk: HouseholdItem,
        clock: FakeClock,
    ) -> None:
        """A deferred need returns to suggested once its date passes."""
        _observe(ledger, milk.id, StockState.LOW)
        need = needs.sync_item(milk.id)
        assert need is not None
        needs.transition(
            need.id,
            NeedStatus.DEFERRED,
            actor=ACTOR,
            deferred_until=NOW + dt.timedelta(days=2),
        )

        clock.advance(days=1)
        still = needs.sync_item(milk.id)
        clock.advance(days=1, seconds=1)
        woken = needs.sync_item(milk.id)

        assert still is not None
        assert still.status is NeedStatus.DEFERRED
        assert woken is not None
        assert woken.status is NeedStatus.SUGGESTED
        assert woken.deferred_until is None
        assert needs.history(need.id)[-1].actor == SYNC_ACTOR

    def test_snooze_without_a_date_waits_for_a_person(
        self,
        needs: ReplenishmentNeeds,
        ledger: InventoryLedger,
        milk: HouseholdItem,
        clock: FakeClock,
    ) -> None:
        """An open-ended deferral never wakes itself."""
        _observe(ledger, milk.id, StockState.LOW)
        need = needs.sync_item(milk.id)
        assert need is not None
        needs.transition(need.id, NeedStatus.DEFERRED, actor=ACTOR)
        clock.advance(days=5)

        after = needs.sync_item(milk.id)

        assert after is not None
        assert after.status is NeedStatus.DEFERRED

    def test_sync_all_covers_every_active_item(
        self, needs: ReplenishmentNeeds, ledger: InventoryLedger, db_path: str
    ) -> None:
        """One pass syncs the whole household."""
        items = HouseholdItemStore(db_path)
        eggs = items.resolve_or_create("eggs", actor=ACTOR)
        rice = items.resolve_or_create("rice", actor=ACTOR)
        _observe(ledger, eggs.id, StockState.OUT)
        _observe(ledger, rice.id, StockState.ON_HAND)

        needs.sync_all()

        assert [need.item_id for need in needs.list_needs()] == [eggs.id]


class TestListing:
    """Needs can be listed by status."""

    def test_list_filters_by_status_and_orders_by_creation(
        self, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """Open needs by default; any set of statuses on request."""
        items = HouseholdItemStore(db_path)
        a = items.resolve_or_create("a", actor=ACTOR)
        b = items.resolve_or_create("b", actor=ACTOR)
        c = items.resolve_or_create("c", actor=ACTOR)
        first = needs.add_reason(
            a.id, NeedReasonKind.OUT, source_key=INVENTORY_SOURCE, actor=ACTOR
        )
        second = needs.add_reason(
            b.id, NeedReasonKind.MANUAL_REQUEST, source_key="manual:x", actor=ACTOR
        )
        third = needs.add_reason(
            c.id, NeedReasonKind.OUT, source_key=INVENTORY_SOURCE, actor=ACTOR
        )
        needs.transition(third.id, NeedStatus.DISMISSED, actor=ACTOR)

        assert [n.id for n in needs.list_needs()] == [first.id, second.id]
        assert [n.id for n in needs.list_needs({NeedStatus.APPROVED})] == [second.id]
        assert [n.id for n in needs.list_needs({NeedStatus.DISMISSED})] == [third.id]

    def test_open_need_for_item_follows_merges(
        self, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """Asking by a merged id finds the survivor's need."""
        items = HouseholdItemStore(db_path)
        tomato = items.resolve_or_create("tomato", actor=ACTOR)
        tomatoes = items.resolve_or_create("tomatoes", actor=ACTOR)
        need = needs.add_reason(
            tomato.id, NeedReasonKind.OUT, source_key=INVENTORY_SOURCE, actor=ACTOR
        )
        items.merge(tomatoes.id, tomato.id, actor=ACTOR)

        found = needs.open_need_for_item(tomatoes.id)

        assert found is not None
        assert found.id == need.id
        assert needs.open_need_for_item(99999) is None

    def test_get_missing_need_returns_none(self, needs: ReplenishmentNeeds) -> None:
        """A missing need is not an error for get."""
        assert needs.get(99999) is None


class TestFoldMergedNeeds:
    """Two open needs left by a merge become one."""

    def test_reasons_move_to_the_older_need_and_the_other_is_cancelled(
        self, needs: ReplenishmentNeeds, db_path: str, clock: FakeClock
    ) -> None:
        """After the fold the survivor has one need with every reason."""
        items = HouseholdItemStore(db_path)
        tomato = items.resolve_or_create("tomato", actor=ACTOR)
        tomatoes = items.resolve_or_create("tomatoes", actor=ACTOR)
        older = needs.add_reason(
            tomato.id,
            NeedReasonKind.LOW_STOCK,
            source_key=INVENTORY_SOURCE,
            actor=ACTOR,
        )
        clock.advance(minutes=1)
        newer = needs.add_reason(
            tomatoes.id,
            NeedReasonKind.RECIPE_DEMAND,
            source_key="recipe:3",
            actor=ACTOR,
        )
        items.merge(tomatoes.id, tomato.id, actor=ACTOR)

        kept = needs.fold_merged_needs(tomato.id, actor="jess")

        assert kept is not None
        assert kept.id == older.id
        assert _kinds(list(kept.active_reasons)) == ["low_stock", "recipe_demand"]
        folded = needs.get(newer.id)
        assert folded is not None
        assert folded.status is NeedStatus.CANCELLED
        last = needs.history(newer.id)[-1]
        assert last.actor == "jess"
        assert str(older.id) in last.note
        assert [n.id for n in needs.list_needs()] == [older.id]

    def test_nothing_to_fold_returns_the_single_need(
        self, needs: ReplenishmentNeeds, milk: HouseholdItem
    ) -> None:
        """An item with one open need is left alone."""
        need = needs.add_reason(
            milk.id, NeedReasonKind.OUT, source_key=INVENTORY_SOURCE, actor=ACTOR
        )

        kept = needs.fold_merged_needs(milk.id, actor=ACTOR)

        assert kept is not None
        assert kept.id == need.id
        assert len(needs.history(need.id)) == 1

    def test_item_without_needs_returns_none(
        self, needs: ReplenishmentNeeds, milk: HouseholdItem
    ) -> None:
        """Nothing open, nothing to fold."""
        assert needs.fold_merged_needs(milk.id, actor=ACTOR) is None


class TestPantryManagerSyncsNeeds:
    """A status change through the old path keeps needs in step."""

    def test_marking_low_suggests_and_restocking_clears(self, db_path: str) -> None:
        """The need appears with the low mark and goes when it is restocked."""
        manager = PantryManager(db_path)
        manager.add_item(InventoryItem(ingredient="eggs", display_name="Eggs"))
        needs = ReplenishmentNeeds(db_path)
        item = HouseholdItemStore(db_path).resolve("eggs")
        assert item is not None

        manager.update_status("eggs", InventoryStatus.LOW)
        suggested = needs.open_need_for_item(item.id)
        manager.mark_restocked(["eggs"])

        assert suggested is not None
        assert suggested.status is NeedStatus.SUGGESTED
        assert needs.open_need_for_item(item.id) is None
        after = needs.get(suggested.id)
        assert after is not None
        assert after.status is NeedStatus.CANCELLED


class TestOneOpenNeedPerItem:
    """The database itself refuses a second open need."""

    def test_second_open_row_for_an_item_violates_the_index(
        self, needs: ReplenishmentNeeds, milk: HouseholdItem, db_path: str
    ) -> None:
        """The partial unique index backs the service's rule."""
        from grocery_butler.db.adapter import IntegrityError

        needs.add_reason(
            milk.id, NeedReasonKind.OUT, source_key=INVENTORY_SOURCE, actor=ACTOR
        )

        with closing(get_connection(db_path)) as conn, pytest.raises(IntegrityError):
            conn.execute(
                "INSERT INTO replenishment_needs"
                " (item_id, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (milk.id, "approved", NOW.isoformat(), NOW.isoformat()),
            )


_PG_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.mark.skipif(not _PG_URL, reason="TEST_DATABASE_URL not set")
class TestPostgres:
    """Needs behave the same on Postgres."""

    @pytest.fixture()
    def pg_item(self) -> Iterator[HouseholdItem]:
        """Yield a fresh item on the Postgres test database, then remove it."""
        init_db(_PG_URL)
        self._clean()
        yield HouseholdItemStore(_PG_URL).resolve_or_create("pgn milk", actor=ACTOR)
        self._clean()

    def _clean(self) -> None:
        """Delete this suite's item with its needs and ledger rows."""
        with closing(get_connection(_PG_URL)) as conn:
            ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM household_items WHERE name = ?", ("pgn milk",)
                ).fetchall()
            ]
            for item_id in ids:
                for statement in (
                    "DELETE FROM replenishment_need_reasons WHERE need_id IN"
                    " (SELECT id FROM replenishment_needs WHERE item_id = ?)",
                    "DELETE FROM replenishment_need_events WHERE need_id IN"
                    " (SELECT id FROM replenishment_needs WHERE item_id = ?)",
                    "DELETE FROM replenishment_needs WHERE item_id = ?",
                    "DELETE FROM inventory_observations WHERE item_id = ?",
                    "DELETE FROM household_item_aliases WHERE item_id = ?",
                    "DELETE FROM household_item_events WHERE item_id = ?",
                    "DELETE FROM household_items WHERE id = ?",
                ):
                    conn.execute(statement, (item_id,))
            conn.commit()

    def test_lifecycle_and_the_one_open_need_rule(self, pg_item: HouseholdItem) -> None:
        """Reasons, moves, sync and the partial index work on Postgres."""
        from grocery_butler.db.adapter import IntegrityError

        clock = FakeClock()
        needs = ReplenishmentNeeds(_PG_URL, clock=clock)
        ledger = InventoryLedger(_PG_URL, clock=clock)

        _observe(ledger, pg_item.id, StockState.LOW)
        need = needs.sync_item(pg_item.id)
        assert need is not None
        joined = needs.add_reason(
            pg_item.id, NeedReasonKind.RECIPE_DEMAND, source_key="recipe:1", actor=ACTOR
        )
        approved = needs.transition(need.id, NeedStatus.APPROVED, actor=ACTOR)

        assert joined.id == need.id
        assert approved.status is NeedStatus.APPROVED
        assert len(approved.active_reasons) == 2
        with closing(get_connection(_PG_URL)) as conn, pytest.raises(IntegrityError):
            conn.execute(
                "INSERT INTO replenishment_needs"
                " (item_id, status, created_at, updated_at) VALUES (?, ?, ?, ?)",
                (pg_item.id, "suggested", NOW.isoformat(), NOW.isoformat()),
            )
