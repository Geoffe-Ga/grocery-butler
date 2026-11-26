"""Append-only inventory observation ledger and its derived snapshot.

Implements ADR 0007. Observations are only ever inserted. A correction or a
retraction is a new row that supersedes an earlier one. An item's current
state is derived on read from its latest valid observation, and reports
``unknown`` once that observation is older than its category's freshness
window.
"""

from __future__ import annotations

import datetime as dt
from contextlib import closing
from typing import TYPE_CHECKING, Any

from grocery_butler.db import get_connection
from grocery_butler.household_items import (
    MIGRATION_ACTOR,
    HouseholdItemStore,
    ItemNotFoundError,
)
from grocery_butler.models import (
    HouseholdItem,
    IngredientCategory,
    InventoryObservation,
    InventorySnapshot,
    NewObservation,
    ObservationKind,
    ObservationSource,
    StockState,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from grocery_butler.db.adapter import DatabaseConnection

# Days a stated state stays believable, per category (ADR 0007). First
# guesses for dogfood to tune.
FRESHNESS_DAYS: dict[IngredientCategory, int] = {
    IngredientCategory.PRODUCE: 7,
    IngredientCategory.MEAT: 7,
    IngredientCategory.BAKERY: 7,
    IngredientCategory.DELI: 7,
    IngredientCategory.DAIRY: 10,
    IngredientCategory.BEVERAGES: 30,
    IngredientCategory.OTHER: 30,
    IngredientCategory.FROZEN: 60,
    IngredientCategory.PANTRY_DRY: 60,
}
DEFAULT_FRESHNESS_DAYS = 30

_SECONDS_PER_DAY = 86400.0

_SELECT_OBSERVATIONS_FOR_ITEMS = (
    "SELECT o.id, o.item_id, o.kind, o.state, o.quantity, o.unit, o.source,"
    " o.confidence, o.actor, o.observed_at, o.recorded_at, o.evidence_ref,"
    " o.notes, o.supersedes_id, later.id AS superseded_by_id"
    " FROM inventory_observations o"
    " LEFT JOIN inventory_observations later ON later.supersedes_id = o.id"
    " WHERE o.item_id IN"
    " (SELECT id FROM household_items WHERE id = ? OR merged_into_id = ?)"
    " ORDER BY o.observed_at, o.id"
)
_SELECT_OBSERVATION_BY_ID = (
    "SELECT o.id, o.item_id, o.kind, o.state, o.quantity, o.unit, o.source,"
    " o.confidence, o.actor, o.observed_at, o.recorded_at, o.evidence_ref,"
    " o.notes, o.supersedes_id, later.id AS superseded_by_id"
    " FROM inventory_observations o"
    " LEFT JOIN inventory_observations later ON later.supersedes_id = o.id"
    " WHERE o.id = ?"
)
_INSERT_OBSERVATION = (
    "INSERT INTO inventory_observations"
    " (item_id, kind, state, quantity, unit, source, confidence, actor,"
    "  observed_at, recorded_at, evidence_ref, notes, supersedes_id)"
    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)"
)

IMPORT_NOTE = "Imported from the inventory table when the ledger was created."


class ObservationNotFoundError(LookupError):
    """Raised when an observation id does not exist."""

    def __init__(self, observation_id: int) -> None:
        """Name the missing observation.

        Args:
            observation_id: The id that was looked up.
        """
        super().__init__(f"No inventory observation has id {observation_id}.")
        self.observation_id = observation_id


class AlreadySupersededError(ValueError):
    """Raised when correcting or retracting a row that was already replaced."""

    def __init__(self, observation_id: int, superseded_by_id: int) -> None:
        """Point at the row that already replaced this one.

        Args:
            observation_id: The observation the caller tried to replace.
            superseded_by_id: The row that already supersedes it.
        """
        super().__init__(
            f"Observation {observation_id} was already replaced by observation "
            f"{superseded_by_id}. Correct that one instead."
        )
        self.observation_id = observation_id
        self.superseded_by_id = superseded_by_id


