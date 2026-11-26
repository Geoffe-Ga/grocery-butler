"""Check reminders: asking about an item only when it is worth asking (ADR 0019).

A reminder is a suggestion to OpenClaw that the household look at one item:
"is there still coffee?". It is worth asking only when the item matters and
we no longer know. So a candidate is an item the household keeps in stock
(its policy is ``always_keep`` or ``replenish_when_low``) whose state is
stale, uncertain, or was never checked.

Reminders must reduce mental load, not become an inbox. So:

- At most a few are issued per day, a limit the household sets.
- An item issued recently isn't issued again until there is new evidence or
  a few days have passed.
- A member can snooze an item, dismiss it until new evidence arrives, or
  say "stop asking about this", which holds until they turn it back on.

OpenClaw decides whether, when and how to say any of it. Nothing here sends
a notification.
"""

from __future__ import annotations

import datetime as dt
import json
from collections import Counter
from contextlib import closing
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from grocery_butler.db import get_connection
from grocery_butler.household_items import HouseholdItemStore
from grocery_butler.inventory_ledger import InventoryLedger, utc_now
from grocery_butler.models import ObservationSource, StaplePolicy
from grocery_butler.observability import audit

if TYPE_CHECKING:
    from collections.abc import Callable

    from grocery_butler.db.adapter import DatabaseConnection
    from grocery_butler.models import HouseholdItem, InventorySnapshot

DEFAULT_DAILY_LIMIT = 3
MAX_DAILY_LIMIT = 20
DAILY_LIMIT_PREFERENCE = "check_reminders_per_day"
UNCERTAIN_BELOW = 0.8
REISSUE_AFTER = dt.timedelta(days=3)
MAX_SNOOZE_DAYS = 90
PREFERRED_HOUR_WINDOW = dt.timedelta(days=60)
MIN_OBSERVATIONS_FOR_HOUR = 5

_PRIORITY = {StaplePolicy.ALWAYS_KEEP: 2, StaplePolicy.REPLENISH_WHEN_LOW: 1}

_SELECT_STATE = (
    "SELECT item_id, stop_asking, snoozed_until, dismissed,"
    " dismissed_observation_id, updated_by, updated_at"
    " FROM check_reminder_state"
)
_SELECT_STATE_FOR_ITEM = (
    "SELECT item_id, stop_asking, snoozed_until, dismissed,"
    " dismissed_observation_id, updated_by, updated_at"
    " FROM check_reminder_state WHERE item_id = ?"
)
_UPSERT_STATE = (
    "INSERT INTO check_reminder_state (item_id, stop_asking, snoozed_until,"
    " dismissed, dismissed_observation_id, updated_by, updated_at)"
    " VALUES (?, ?, ?, ?, ?, ?, ?)"
    " ON CONFLICT (item_id) DO UPDATE SET stop_asking = excluded.stop_asking,"
    " snoozed_until = excluded.snoozed_until, dismissed = excluded.dismissed,"
    " dismissed_observation_id = excluded.dismissed_observation_id,"
    " updated_by = excluded.updated_by, updated_at = excluded.updated_at"
    " RETURNING item_id"
)
_INSERT_EVENT = (
    "INSERT INTO check_reminder_events (item_id, action, actor, detail, created_at)"
    " VALUES (?, ?, ?, ?, ?)"
)
_ISSUED_SINCE = (
    "SELECT item_id, created_at FROM check_reminder_events"
    " WHERE action = ? AND created_at >= ? ORDER BY id"
)
_EVENTS_FOR_ITEM = (
    "SELECT id, item_id, action, actor, detail, created_at"
    " FROM check_reminder_events WHERE item_id = ? ORDER BY id"
)
_HUMAN_OBSERVATION_TIMES = (
    "SELECT observed_at FROM inventory_observations"
    " WHERE source = ? AND observed_at >= ?"
)
_PREFERENCE = "SELECT value FROM preferences WHERE key = ?"


class CheckReason(StrEnum):
    """Why an item is worth checking."""

    NEVER_CHECKED = "never_checked"
    STALE = "stale"
    UNCERTAIN = "uncertain"


