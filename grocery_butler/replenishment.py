"""Explicit replenishment needs: one per item, with reasons and a state machine.

Implements ADR 0008. A need says a household item should be bought. It has a
status, the reasons it is wanted, and a record of every status change. At
most one need per item is open at a time, so an item wanted for several
reasons is one need that lists them all. Inventory observations raise and
withdraw suggestions; only a person approves, defers or dismisses.
"""

from __future__ import annotations

import datetime as dt
import json
from contextlib import closing
from typing import TYPE_CHECKING, Any

from grocery_butler.db import get_connection
from grocery_butler.household_items import HouseholdItemStore
from grocery_butler.inventory_ledger import InventoryLedger, utc_now
from grocery_butler.models import (
    OPEN_NEED_STATUSES,
    InventorySnapshot,
    NeedEvent,
    NeedReason,
    NeedReasonKind,
    NeedStatus,
    ReplenishmentNeed,
    StockState,
)

if TYPE_CHECKING:
    from collections.abc import Callable, Collection

    from grocery_butler.db.adapter import DatabaseConnection

# The moves ADR 0008 allows. Closed statuses have none.
ALLOWED_MOVES: dict[NeedStatus, frozenset[NeedStatus]] = {
    NeedStatus.SUGGESTED: frozenset(
        {
            NeedStatus.APPROVED,
            NeedStatus.DEFERRED,
            NeedStatus.DISMISSED,
            NeedStatus.CANCELLED,
        }
    ),
    NeedStatus.APPROVED: frozenset(
        {
            NeedStatus.PLANNED,
            NeedStatus.DEFERRED,
            NeedStatus.DISMISSED,
            NeedStatus.CANCELLED,
        }
    ),
    NeedStatus.DEFERRED: frozenset(
        {
            NeedStatus.SUGGESTED,
            NeedStatus.APPROVED,
            NeedStatus.DISMISSED,
            NeedStatus.CANCELLED,
        }
    ),
    NeedStatus.PLANNED: frozenset(
        {NeedStatus.ORDERED, NeedStatus.APPROVED, NeedStatus.CANCELLED}
    ),
    NeedStatus.ORDERED: frozenset(
        {NeedStatus.RECEIVED, NeedStatus.APPROVED, NeedStatus.CANCELLED}
    ),
}

INVENTORY_SOURCE = "inventory"
SYNC_ACTOR = "system"

# The inventory reason each stated state calls for.
_REASON_FOR_STATE: dict[StockState, NeedReasonKind] = {
    StockState.LOW: NeedReasonKind.LOW_STOCK,
    StockState.OUT: NeedReasonKind.OUT,
}
_INVENTORY_REASONS = (NeedReasonKind.LOW_STOCK, NeedReasonKind.OUT)

_OPEN_VALUES = tuple(sorted(status.value for status in OPEN_NEED_STATUSES))

_SELECT_NEED = (
    "SELECT id, item_id, status, deferred_until, created_at, updated_at"
    " FROM replenishment_needs WHERE id = ?"
)
_SELECT_ALL_NEEDS = (
    "SELECT id, item_id, status, deferred_until, created_at, updated_at"
    " FROM replenishment_needs ORDER BY created_at, id"
)
_SELECT_NEEDS_OF_ITEM = (
    "SELECT n.id, n.item_id, n.status, n.deferred_until, n.created_at,"
    " n.updated_at FROM replenishment_needs n"
    " WHERE n.item_id IN"
    " (SELECT id FROM household_items WHERE id = ? OR merged_into_id = ?)"
    " ORDER BY n.created_at, n.id"
)
_SELECT_REASONS = (
    "SELECT id, kind, source_key, detail, active, created_at"
    " FROM replenishment_need_reasons WHERE need_id = ? ORDER BY id"
)


class NeedNotFoundError(LookupError):
    """Raised when a replenishment need id does not exist."""

    def __init__(self, need_id: int) -> None:
        """Name the missing need.

        Args:
            need_id: The id that was looked up.
        """
        super().__init__(f"No replenishment need has id {need_id}.")
        self.need_id = need_id


