"""Tests for check reminders (ADR 0019, #169)."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest

from grocery_butler.check_reminders import (
    DAILY_LIMIT_PREFERENCE,
    DEFAULT_DAILY_LIMIT,
    MAX_SNOOZE_DAYS,
    REISSUE_AFTER,
    CheckReason,
    CheckReminders,
    ReminderAction,
    SnoozeInvalidError,
)
from grocery_butler.db import get_connection, init_db
from grocery_butler.household_items import HouseholdItemStore, ItemNotFoundError
from grocery_butler.inventory_ledger import InventoryLedger
from grocery_butler.models import (
    HouseholdItemUpdate,
    NewObservation,
    ObservationSource,
    StaplePolicy,
    StockState,
)
from grocery_butler.observability import AUDIT_LOGGER

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

NOW = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.UTC)
ACTOR = "geoff@test"
CHANNEL = "openclaw"


class FakeClock:
    """A clock tests move by hand."""

    def __init__(self) -> None:
        """Start at NOW."""
        self.now = NOW

    def __call__(self) -> dt.datetime:
        """Return the current fake time."""
        return self.now


class Home:
    """A household with no staples, so each test adds exactly what it needs."""

    def __init__(self, db_path: str) -> None:
        """Clear the seeded staples' policies and bind the services."""
        self.db_path = db_path
        self.clock = FakeClock()
        self.items = HouseholdItemStore(db_path)
        self.ledger = InventoryLedger(db_path, clock=self.clock)
        self.reminders = CheckReminders(db_path, clock=self.clock)
        with closing(get_connection(db_path)) as conn:
            conn.execute("UPDATE household_items SET staple_policy = NULL")
            conn.commit()

    def item(self, name: str, policy: StaplePolicy | None) -> int:
        """Create an item with a policy; return its id."""
        item = self.items.resolve_or_create(name, actor=ACTOR)
        self.items.update(
            item.id, HouseholdItemUpdate(staple_policy=policy), actor=ACTOR
        )
        return item.id

    def observe(
        self,
        item_id: int,
        *,
        days_ago: float = 0,
        confidence: float | None = None,
        source: ObservationSource = ObservationSource.HUMAN,
        hour: int | None = None,
    ) -> int:
        """Record an on-hand observation; return its id."""
        when = self.clock.now - dt.timedelta(days=days_ago)
        if hour is not None:
            when = when.replace(hour=hour, minute=0)
        return self.ledger.record(
            NewObservation(
                item_id=item_id,
                state=StockState.ON_HAND,
                source=source,
                actor=ACTOR,
                confidence=confidence,
                observed_at=when,
            )
        ).id

    def names(self) -> list[str]:
        """Return the names of the current candidates, in rank order."""
        return [c.display_name for c in self.reminders.overview().candidates]

    def set_limit(self, value: str) -> None:
        """Set the daily limit preference."""
        with closing(get_connection(self.db_path)) as conn:
            conn.execute(
                "DELETE FROM preferences WHERE key = ?", (DAILY_LIMIT_PREFERENCE,)
            )
            conn.execute(
                "INSERT INTO preferences (key, value) VALUES (?, ?) RETURNING key",
                (DAILY_LIMIT_PREFERENCE, value),
            ).fetchall()
            conn.commit()


@pytest.fixture()
def home(tmp_path: Path) -> Home:
    """Return a household on a migrated temporary database."""
    path = str(tmp_path / "reminders.db")
    init_db(path)
    return Home(path)