class ReminderAction(StrEnum):
    """What can happen to an item's reminders, each recorded with its actor."""

    ISSUED = "issued"
    SNOOZED = "snoozed"
    DISMISSED = "dismissed"
    STOPPED = "stopped"
    RESUMED = "resumed"


class CheckCandidate(BaseModel):
    """One item worth asking the household about.

    ``priority`` is 2 for an always-keep item and 1 for replenish-when-low.
    ``age_days`` and ``confidence`` describe the last observation, when
    there is one.
    """

    item_id: int
    display_name: str
    policy: StaplePolicy
    priority: int
    reason: CheckReason
    age_days: float | None
    fresh_for_days: int
    confidence: float | None
    last_observation_id: int | None


class ReminderState(BaseModel):
    """What a member has asked for about one item's reminders."""

    item_id: int
    stop_asking: bool
    snoozed_until: dt.datetime | None
    dismissed: bool
    dismissed_observation_id: int | None
    updated_by: str | None
    updated_at: dt.datetime | None


class ReminderEvent(BaseModel):
    """One entry in an item's reminder history."""

    id: int
    item_id: int
    action: ReminderAction
    actor: str
    detail: dict[str, Any]
    created_at: dt.datetime


class ReminderOverview(BaseModel):
    """The candidates and how many may still be issued today."""

    candidates: list[CheckCandidate]
    daily_limit: int
    issued_today: int
    remaining_today: int
    suggested_hour_utc: int | None


class SnoozeInvalidError(ValueError):
    """Raised when a snooze is asked for an impossible length of time."""

    def __init__(self, days: int) -> None:
        """Say what a snooze may be.

        Args:
            days: The number of days that was asked for.
        """
        self.days = days
        super().__init__(
            f"A snooze lasts 1 to {MAX_SNOOZE_DAYS} days; {days} was asked for."
        )


def _when(value: str | None) -> dt.datetime | None:
    """Parse a stored timestamp.

    Args:
        value: ISO 8601 text, or None.

    Returns:
        The time, or None.
    """
    return dt.datetime.fromisoformat(value) if value else None


def _reason(snapshot: InventorySnapshot) -> CheckReason | None:
    """Say why an item is worth checking, if it is.

    Args:
        snapshot: The item's inventory snapshot.

    Returns:
        The reason, or None when we know enough.
    """
    observation = snapshot.observation
    if observation is None and snapshot.stated_state is None:
        return CheckReason.NEVER_CHECKED
    if snapshot.stale:
        return CheckReason.STALE
    confidence = observation.confidence if observation is not None else None
    if confidence is not None and confidence < UNCERTAIN_BELOW:
        return CheckReason.UNCERTAIN
    return None


def _empty_state(item_id: int) -> ReminderState:
    """Build the state of an item nobody has said anything about.

    Args:
        item_id: The item.

    Returns:
        A state with nothing set.
    """
    return ReminderState(
        item_id=item_id,
        stop_asking=False,
        snoozed_until=None,
        dismissed=False,
        dismissed_observation_id=None,
        updated_by=None,
        updated_at=None,
    )


def _row_to_state(row: Any) -> ReminderState:
    """Build a state from its row.

    Args:
        row: A ``check_reminder_state`` row.

    Returns:
        The state.
    """
    return ReminderState(
        item_id=row["item_id"],
        stop_asking=bool(row["stop_asking"]),
        snoozed_until=_when(row["snoozed_until"]),
        dismissed=bool(row["dismissed"]),
        dismissed_observation_id=row["dismissed_observation_id"],
        updated_by=row["updated_by"],
        updated_at=_when(row["updated_at"]),
    )