class InvalidNeedMoveError(ValueError):
    """Raised when a status change is not one ADR 0008 allows."""

    def __init__(self, need_id: int, current: NeedStatus, target: NeedStatus) -> None:
        """Say which move was refused and what is allowed instead.

        Args:
            need_id: The need that was to be moved.
            current: Its current status.
            target: The status that was asked for.
        """
        allowed = sorted(status.value for status in ALLOWED_MOVES.get(current, ()))
        options = ", ".join(allowed) if allowed else "nothing; it is closed"
        super().__init__(
            f"Need {need_id} is {current.value} and can't become {target.value}. "
            f"From {current.value} it can become: {options}."
        )
        self.need_id = need_id
        self.current = current
        self.target = target


class ReplenishmentNeeds:
    """Create, move and list replenishment needs.

    Opens a connection per operation. The schema must already be migrated.

    Args:
        db_path: SQLite file path or PostgreSQL URL.
        clock: Returns the current UTC time. Tests pass a fixed clock.
    """

    def __init__(
        self, db_path: str, clock: Callable[[], dt.datetime] = utc_now
    ) -> None:
        """Initialize the service.

        Args:
            db_path: SQLite file path or PostgreSQL URL.
            clock: Returns the current UTC time.
        """
        self._db_path = db_path
        self._clock = clock
        self._items = HouseholdItemStore(db_path)
        self._ledger = InventoryLedger(db_path, clock=clock)

    # -- reads ---------------------------------------------------------

    def get(self, need_id: int) -> ReplenishmentNeed | None:
        """Return one need with its reasons.

        Args:
            need_id: The need's id.

        Returns:
            The need, or None if there is none.
        """
        with closing(get_connection(self._db_path)) as conn:
            row = conn.execute(_SELECT_NEED, (need_id,)).fetchone()
            return self._build(conn, row) if row else None

    def list_needs(
        self, statuses: Collection[NeedStatus] | None = None
    ) -> list[ReplenishmentNeed]:
        """List needs, oldest first.

        Args:
            statuses: Statuses to include. Defaults to every open status.

        Returns:
            The matching needs with their reasons.
        """
        wanted = OPEN_NEED_STATUSES if statuses is None else frozenset(statuses)
        with closing(get_connection(self._db_path)) as conn:
            rows = conn.execute(_SELECT_ALL_NEEDS).fetchall()
            return [
                self._build(conn, row)
                for row in rows
                if NeedStatus(row["status"]) in wanted
            ]

    def open_need_for_item(self, item_id: int) -> ReplenishmentNeed | None:
        """Return the open need of an item, following merges.

        Args:
            item_id: Any item id; a merged id is followed to its target.

        Returns:
            The item's oldest open need, or None when it has none or the
            item does not exist.
        """
        item = self._items.get(item_id)
        if item is None:
            return None
        canonical_id = item.merged_into_id or item.id
        open_needs = self._open_needs(canonical_id)
        return open_needs[0] if open_needs else None

    def history(self, need_id: int) -> list[NeedEvent]:
        """Return a need's status changes, oldest first.

        Args:
            need_id: The need's id.

        Returns:
            Every recorded status change.
        """
        with closing(get_connection(self._db_path)) as conn:
            rows = conn.execute(
                "SELECT id, need_id, from_status, to_status, actor, note, created_at"
                " FROM replenishment_need_events WHERE need_id = ? ORDER BY id",
                (need_id,),
            ).fetchall()
        return [
            NeedEvent(
                id=row["id"],
                need_id=row["need_id"],
                from_status=(
                    NeedStatus(row["from_status"]) if row["from_status"] else None
                ),
                to_status=NeedStatus(row["to_status"]),
                actor=row["actor"],
                note=row["note"],
                created_at=dt.datetime.fromisoformat(row["created_at"]),
            )
            for row in rows
        ]

    # -- writes --------------------------------------------------------

    def add_reason(
        self,
        item_id: int,
        kind: NeedReasonKind,
        *,
        source_key: str,
        actor: str,
        detail: dict[str, Any] | None = None,
    ) -> ReplenishmentNeed:
        """Record a reason an item is wanted.

        The reason joins the item's open need, or starts a new one. A need
        started by a manual request is ``approved``; any other starts
        ``suggested``. The same kind from the same source is stored once,
        with its detail refreshed.

        Args:
            item_id: Any item id; a merged id is followed to its target.
            kind: Why the item is wanted.
            source_key: Which source says so, e.g. ``inventory``.
            actor: Who or what is recording it.
            detail: JSON-serializable facts about the reason.

        Returns:
            The need the reason now belongs to.

        Raises:
            ItemNotFoundError: If no item has this id.
        """
        canonical_id = self._items.canonical(item_id).id
        now = self._clock().isoformat()
        payload = json.dumps(detail or {}, sort_keys=True)
        existing = self._open_needs(canonical_id)
        with closing(get_connection(self._db_path)) as conn:
            if existing:
                need_id = existing[0].id
            else:
                start = (
                    NeedStatus.APPROVED
                    if kind is NeedReasonKind.MANUAL_REQUEST
                    else NeedStatus.SUGGESTED
                )
                need_id = self._insert_need(conn, canonical_id, start, actor, now)
            self._upsert_reason(conn, need_id, kind, source_key, payload, now)
            conn.commit()
            return self._require(conn, need_id)

    def withdraw_reason(
        self, item_id: int, kind: NeedReasonKind, *, source_key: str, actor: str
    ) -> None:
        """Mark a reason as no longer holding.

        If that leaves a ``suggested`` need with no active reason, the need
        is cancelled. A need a person already decided on is left as it is.
        Withdrawing a reason that isn't there does nothing.

        Args:
            item_id: Any item id; a merged id is followed to its target.
            kind: The reason's kind.
            source_key: The source that no longer says so.
            actor: Who or what is withdrawing it.
        """
        need = self.open_need_for_item(item_id)
        if need is None:
            return
        with closing(get_connection(self._db_path)) as conn:
            conn.execute(
                "UPDATE replenishment_need_reasons SET active = ?"
                " WHERE need_id = ? AND kind = ? AND source_key = ?",
                (False, need.id, kind.value, source_key),
            )
            conn.commit()
        refreshed = self.get(need.id)
        if (
            refreshed is not None
            and refreshed.status is NeedStatus.SUGGESTED
            and not refreshed.active_reasons
        ):
            self.transition(
                need.id,
                NeedStatus.CANCELLED,
                actor=actor,
                note="No reason for it remains.",
            )

    def transition(
        self,
        need_id: int,
        to_status: NeedStatus,
        *,
        actor: str,
        note: str = "",
        deferred_until: dt.datetime | None = None,
    ) -> ReplenishmentNeed:
        """Move a need to another status, if ADR 0008 allows the move.

        Args:
            need_id: The need to move.
            to_status: The status to move it to.
            actor: Who is moving it.
            note: Why, if known.
            deferred_until: When a deferral ends. Only valid with
                ``DEFERRED``; omit it for an open-ended deferral.

        Returns:
            The need after the move.

        Raises:
            NeedNotFoundError: If the need does not exist.
            InvalidNeedMoveError: If the move is not allowed.
            ValueError: If ``deferred_until`` is given for another status.
        """
        if deferred_until is not None and to_status is not NeedStatus.DEFERRED:
            msg = "A date can be set on only a deferred need."
            raise ValueError(msg)
        now = self._clock().isoformat()
        until = (
            deferred_until.astimezone(dt.UTC).isoformat() if deferred_until else None
        )
        with closing(get_connection(self._db_path)) as conn:
            need = self._require(conn, need_id)
            if to_status not in ALLOWED_MOVES.get(need.status, frozenset()):
                raise InvalidNeedMoveError(need_id, need.status, to_status)
            conn.execute(
                "UPDATE replenishment_needs"
                " SET status = ?, deferred_until = ?, updated_at = ? WHERE id = ?",
                (to_status.value, until, now, need_id),
            )
            self._record(conn, need_id, need.status, to_status, actor, note, now)
            conn.commit()
            return self._require(conn, need_id)

    def sync_item(self, item_id: int) -> ReplenishmentNeed | None:
        """Bring an item's need in step with its inventory snapshot.

        Wakes a deferral whose date has passed, then applies ADR 0008's
        rule: ``low`` and ``out`` add their reason, ``on_hand`` withdraws
        both, and ``unknown`` changes nothing. A dismissal holds against
        the observation that prompted it.

        Args:
            item_id: Any item id; a merged id is followed to its target.

        Returns:
            The item's open need after the sync, or None when it has none.

        Raises:
            ItemNotFoundError: If no item has this id.
        """
        snapshot = self._ledger.snapshot(item_id)
        self._wake_if_due(snapshot.item_id)
        wanted = _REASON_FOR_STATE.get(snapshot.state)
        # Add before withdrawing: a need going from low to out must never be
        # left without a reason in between, or it would be cancelled.
        if wanted is not None and not self._dismissed_since(snapshot):
            observation_id = snapshot.observation.id if snapshot.observation else None
            self.add_reason(
                snapshot.item_id,
                wanted,
                source_key=INVENTORY_SOURCE,
                actor=SYNC_ACTOR,
                detail={"observation_id": observation_id},
            )
        if snapshot.state is not StockState.UNKNOWN:
            for kind in _INVENTORY_REASONS:
                if kind is not wanted:
                    self.withdraw_reason(
                        snapshot.item_id,
                        kind,
                        source_key=INVENTORY_SOURCE,
                        actor=SYNC_ACTOR,
                    )
        return self.open_need_for_item(snapshot.item_id)

    def sync_all(self) -> None:
        """Sync the need of every active household item."""
        for item in self._items.list_items():
            self.sync_item(item.id)

    def fold_merged_needs(
        self, item_id: int, *, actor: str
    ) -> ReplenishmentNeed | None:
        """Fold the open needs a merge left on one item into the oldest.

        Two items that each had an open need can be merged (ADR 0006). The
        survivor then has two. This moves the newer needs' active reasons
        onto the oldest and cancels the newer ones.

        Args:
            item_id: Any item id; a merged id is followed to its target.
            actor: Who is folding them, normally whoever merged the items.

        Returns:
            The surviving open need, or None when the item has none.

        Raises:
            ItemNotFoundError: If no item has this id.
        """
        canonical_id = self._items.canonical(item_id).id
        open_needs = self._open_needs(canonical_id)
        if not open_needs:
            return None
        keeper, extras = open_needs[0], open_needs[1:]
        now = self._clock().isoformat()
        for extra in extras:
            with closing(get_connection(self._db_path)) as conn:
                for reason in extra.active_reasons:
                    self._upsert_reason(
                        conn,
                        keeper.id,
                        reason.kind,
                        reason.source_key,
                        json.dumps(reason.detail, sort_keys=True),
                        now,
                    )
                conn.execute(
                    "UPDATE replenishment_needs SET status = ?, deferred_until = NULL,"
                    " updated_at = ? WHERE id = ?",
                    (NeedStatus.CANCELLED.value, now, extra.id),
                )
                self._record(
                    conn,
                    extra.id,
                    extra.status,
                    NeedStatus.CANCELLED,
                    actor,
                    f"Folded into need {keeper.id} after its items were merged.",
                    now,
                )
                conn.commit()
        return self.get(keeper.id)

    # -- internals -----------------------------------------------------

    def _open_needs(self, canonical_id: int) -> list[ReplenishmentNeed]:
        """Return a canonical item's open needs, oldest first.

        Includes needs recorded against items merged into it.

        Args:
            canonical_id: A canonical item id.

        Returns:
            The open needs; normally zero or one.
        """
        with closing(get_connection(self._db_path)) as conn:
            rows = conn.execute(
                _SELECT_NEEDS_OF_ITEM, (canonical_id, canonical_id)
            ).fetchall()
            return [
                self._build(conn, row) for row in rows if row["status"] in _OPEN_VALUES
            ]

    def _wake_if_due(self, canonical_id: int) -> None:
        """Return a deferred need to suggested once its date has passed.

        Args:
            canonical_id: A canonical item id.
        """
        for need in self._open_needs(canonical_id):
            due = (
                need.deferred_until is not None and need.deferred_until < self._clock()
            )
            if need.status is NeedStatus.DEFERRED and due:
                self.transition(
                    need.id,
                    NeedStatus.SUGGESTED,
                    actor=SYNC_ACTOR,
                    note="The deferral ended.",
                )

    def _dismissed_since(self, snapshot: InventorySnapshot) -> bool:
        """Tell whether the item's latest need was dismissed after this evidence.

        Args:
            snapshot: The item's current snapshot.

        Returns:
            True when a dismissal is newer than the snapshot's observation,
            so the same evidence must not raise a new suggestion.
        """
        if snapshot.observation is None:
            return False
        with closing(get_connection(self._db_path)) as conn:
            rows = conn.execute(
                _SELECT_NEEDS_OF_ITEM, (snapshot.item_id, snapshot.item_id)
            ).fetchall()
        if not rows or rows[-1]["status"] != NeedStatus.DISMISSED.value:
            return False
        dismissed_at = dt.datetime.fromisoformat(rows[-1]["updated_at"])
        return dismissed_at >= snapshot.observation.observed_at

    def _insert_need(
        self,
        conn: DatabaseConnection,
        item_id: int,
        status: NeedStatus,
        actor: str,
        now: str,
    ) -> int:
        """Insert a need and its first event (not committed here).

        Args:
            conn: Open connection.
            item_id: The canonical item the need is for.
            status: The status the need starts in.
            actor: Who or what created it.
            now: The current time, ISO 8601.

        Returns:
            The new need's id.
        """
        cursor = conn.execute(
            "INSERT INTO replenishment_needs (item_id, status, created_at, updated_at)"
            " VALUES (?, ?, ?, ?)",
            (item_id, status.value, now, now),
        )
        need_id = cursor.lastrowid
        if need_id is None:  # pragma: no cover - both backends return the id
            raise NeedNotFoundError(0)
        self._record(conn, need_id, None, status, actor, "", now)
        return need_id

    def _upsert_reason(
        self,
        conn: DatabaseConnection,
        need_id: int,
        kind: NeedReasonKind,
        source_key: str,
        payload: str,
        now: str,
    ) -> None:
        """Add a reason, or refresh and reactivate the one already there.

        Args:
            conn: Open connection.
            need_id: The need the reason belongs to.
            kind: The reason's kind.
            source_key: The source that says so.
            payload: The reason's detail as JSON text.
            now: The current time, ISO 8601.
        """
        updated = conn.execute(
            "UPDATE replenishment_need_reasons SET detail = ?, active = ?"
            " WHERE need_id = ? AND kind = ? AND source_key = ?",
            (payload, True, need_id, kind.value, source_key),
        )
        if updated.rowcount:
            return
        conn.execute(
            "INSERT INTO replenishment_need_reasons"
            " (need_id, kind, source_key, detail, created_at) VALUES (?, ?, ?, ?, ?)",
            (need_id, kind.value, source_key, payload, now),
        )

    def _record(
        self,
        conn: DatabaseConnection,
        need_id: int,
        from_status: NeedStatus | None,
        to_status: NeedStatus,
        actor: str,
        note: str,
        now: str,
    ) -> None:
        """Append a status change to a need's audit trail (not committed).

        Args:
            conn: Open connection.
            need_id: The need that changed.
            from_status: Its status before, or None for a new need.
            to_status: Its status after.
            actor: Who or what made the change.
            note: Why, if known.
            now: The current time, ISO 8601.
        """
        conn.execute(
            "INSERT INTO replenishment_need_events"
            " (need_id, from_status, to_status, actor, note, created_at)"
            " VALUES (?, ?, ?, ?, ?, ?)",
            (
                need_id,
                from_status.value if from_status else None,
                to_status.value,
                actor,
                note,
                now,
            ),
        )

    def _require(self, conn: DatabaseConnection, need_id: int) -> ReplenishmentNeed:
        """Fetch one need on an open connection, or raise.

        Args:
            conn: Open connection.
            need_id: The need's id.

        Returns:
            The need.

        Raises:
            NeedNotFoundError: If the need does not exist.
        """
        row = conn.execute(_SELECT_NEED, (need_id,)).fetchone()
        if row is None:
            raise NeedNotFoundError(need_id)
        return self._build(conn, row)

    def _build(self, conn: DatabaseConnection, row: Any) -> ReplenishmentNeed:
        """Build a need, with its reasons, from a need row.

        Args:
            conn: Open connection.
            row: A row from one of the need SELECT statements.

        Returns:
            The need.
        """
        reasons = [
            NeedReason(
                id=reason["id"],
                kind=NeedReasonKind(reason["kind"]),
                source_key=reason["source_key"],
                detail=json.loads(reason["detail"]),
                active=bool(reason["active"]),
                created_at=dt.datetime.fromisoformat(reason["created_at"]),
            )
            for reason in conn.execute(_SELECT_REASONS, (row["id"],)).fetchall()
        ]
        until = row["deferred_until"]
        return ReplenishmentNeed(
            id=row["id"],
            item_id=row["item_id"],
            status=NeedStatus(row["status"]),
            deferred_until=dt.datetime.fromisoformat(until) if until else None,
            reasons=reasons,
            created_at=dt.datetime.fromisoformat(row["created_at"]),
            updated_at=dt.datetime.fromisoformat(row["updated_at"]),
        )