class _ListHandler(logging.Handler):
    """Collect audit records as parsed JSON."""

    def __init__(self) -> None:
        """Start with no records."""
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Parse and keep one audit line."""
        self.events.append(json.loads(record.getMessage()))


@pytest.fixture()
def events() -> Iterator[list[dict[str, Any]]]:
    """Capture every audit event emitted during the test."""
    handler = _ListHandler()
    logger = logging.getLogger(AUDIT_LOGGER)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield handler.events
    logger.removeHandler(handler)
    logger.setLevel(previous)


class TestCandidates:
    """Only a kept item we no longer know about is worth asking about."""

    def test_the_example_from_the_issue(self, home: Home) -> None:
        """Milk seen 5 days ago at 0.6 confidence, always keep: one prompt."""
        milk = home.item("milk", StaplePolicy.ALWAYS_KEEP)
        observation = home.observe(
            milk, days_ago=5, confidence=0.6, source=ObservationSource.PHOTO
        )

        candidates = home.reminders.overview().candidates

        assert len(candidates) == 1
        candidate = candidates[0]
        assert candidate.item_id == milk
        assert candidate.display_name == "Milk"
        assert candidate.policy is StaplePolicy.ALWAYS_KEEP
        assert candidate.priority == 2
        assert candidate.reason is CheckReason.UNCERTAIN
        assert candidate.confidence == 0.6
        assert candidate.age_days == pytest.approx(5.0)
        assert candidate.last_observation_id == observation

    def test_a_fresh_confident_observation_is_not_a_candidate(self, home: Home) -> None:
        """We know enough; nobody is asked."""
        milk = home.item("milk", StaplePolicy.ALWAYS_KEEP)
        home.observe(milk)

        assert home.names() == []

    def test_a_stale_observation_is_a_candidate(self, home: Home) -> None:
        """Past its freshness window the item is worth a look."""
        rice = home.item("rice", StaplePolicy.REPLENISH_WHEN_LOW)
        home.observe(rice, days_ago=400)

        candidate = home.reminders.overview().candidates[0]

        assert candidate.reason is CheckReason.STALE
        assert candidate.priority == 1
        assert candidate.age_days is not None
        assert candidate.age_days > candidate.fresh_for_days

    def test_a_kept_item_never_checked_is_a_candidate(self, home: Home) -> None:
        """A staple nobody has ever looked at is worth asking about once."""
        home.item("coffee", StaplePolicy.ALWAYS_KEEP)

        candidate = home.reminders.overview().candidates[0]

        assert candidate.reason is CheckReason.NEVER_CHECKED
        assert (candidate.age_days, candidate.confidence) == (None, None)
        assert candidate.last_observation_id is None

    @pytest.mark.parametrize(
        "policy", [None, StaplePolicy.MEALS_ONLY, StaplePolicy.NEVER_SUGGEST]
    )
    def test_items_not_kept_in_stock_are_never_asked_about(
        self, home: Home, policy: StaplePolicy | None
    ) -> None:
        """Paprika bought only for meals is not worth a reminder."""
        paprika = home.item("paprika", policy)
        home.observe(paprika, days_ago=400)

        assert home.names() == []

    @pytest.mark.parametrize(
        ("confidence", "asked"), [(0.79, True), (0.8, False), (1.0, False)]
    )
    def test_the_uncertainty_boundary(
        self, home: Home, confidence: float, asked: bool
    ) -> None:
        """Below 0.8 is uncertain; 0.8 and above is not."""
        milk = home.item("milk", StaplePolicy.ALWAYS_KEEP)
        home.observe(milk, confidence=confidence, source=ObservationSource.PHOTO)

        assert (home.names() == ["Milk"]) is asked

    def test_ranking_puts_always_keep_first_then_the_longest_unchecked(
        self, home: Home
    ) -> None:
        """Priority, then age, then name."""
        home.set_limit("10")
        home.observe(home.item("rice", StaplePolicy.REPLENISH_WHEN_LOW), days_ago=900)
        home.observe(home.item("milk", StaplePolicy.ALWAYS_KEEP), days_ago=400)
        home.observe(home.item("coffee", StaplePolicy.ALWAYS_KEEP), days_ago=500)
        home.item("tea", StaplePolicy.ALWAYS_KEEP)
        home.item("oats", StaplePolicy.ALWAYS_KEEP)

        assert home.names() == ["Oats", "Tea", "Coffee", "Milk", "Rice"]

    def test_a_merged_item_is_asked_about_once_under_its_surviving_name(
        self, home: Home
    ) -> None:
        """The duplicate doesn't get its own reminder."""
        keep = home.item("oat milk", StaplePolicy.ALWAYS_KEEP)
        duplicate = home.item("oatmilk", StaplePolicy.ALWAYS_KEEP)
        home.items.merge(duplicate, keep, actor=ACTOR)

        assert home.names() == ["Oat Milk"]