class CheckReminders:
    """Chooses which items to ask about, and remembers what members said.

    Args:
        db_path: Database file path or PostgreSQL URL.
        clock: Returns the current UTC time; injectable for tests.
    """

    def __init__(
        self, db_path: str, clock: Callable[[], dt.datetime] = utc_now
    ) -> None:
        """Bind to a database.

        Args:
            db_path: Database file path or PostgreSQL URL.
            clock: Returns the current UTC time.
        """
        self._db_path = db_path
        self._clock = clock
        self._items = HouseholdItemStore(db_path)
        self._ledger = InventoryLedger(db_path, clock=clock)

    # ------------------------------------------------------------------
    # Settings and state
    # ------------------------------------------------------------------

    def daily_limit(self) -> int:
        """Return how many reminders may be issued per day.

        Read from the ``check_reminders_per_day`` preference. A missing,
        unreadable or out-of-range value falls back to the default rather
        than removing the limit.

        Returns:
            The limit, 0 to ``MAX_DAILY_LIMIT``. Zero turns reminders off.
        """
        with closing(get_connection(self._db_path)) as conn:
            row = conn.execute(_PREFERENCE, (DAILY_LIMIT_PREFERENCE,)).fetchone()
        raw = str(row["value"]).strip() if row is not None else ""
        if not raw.isdigit():
            return DEFAULT_DAILY_LIMIT
        return int(raw) if int(raw) <= MAX_DAILY_LIMIT else DEFAULT_DAILY_LIMIT

    def state(self, item_id: int) -> ReminderState:
        """Return what members have asked for about an item's reminders.

        Args:
            item_id: Any item id; a merged id is followed to its target.

        Returns:
            The item's reminder state.

        Raises:
            ItemNotFoundError: If no item has this id.
        """
        canonical = self._items.canonical(item_id).id
        with closing(get_connection(self._db_path)) as conn:
            row = conn.execute(_SELECT_STATE_FOR_ITEM, (canonical,)).fetchone()
        return _row_to_state(row) if row is not None else _empty_state(canonical)

    def history(self, item_id: int) -> list[ReminderEvent]:
        """Return an item's reminder history, oldest first.

        Args:
            item_id: Any item id; a merged id is followed to its target.

        Returns:
            Every reminder issued and every choice made, with its actor.

        Raises:
            ItemNotFoundError: If no item has this id.
        """
        canonical = self._items.canonical(item_id).id
        with closing(get_connection(self._db_path)) as conn:
            rows = conn.execute(_EVENTS_FOR_ITEM, (canonical,)).fetchall()
        return [
            ReminderEvent(
                id=row["id"],
                item_id=row["item_id"],
                action=ReminderAction(row["action"]),
                actor=row["actor"],
                detail=json.loads(row["detail"]),
                created_at=dt.datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    def _states(self, conn: DatabaseConnection) -> dict[int, ReminderState]:
        """Read every stored reminder state.

        Args:
            conn: An open connection.

        Returns:
            States by item id.
        """
        rows = conn.execute(_SELECT_STATE).fetchall()
        return {row["item_id"]: _row_to_state(row) for row in rows}

    def _issued(
        self, conn: DatabaseConnection, since: dt.datetime
    ) -> list[tuple[int, dt.datetime]]:
        """Read the reminders issued since a time.

        Args:
            conn: An open connection.
            since: The earliest issue time to include.

        Returns:
            ``(item id, when)`` pairs, oldest first.
        """
        rows = conn.execute(
            _ISSUED_SINCE, (ReminderAction.ISSUED.value, since.isoformat())
        ).fetchall()
        return [
            (row["item_id"], dt.datetime.fromisoformat(row["created_at"]))
            for row in rows
        ]

    # ------------------------------------------------------------------
    # Candidates
    # ------------------------------------------------------------------

    def _suppressed(
        self, state: ReminderState | None, candidate: CheckCandidate
    ) -> bool:
        """Say whether a member's choice holds a candidate back.

        Args:
            state: The item's stored state, if any.
            candidate: The candidate.

        Returns:
            True when the member said stop, the snooze hasn't run out, or
            the dismissal still holds because no new evidence has arrived.
        """
        if state is None:
            return False
        if state.stop_asking:
            return True
        if state.snoozed_until is not None and state.snoozed_until > self._clock():
            return True
        return (
            state.dismissed
            and state.dismissed_observation_id == candidate.last_observation_id
        )

    def _candidate(
        self, item: HouseholdItem, snapshot: InventorySnapshot
    ) -> CheckCandidate | None:
        """Build the candidate for an item, if it is worth checking.

        Args:
            item: A canonical item.
            snapshot: Its inventory snapshot.

        Returns:
            The candidate, or None for an item whose policy doesn't keep it
            in stock or whose state we know well enough.
        """
        priority = _PRIORITY.get(item.staple_policy) if item.staple_policy else None
        reason = _reason(snapshot)
        if priority is None or reason is None or item.staple_policy is None:
            return None
        observation = snapshot.observation
        return CheckCandidate(
            item_id=item.id,
            display_name=item.display_name,
            policy=item.staple_policy,
            priority=priority,
            reason=reason,
            age_days=snapshot.age_days,
            fresh_for_days=snapshot.fresh_for_days,
            confidence=observation.confidence if observation is not None else None,
            last_observation_id=observation.id if observation is not None else None,
        )

    def _ranked(self) -> list[CheckCandidate]:
        """List every item worth checking that no member's choice holds back.

        Returns:
            Candidates, most worth asking first: always-keep items before
            replenish-when-low, then the longest unchecked, then by name.
        """
        items = {item.id: item for item in self._items.list_items()}
        with closing(get_connection(self._db_path)) as conn:
            states = self._states(conn)
            recent = self._issued(conn, self._clock() - REISSUE_AFTER)
        recently_issued = {item_id for item_id, _ in recent}
        found = []
        for snapshot in self._ledger.snapshots():
            item = items.get(snapshot.item_id)
            candidate = self._candidate(item, snapshot) if item is not None else None
            if candidate is None or candidate.item_id in recently_issued:
                continue
            if not self._suppressed(states.get(candidate.item_id), candidate):
                found.append(candidate)
        return sorted(
            found,
            key=lambda c: (
                -c.priority,
                -(c.age_days if c.age_days is not None else float("inf")),
                c.display_name,
            ),
        )

    def _issued_today(self) -> int:
        """Count the reminders issued so far this UTC day.

        Returns:
            The count.
        """
        now = self._clock()
        midnight = now.replace(hour=0, minute=0, second=0, microsecond=0)
        with closing(get_connection(self._db_path)) as conn:
            return len(self._issued(conn, midnight))

    def suggested_hour(self) -> int | None:
        """Suggest the UTC hour the household usually looks at its kitchen.

        Learned from when members recorded observations themselves over the
        last 60 days: the most common hour, the earliest on a tie.

        Returns:
            An hour 0 to 23, or None until there are enough observations.
        """
        since = (self._clock() - PREFERRED_HOUR_WINDOW).isoformat()
        with closing(get_connection(self._db_path)) as conn:
            rows = conn.execute(
                _HUMAN_OBSERVATION_TIMES, (ObservationSource.HUMAN.value, since)
            ).fetchall()
        if len(rows) < MIN_OBSERVATIONS_FOR_HOUR:
            return None
        hours = Counter(
            dt.datetime.fromisoformat(str(row["observed_at"])).astimezone(dt.UTC).hour
            for row in rows
        )
        best = max(hours.values())
        return min(hour for hour, count in hours.items() if count == best)

    def overview(self) -> ReminderOverview:
        """List what could be asked now. Nothing is recorded.

        Returns:
            At most as many candidates as may still be issued today, with
            the limit, today's count and the suggested hour.
        """
        limit = self.daily_limit()
        issued = self._issued_today()
        remaining = max(0, limit - issued)
        return ReminderOverview(
            candidates=self._ranked()[:remaining],
            daily_limit=limit,
            issued_today=issued,
            remaining_today=remaining,
            suggested_hour_utc=self.suggested_hour(),
        )

    def issue(
        self, *, actor: str, max_count: int | None = None
    ) -> list[CheckCandidate]:
        """Take the next reminders to surface, and record them as issued.

        Never more than the day's remaining allowance, whatever is asked.

        Args:
            actor: Who is taking them, e.g. the OpenClaw channel.
            max_count: Take at most this many. None takes all that remain.

        Returns:
            The reminders issued, most worth asking first.

        Raises:
            ValueError: If ``max_count`` is negative.
        """
        if max_count is not None and max_count < 0:
            msg = f"max_count can't be negative; {max_count} was given."
            raise ValueError(msg)
        remaining = max(0, self.daily_limit() - self._issued_today())
        take = remaining if max_count is None else min(remaining, max_count)
        chosen = self._ranked()[:take]
        now = self._clock().isoformat()
        with closing(get_connection(self._db_path)) as conn:
            for candidate in chosen:
                detail = json.dumps({"reason": candidate.reason.value}, sort_keys=True)
                conn.execute(
                    _INSERT_EVENT,
                    (
                        candidate.item_id,
                        ReminderAction.ISSUED.value,
                        actor,
                        detail,
                        now,
                    ),
                )
            conn.commit()
        if chosen:
            audit("reminder.issued", count=len(chosen), actor=actor)
        return chosen

    # ------------------------------------------------------------------
    # Member choices
    # ------------------------------------------------------------------

    def _save(
        self,
        state: ReminderState,
        action: ReminderAction,
        actor: str,
        detail: dict[str, Any],
    ) -> ReminderState:
        """Store an item's reminder state and record the choice behind it.

        Args:
            state: The state to store.
            action: What the member chose.
            actor: The member.
            detail: Facts about the choice, for the history.

        Returns:
            The stored state.
        """
        now = self._clock()
        with closing(get_connection(self._db_path)) as conn:
            conn.execute(
                _UPSERT_STATE,
                (
                    state.item_id,
                    state.stop_asking,
                    state.snoozed_until.isoformat() if state.snoozed_until else None,
                    state.dismissed,
                    state.dismissed_observation_id,
                    actor,
                    now.isoformat(),
                ),
            ).fetchall()
            conn.execute(
                _INSERT_EVENT,
                (
                    state.item_id,
                    action.value,
                    actor,
                    json.dumps(detail, sort_keys=True),
                    now.isoformat(),
                ),
            )
            conn.commit()
        audit(f"reminder.{action.value}", item_id=state.item_id, actor=actor)
        return self.state(state.item_id)

    def snooze(self, item_id: int, *, days: int, actor: str) -> ReminderState:
        """Hold an item's reminders back for a number of days.

        Args:
            item_id: The item.
            days: How long, 1 to ``MAX_SNOOZE_DAYS``.
            actor: The member snoozing it.

        Returns:
            The item's reminder state.

        Raises:
            ItemNotFoundError: If no item has this id.
            SnoozeInvalidError: If ``days`` is out of range.
        """
        if not 1 <= days <= MAX_SNOOZE_DAYS:
            raise SnoozeInvalidError(days)
        current = self.state(item_id)
        until = self._clock() + dt.timedelta(days=days)
        return self._save(
            current.model_copy(update={"snoozed_until": until}),
            ReminderAction.SNOOZED,
            actor,
            {"days": days},
        )

    def dismiss(self, item_id: int, *, actor: str) -> ReminderState:
        """Hold an item's reminders back until new evidence arrives.

        Args:
            item_id: The item.
            actor: The member dismissing it.

        Returns:
            The item's reminder state.

        Raises:
            ItemNotFoundError: If no item has this id.
        """
        current = self.state(item_id)
        observation = self._ledger.snapshot(current.item_id).observation
        observation_id = observation.id if observation is not None else None
        return self._save(
            current.model_copy(
                update={"dismissed": True, "dismissed_observation_id": observation_id}
            ),
            ReminderAction.DISMISSED,
            actor,
            {"observation_id": observation_id},
        )

    def stop(self, item_id: int, *, actor: str) -> ReminderState:
        """Stop asking about an item until a member turns it back on.

        Args:
            item_id: The item.
            actor: The member.

        Returns:
            The item's reminder state.

        Raises:
            ItemNotFoundError: If no item has this id.
        """
        current = self.state(item_id)
        return self._save(
            current.model_copy(update={"stop_asking": True}),
            ReminderAction.STOPPED,
            actor,
            {},
        )

    def resume(self, item_id: int, *, actor: str) -> ReminderState:
        """Start asking about an item again, clearing every hold on it.

        Args:
            item_id: The item.
            actor: The member.

        Returns:
            The item's reminder state.

        Raises:
            ItemNotFoundError: If no item has this id.
        """
        current = self.state(item_id)
        return self._save(
            current.model_copy(
                update={
                    "stop_asking": False,
                    "snoozed_until": None,
                    "dismissed": False,
                    "dismissed_observation_id": None,
                }
            ),
            ReminderAction.RESUMED,
            actor,
            {},
        )