def utc_now() -> dt.datetime:
    """Return the current time in UTC.

    Returns:
        A timezone-aware datetime.
    """
    return dt.datetime.now(tz=dt.UTC)


def to_utc(value: object) -> dt.datetime | None:
    """Read a stored or legacy timestamp as an aware UTC datetime.

    Accepts a datetime (naive ones are taken as UTC, which is what both
    databases store for ``CURRENT_TIMESTAMP``) or an ISO 8601 string, with a
    ``T`` or a space between date and time.

    Args:
        value: A datetime, an ISO 8601 string, or None.

    Returns:
        The time in UTC, or None when there is no usable value.
    """
    if isinstance(value, str):
        try:
            value = dt.datetime.fromisoformat(value)
        except ValueError:
            return None
    if not isinstance(value, dt.datetime):
        return None
    if value.tzinfo is None:
        return value.replace(tzinfo=dt.UTC)
    return value.astimezone(dt.UTC)


def freshness_days(item: HouseholdItem) -> int:
    """Return how many days a stated state stays believable for an item.

    Args:
        item: The household item.

    Returns:
        The freshness window for the item's category, in days.
    """
    if item.category is None:
        return DEFAULT_FRESHNESS_DAYS
    return FRESHNESS_DAYS[item.category]


def _row_to_observation(row: Any) -> InventoryObservation:
    """Build an InventoryObservation from a ledger row.

    Args:
        row: A row from one of the observation SELECT statements.

    Returns:
        The observation.
    """
    return InventoryObservation(
        id=row["id"],
        item_id=row["item_id"],
        kind=ObservationKind(row["kind"]),
        state=StockState(row["state"]) if row["state"] else None,
        quantity=row["quantity"],
        unit=row["unit"],
        source=ObservationSource(row["source"]),
        confidence=row["confidence"],
        actor=row["actor"],
        observed_at=dt.datetime.fromisoformat(row["observed_at"]),
        recorded_at=dt.datetime.fromisoformat(row["recorded_at"]),
        evidence_ref=row["evidence_ref"],
        notes=row["notes"],
        supersedes_id=row["supersedes_id"],
        superseded_by_id=row["superseded_by_id"],
    )