class TestRateLimit:
    """No more reminders per day than the household's limit."""

    def _many(self, home: Home, count: int) -> None:
        """Create several always-keep items nobody has checked."""
        for index in range(count):
            home.item(f"staple {index:02d}", StaplePolicy.ALWAYS_KEEP)

    def test_the_default_limit(self, home: Home) -> None:
        """With nothing configured the limit is the default."""
        self._many(home, 6)

        overview = home.reminders.overview()

        assert overview.daily_limit == DEFAULT_DAILY_LIMIT == 3
        assert len(overview.candidates) == 3
        assert (overview.issued_today, overview.remaining_today) == (0, 3)

    def test_issue_never_exceeds_the_limit_in_a_day(self, home: Home) -> None:
        """However often and however much is asked, three go out."""
        self._many(home, 10)

        first = home.reminders.issue(actor=CHANNEL, max_count=2)
        second = home.reminders.issue(actor=CHANNEL, max_count=50)
        third = home.reminders.issue(actor=CHANNEL)
        overview = home.reminders.overview()

        assert (len(first), len(second), len(third)) == (2, 1, 0)
        assert (overview.issued_today, overview.remaining_today) == (3, 0)
        assert overview.candidates == []

    def test_the_allowance_returns_the_next_day(self, home: Home) -> None:
        """A new UTC day, a new allowance, and different items."""
        self._many(home, 10)
        yesterday = {c.item_id for c in home.reminders.issue(actor=CHANNEL)}

        home.clock.now = NOW + dt.timedelta(days=1)
        today = home.reminders.issue(actor=CHANNEL)

        assert len(today) == 3
        assert yesterday.isdisjoint({c.item_id for c in today})

    def test_the_day_is_the_utc_day(self, home: Home) -> None:
        """Just before midnight counts for today; just after, for tomorrow."""
        self._many(home, 10)
        home.clock.now = NOW.replace(hour=23, minute=59)
        home.reminders.issue(actor=CHANNEL)

        home.clock.now = NOW.replace(hour=23, minute=59, second=30)
        late = home.reminders.overview().remaining_today
        home.clock.now = (NOW + dt.timedelta(days=1)).replace(hour=0, minute=1)

        assert late == 0
        assert home.reminders.overview().remaining_today == 3

    def test_a_configured_limit_is_honoured(self, home: Home) -> None:
        """The household can ask for fewer, or more."""
        self._many(home, 10)
        home.set_limit("1")

        assert len(home.reminders.issue(actor=CHANNEL)) == 1
        assert home.reminders.issue(actor=CHANNEL) == []

    def test_a_limit_of_zero_turns_reminders_off(self, home: Home) -> None:
        """Zero means never."""
        self._many(home, 3)
        home.set_limit("0")

        assert home.reminders.overview().candidates == []
        assert home.reminders.issue(actor=CHANNEL) == []

    @pytest.mark.parametrize("value", ["", "lots", "-1", "3.5", "9999"])
    def test_a_bad_limit_falls_back_to_the_default(
        self, home: Home, value: str
    ) -> None:
        """A broken setting never removes the limit."""
        home.set_limit(value)

        assert home.reminders.daily_limit() == DEFAULT_DAILY_LIMIT

    def test_max_count_can_not_be_negative(self, home: Home) -> None:
        """A negative ask is a programming error."""
        with pytest.raises(ValueError, match="can't be negative"):
            home.reminders.issue(actor=CHANNEL, max_count=-1)

    def test_an_issued_item_is_not_issued_again_for_a_few_days(
        self, home: Home
    ) -> None:
        """One unanswered question isn't repeated every morning."""
        coffee = home.item("coffee", StaplePolicy.ALWAYS_KEEP)
        home.reminders.issue(actor=CHANNEL)

        home.clock.now = NOW + REISSUE_AFTER - dt.timedelta(hours=1)
        soon = home.names()
        home.clock.now = NOW + REISSUE_AFTER + dt.timedelta(hours=1)

        assert soon == []
        assert [c.item_id for c in home.reminders.overview().candidates] == [coffee]

    def test_issuing_is_recorded_with_who_took_it(self, home: Home) -> None:
        """The history shows each reminder, its reason and the channel."""
        coffee = home.item("coffee", StaplePolicy.ALWAYS_KEEP)

        home.reminders.issue(actor=CHANNEL)

        history = home.reminders.history(coffee)
        assert [(e.action, e.actor, e.detail) for e in history] == [
            (ReminderAction.ISSUED, CHANNEL, {"reason": "never_checked"})
        ]
        assert history[0].created_at == NOW


class TestMemberChoices:
    """Snooze, dismiss and stop asking, each recorded with its actor."""

    def test_stop_asking_holds_until_turned_back_on(self, home: Home) -> None:
        """Time and new evidence don't bring it back; resume does."""
        coffee = home.item("coffee", StaplePolicy.ALWAYS_KEEP)

        stopped = home.reminders.stop(coffee, actor=ACTOR)
        home.observe(coffee, days_ago=0)
        home.clock.now = NOW + dt.timedelta(days=800)
        long_after = home.names()
        resumed = home.reminders.resume(coffee, actor=ACTOR)

        assert stopped.stop_asking is True
        assert (stopped.updated_by, stopped.updated_at) == (ACTOR, NOW)
        assert long_after == []
        assert resumed.stop_asking is False
        assert home.names() == ["Coffee"]

    def test_snooze_holds_for_its_days_and_then_ends(self, home: Home) -> None:
        """A snoozed item comes back when the snooze runs out."""
        coffee = home.item("coffee", StaplePolicy.ALWAYS_KEEP)

        state = home.reminders.snooze(coffee, days=7, actor=ACTOR)
        home.clock.now = NOW + dt.timedelta(days=6, hours=23)
        during = home.names()
        home.clock.now = NOW + dt.timedelta(days=7, minutes=1)

        assert state.snoozed_until == NOW + dt.timedelta(days=7)
        assert during == []
        assert home.names() == ["Coffee"]

    @pytest.mark.parametrize("days", [0, -3, MAX_SNOOZE_DAYS + 1])
    def test_an_impossible_snooze_is_refused(self, home: Home, days: int) -> None:
        """A snooze is 1 to 90 days."""
        coffee = home.item("coffee", StaplePolicy.ALWAYS_KEEP)

        with pytest.raises(SnoozeInvalidError, match="1 to 90 days"):
            home.reminders.snooze(coffee, days=days, actor=ACTOR)

        assert home.reminders.history(coffee) == []

    def test_dismiss_holds_until_new_evidence_arrives(self, home: Home) -> None:
        """A dismissed item doesn't come back with time, only with evidence."""
        milk = home.item("milk", StaplePolicy.ALWAYS_KEEP)
        seen = home.observe(
            milk, days_ago=5, confidence=0.6, source=ObservationSource.PHOTO
        )

        state = home.reminders.dismiss(milk, actor=ACTOR)
        home.clock.now = NOW + dt.timedelta(days=30)
        later = home.names()
        home.observe(milk, confidence=0.5, source=ObservationSource.PHOTO)

        assert (state.dismissed, state.dismissed_observation_id) == (True, seen)
        assert later == []
        assert home.names() == ["Milk"]

    def test_dismissing_a_never_checked_item_holds_until_it_is_checked(
        self, home: Home
    ) -> None:
        """With no evidence yet, the first observation is the new evidence."""
        coffee = home.item("coffee", StaplePolicy.ALWAYS_KEEP)

        state = home.reminders.dismiss(coffee, actor=ACTOR)
        dismissed = home.names()
        home.observe(coffee, confidence=0.5, source=ObservationSource.PHOTO)

        assert state.dismissed_observation_id is None
        assert dismissed == []
        assert home.names() == ["Coffee"]

    def test_every_choice_is_recorded_with_its_actor(self, home: Home) -> None:
        """The history names who snoozed, dismissed, stopped and resumed."""
        coffee = home.item("coffee", StaplePolicy.ALWAYS_KEEP)

        home.reminders.snooze(coffee, days=2, actor="geoff@test")
        home.reminders.dismiss(coffee, actor="jess@test")
        home.reminders.stop(coffee, actor="geoff@test")
        home.reminders.resume(coffee, actor="jess@test")

        assert [(e.action, e.actor) for e in home.reminders.history(coffee)] == [
            (ReminderAction.SNOOZED, "geoff@test"),
            (ReminderAction.DISMISSED, "jess@test"),
            (ReminderAction.STOPPED, "geoff@test"),
            (ReminderAction.RESUMED, "jess@test"),
        ]
        assert home.reminders.history(coffee)[0].detail == {"days": 2}

    def test_resume_clears_every_hold(self, home: Home) -> None:
        """Snoozed, dismissed and stopped all go at once."""
        coffee = home.item("coffee", StaplePolicy.ALWAYS_KEEP)
        home.reminders.snooze(coffee, days=30, actor=ACTOR)
        home.reminders.dismiss(coffee, actor=ACTOR)
        home.reminders.stop(coffee, actor=ACTOR)

        state = home.reminders.resume(coffee, actor=ACTOR)

        assert (
            state.stop_asking,
            state.snoozed_until,
            state.dismissed,
            state.dismissed_observation_id,
        ) == (False, None, False, None)
        assert home.names() == ["Coffee"]

    def test_an_item_nobody_touched_has_an_empty_state(self, home: Home) -> None:
        """No row means nothing is held back."""
        coffee = home.item("coffee", StaplePolicy.ALWAYS_KEEP)

        state = home.reminders.state(coffee)

        assert (state.item_id, state.stop_asking, state.updated_by) == (
            coffee,
            False,
            None,
        )

    def test_unknown_items_are_refused(self, home: Home) -> None:
        """A choice can't be made about an item that doesn't exist."""
        with pytest.raises(ItemNotFoundError):
            home.reminders.stop(99999, actor=ACTOR)
        with pytest.raises(ItemNotFoundError):
            home.reminders.state(99999)
        with pytest.raises(ItemNotFoundError):
            home.reminders.history(99999)