class InventoryLedger:
    """Append observations and derive what the household currently has.

    Opens a connection per operation. The schema must already be migrated.

    Args:
        db_path: SQLite file path or PostgreSQL URL.
        clock: Returns the current UTC time. Tests pass a fixed clock.
    """

    def __init__(
        self, db_path: str, clock: Callable[[], dt.datetime] = utc_now
    ) -> None:
        """Initialize the ledger.

        Args:
            db_path: SQLite file path or PostgreSQL URL.
            clock: Returns the current UTC time.
        """
        self._db_path = db_path
        self._clock = clock
        self._items = HouseholdItemStore(db_path)

    # -- writes --------------------------------------------------------

    def record(self, observation: NewObservation) -> InventoryObservation:
        """Append an observation.

        Args:
            observation: The validated observation to store.

        Returns:
            The stored row.

        Raises:
            ItemNotFoundError: If the observation's item does not exist.
        """
        return self._append(observation, None)

    def correct(
        self, observation_id: int, replacement: NewObservation
    ) -> InventoryObservation:
        """Replace an earlier observation with a corrected one.

        The earlier row stays in the ledger and stops counting. The
        replacement may name a different item, for an observation that was
        attached to the wrong thing.

        Args:
            observation_id: The observation that was wrong.
            replacement: What should have been recorded.

        Returns:
            The stored correction.

        Raises:
            ObservationNotFoundError: If the observation does not exist.
            AlreadySupersededError: If it was already corrected or retracted.
            ItemNotFoundError: If the replacement's item does not exist.
        """
        self._require_current(observation_id)
        return self._append(replacement, observation_id)

    def retract(
        self, observation_id: int, *, actor: str, notes: str = ""
    ) -> InventoryObservation:
        """Withdraw an observation without stating anything in its place.

        Args:
            observation_id: The observation to withdraw.
            actor: Who is withdrawing it.
            notes: Why, if known.

        Returns:
            The stored retraction.

        Raises:
            ObservationNotFoundError: If the observation does not exist.
            AlreadySupersededError: If it was already corrected or retracted.
        """
        target = self._require_current(observation_id)
        now = self._clock().isoformat()
        with closing(get_connection(self._db_path)) as conn:
            cursor = conn.execute(
                _INSERT_OBSERVATION,
                (
                    target.item_id,
                    ObservationKind.RETRACTION.value,
                    None,
                    None,
                    None,
                    ObservationSource.HUMAN.value,
                    None,
                    actor,
                    now,
                    now,
                    None,
                    notes,
                    observation_id,
                ),
            )
            conn.commit()
            return self._require(conn, cursor.lastrowid)

    # -- reads ---------------------------------------------------------

    def get(self, observation_id: int) -> InventoryObservation | None:
        """Return one observation.

        Args:
            observation_id: The observation's id.

        Returns:
            The observation, or None if there is none.
        """
        with closing(get_connection(self._db_path)) as conn:
            row = conn.execute(_SELECT_OBSERVATION_BY_ID, (observation_id,)).fetchone()
        return _row_to_observation(row) if row else None

    def history(self, item_id: int) -> list[InventoryObservation]:
        """Return every ledger row for an item, oldest observed first.

        Includes superseded rows and retractions, and rows recorded against
        items since merged into this one.

        Args:
            item_id: Any item id; a merged id is followed to its target.

        Returns:
            The item's full ledger history.

        Raises:
            ItemNotFoundError: If no item has this id.
        """
        canonical = self._items.canonical(item_id)
        with closing(get_connection(self._db_path)) as conn:
            rows = conn.execute(
                _SELECT_OBSERVATIONS_FOR_ITEMS, (canonical.id, canonical.id)
            ).fetchall()
        return [_row_to_observation(row) for row in rows]

    def snapshot(self, item_id: int) -> InventorySnapshot:
        """Derive what we currently believe about an item, and why.

        Args:
            item_id: Any item id; a merged id is followed to its target.

        Returns:
            The item's snapshot. Its state is ``UNKNOWN`` when nothing valid
            was observed or the latest observation is stale.

        Raises:
            ItemNotFoundError: If no item has this id.
        """
        canonical = self._items.canonical(item_id)
        return self._derive(canonical, self.history(canonical.id))

    def snapshots(self) -> list[InventorySnapshot]:
        """Derive the snapshot of every active item, ordered by item name.

        Returns:
            One snapshot per active household item.
        """
        return [
            self._derive(item, self.history(item.id))
            for item in self._items.list_items()
        ]

    # -- internals -----------------------------------------------------

    def _derive(
        self, item: HouseholdItem, history: list[InventoryObservation]
    ) -> InventorySnapshot:
        """Derive an item's snapshot from its ledger history.

        Args:
            item: The canonical item.
            history: The item's ledger rows, oldest observed first.

        Returns:
            The snapshot.
        """
        window = freshness_days(item)
        valid = [
            row
            for row in history
            if row.kind is ObservationKind.OBSERVATION and row.superseded_by_id is None
        ]
        if not valid:
            return InventorySnapshot(
                item_id=item.id,
                state=StockState.UNKNOWN,
                stated_state=None,
                stale=False,
                age_days=None,
                fresh_for_days=window,
                observation=None,
            )
        latest = valid[-1]
        age_days = (self._clock() - latest.observed_at).total_seconds() / (
            _SECONDS_PER_DAY
        )
        stale = age_days > window
        stated = latest.state or StockState.UNKNOWN
        return InventorySnapshot(
            item_id=item.id,
            state=StockState.UNKNOWN if stale else stated,
            stated_state=stated,
            stale=stale,
            age_days=age_days,
            fresh_for_days=window,
            observation=latest,
        )

    def insert_into(
        self,
        conn: DatabaseConnection,
        observation: NewObservation,
        supersedes_id: int | None = None,
    ) -> int:
        """Insert a stated observation on a caller's connection, uncommitted.

        For callers that must write several rows, or rows in other tables,
        in one transaction. The caller commits, and must have checked that
        the item exists.

        Args:
            conn: The caller's open connection.
            observation: The validated observation.
            supersedes_id: The row this one replaces, if any.

        Returns:
            The new row's id.

        Raises:
            ObservationNotFoundError: If the database returns no id.
        """
        now = self._clock()
        observed_at = (observation.observed_at or now).astimezone(dt.UTC)
        cursor = conn.execute(
            _INSERT_OBSERVATION,
            (
                observation.item_id,
                ObservationKind.OBSERVATION.value,
                observation.state.value,
                observation.quantity,
                observation.unit,
                observation.source.value,
                observation.confidence,
                observation.actor,
                observed_at.isoformat(),
                now.isoformat(),
                observation.evidence_ref,
                observation.notes,
                supersedes_id,
            ),
        )
        row_id = cursor.lastrowid
        if row_id is None:  # pragma: no cover - both backends return the id
            raise ObservationNotFoundError(0)
        return row_id

    def _append(
        self, observation: NewObservation, supersedes_id: int | None
    ) -> InventoryObservation:
        """Insert one stated observation and commit it.

        Args:
            observation: The validated observation.
            supersedes_id: The row this one replaces, if any.

        Returns:
            The stored row.

        Raises:
            ItemNotFoundError: If the observation's item does not exist.
        """
        if self._items.get(observation.item_id) is None:
            raise ItemNotFoundError(observation.item_id)
        with closing(get_connection(self._db_path)) as conn:
            row_id = self.insert_into(conn, observation, supersedes_id)
            conn.commit()
            return self._require(conn, row_id)

    def _require(
        self, conn: DatabaseConnection, observation_id: int | None
    ) -> InventoryObservation:
        """Fetch one observation on an open connection, or raise.

        Args:
            conn: Open connection.
            observation_id: The observation's id.

        Returns:
            The observation.

        Raises:
            ObservationNotFoundError: If the observation does not exist.
        """
        row = conn.execute(_SELECT_OBSERVATION_BY_ID, (observation_id,)).fetchone()
        if row is None:
            raise ObservationNotFoundError(observation_id or 0)
        return _row_to_observation(row)

    def _require_current(self, observation_id: int) -> InventoryObservation:
        """Fetch an observation that nothing has superseded yet.

        Args:
            observation_id: The observation's id.

        Returns:
            The observation.

        Raises:
            ObservationNotFoundError: If the observation does not exist.
            AlreadySupersededError: If a later row already replaced it.
        """
        with closing(get_connection(self._db_path)) as conn:
            target = self._require(conn, observation_id)
        if target.superseded_by_id is not None:
            raise AlreadySupersededError(observation_id, target.superseded_by_id)
        return target


def import_inventory_rows(db_path: str) -> None:
    """Import existing inventory rows into the ledger. Migration 011's hook.

    Each ``household_inventory`` row that is linked to an item with no ledger
    history becomes one ``system`` observation of its current status, dated
    at its last status change. Running it again changes nothing.

    Args:
        db_path: SQLite file path or PostgreSQL URL.
    """
    ledger = InventoryLedger(db_path)
    with closing(get_connection(db_path)) as conn:
        rows = conn.execute(
            "SELECT item_id, status, last_status_change FROM household_inventory"
            " WHERE item_id IS NOT NULL ORDER BY id"
        ).fetchall()
    for row in rows:
        if ledger.history(row["item_id"]):
            continue
        ledger.record(
            NewObservation(
                item_id=row["item_id"],
                state=StockState(row["status"]),
                source=ObservationSource.SYSTEM,
                actor=MIGRATION_ACTOR,
                observed_at=to_utc(row["last_status_change"]),
                notes=IMPORT_NOTE,
            )
        )