class TestSuggestedHour:
    """When the household usually looks is learned from when it reports."""

    def test_no_suggestion_until_there_is_enough_to_go_on(self, home: Home) -> None:
        """Four observations are not a habit."""
        milk = home.item("milk", StaplePolicy.ALWAYS_KEEP)
        for day in range(4):
            home.observe(milk, days_ago=day + 1, hour=18)

        assert home.reminders.suggested_hour() is None
        assert home.reminders.overview().suggested_hour_utc is None

    def test_the_most_common_hour_wins(self, home: Home) -> None:
        """Mostly evenings means suggest the evening."""
        milk = home.item("milk", StaplePolicy.ALWAYS_KEEP)
        for day in range(4):
            home.observe(milk, days_ago=day + 1, hour=18)
        home.observe(milk, days_ago=6, hour=8)
        home.observe(milk, days_ago=7, hour=8)

        assert home.reminders.suggested_hour() == 18

    def test_a_tie_goes_to_the_earlier_hour(self, home: Home) -> None:
        """The same answer every time, whatever order the rows come in."""
        milk = home.item("milk", StaplePolicy.ALWAYS_KEEP)
        for day in range(3):
            home.observe(milk, days_ago=day + 1, hour=18)
            home.observe(milk, days_ago=day + 10, hour=7)

        assert home.reminders.suggested_hour() == 7

    def test_only_what_people_reported_themselves_counts(self, home: Home) -> None:
        """A photo batch at 3am says nothing about when people look."""
        milk = home.item("milk", StaplePolicy.ALWAYS_KEEP)
        for day in range(8):
            home.observe(
                milk,
                days_ago=day + 1,
                hour=3,
                source=ObservationSource.PHOTO,
                confidence=0.9,
            )

        assert home.reminders.suggested_hour() is None

    def test_old_habits_are_forgotten(self, home: Home) -> None:
        """Observations older than 60 days don't count."""
        milk = home.item("milk", StaplePolicy.ALWAYS_KEEP)
        for day in range(8):
            home.observe(milk, days_ago=90 + day, hour=18)

        assert home.reminders.suggested_hour() is None


class TestAudit:
    """Reminders leave audit events with counts and ids, never names."""

    def test_issue_and_choices_are_audited(
        self, home: Home, events: list[dict[str, Any]]
    ) -> None:
        """The trail shows how many were issued and each choice made."""
        coffee = home.item("coffee", StaplePolicy.ALWAYS_KEEP)

        home.reminders.issue(actor=CHANNEL)
        home.reminders.stop(coffee, actor=ACTOR)

        issued = next(e for e in events if e["event"] == "reminder.issued")
        stopped = next(e for e in events if e["event"] == "reminder.stopped")
        assert (issued["count"], issued["actor"]) == (1, CHANNEL)
        assert (stopped["item_id"], stopped["actor"]) == (coffee, ACTOR)
        assert "Coffee" not in json.dumps(events)

    def test_issuing_nothing_emits_nothing(
        self, home: Home, events: list[dict[str, Any]]
    ) -> None:
        """An empty take is not an event."""
        home.reminders.issue(actor=CHANNEL)

        assert [e for e in events if e["event"] == "reminder.issued"] == []


_PG_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.mark.skipif(not _PG_URL, reason="TEST_DATABASE_URL not set")
class TestPostgres:
    """Reminder state and history behave the same on Postgres."""

    @pytest.fixture()
    def item_id(self) -> Iterator[int]:
        """Create a fresh always-keep item and clean its reminder rows after."""
        init_db(_PG_URL)
        items = HouseholdItemStore(_PG_URL)
        item = items.resolve_or_create(
            f"pg reminder item {os.urandom(4).hex()}", actor="pg-reminder-test"
        )
        items.update(
            item.id,
            HouseholdItemUpdate(staple_policy=StaplePolicy.ALWAYS_KEEP),
            actor="pg-reminder-test",
        )
        yield item.id
        with closing(get_connection(_PG_URL)) as conn:
            conn.execute(
                "DELETE FROM check_reminder_events WHERE item_id = ?", (item.id,)
            )
            conn.execute(
                "DELETE FROM check_reminder_state WHERE item_id = ?", (item.id,)
            )
            conn.commit()
        items.update(
            item.id,
            HouseholdItemUpdate(staple_policy=StaplePolicy.NEVER_SUGGEST),
            actor="pg-reminder-test",
        )

    def test_state_upsert_and_history(self, item_id: int) -> None:
        """Stop, resume and snooze round-trip through the upsert."""
        reminders = CheckReminders(_PG_URL)
        actor = "pg-reminder-test"

        stopped = reminders.stop(item_id, actor=actor)
        resumed = reminders.resume(item_id, actor=actor)
        snoozed = reminders.snooze(item_id, days=3, actor=actor)

        assert stopped.stop_asking is True
        assert resumed.stop_asking is False
        assert snoozed.snoozed_until is not None
        assert [e.action for e in reminders.history(item_id)] == [
            ReminderAction.STOPPED,
            ReminderAction.RESUMED,
            ReminderAction.SNOOZED,
        ]
        assert item_id not in {c.item_id for c in reminders.overview().candidates}
