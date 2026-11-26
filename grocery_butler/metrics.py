"""Operational metrics: how well the system is doing its job (ADR 0015).

Five numbers, read on demand:

- **extraction failures**: model replies that could not be turned into
  structured rows. Counted as they happen.
- **adapter failures**: fulfillment handoffs that failed. Counted as they
  happen.
- **correction rate**: of the proposals a member has decided, the share
  they had to correct. Read from the proposals themselves.
- **stale inventory**: items whose last observation is older than its
  freshness window. Read from the ledger.
- **reconciliation gaps**: needs marked ordered that nobody has confirmed
  received within the grace period, plus handoffs that old with no
  confirmed receipt. Read from the needs, attempts and receipts.

The first two leave no other trace, so they are stored as daily counters.
The other three are derived from rows that already exist, so they can't
drift from the truth.
"""

from __future__ import annotations

import datetime as dt
from contextlib import closing
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from grocery_butler.db import get_connection
from grocery_butler.inventory_ledger import InventoryLedger, utc_now
from grocery_butler.models import NeedStatus, ProposalDecision

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from grocery_butler.db.adapter import DatabaseConnection

# How long an ordered need may wait for a receipt before it counts as a gap.
RECONCILIATION_GRACE = dt.timedelta(days=3)

_INCREMENT = (
    "INSERT INTO operational_counters (name, day, count) VALUES (?, ?, ?)"
    " ON CONFLICT (name, day)"
    " DO UPDATE SET count = operational_counters.count + excluded.count"
    " RETURNING count"
)
_TOTAL = (
    "SELECT COALESCE(SUM(count), 0) AS total FROM operational_counters WHERE name = ?"
)
_TOTAL_SINCE = (
    "SELECT COALESCE(SUM(count), 0) AS total FROM operational_counters"
    " WHERE name = ? AND day >= ?"
)
_DECISIONS = (
    "SELECT decision, COUNT(*) AS n FROM observation_proposals"
    " WHERE decision != ? GROUP BY decision"
)
_GAPS = (
    "SELECT COUNT(*) AS n FROM replenishment_needs WHERE status = ? AND updated_at < ?"
)
_HANDOFF_GAPS = (
    "SELECT COUNT(*) AS n FROM fulfillment_attempts"
    " WHERE status = 'created' AND created_at < ?"
    " AND id NOT IN ("
    "   SELECT attempt_id FROM receipts"
    "   WHERE attempt_id IS NOT NULL AND status = 'confirmed'"
    " )"
)


class Counter(StrEnum):
    """The things counted as they happen."""

    EXTRACTION_FAILURES = "extraction_failures"
    ADAPTER_FAILURES = "adapter_failures"


# The audit events that advance a counter.
COUNTED_EVENTS: dict[str, Counter] = {
    "extraction.failed": Counter.EXTRACTION_FAILURES,
    "adapter.failed": Counter.ADAPTER_FAILURES,
}


def _number(
    conn: DatabaseConnection, sql: str, params: Sequence[Any], column: str
) -> int:
    """Run a query that yields one number.

    Args:
        conn: An open connection.
        sql: The query.
        params: Its parameters.
        column: The column holding the number.

    Returns:
        The number, or 0 when the query returned no row.
    """
    row = conn.execute(sql, params).fetchone()
    return int(row[column]) if row is not None else 0


class Metrics:
    """Reads and advances the operational metrics.

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

    # ------------------------------------------------------------------
    # Counters
    # ------------------------------------------------------------------

    def increment(self, counter: Counter, by: int = 1) -> int:
        """Advance a counter for today.

        Args:
            counter: Which counter.
            by: How much to add. At least 1.

        Returns:
            Today's count after adding.

        Raises:
            ValueError: If ``by`` is less than 1.
        """
        if by < 1:
            msg = f"A counter only goes up; can't add {by}."
            raise ValueError(msg)
        day = self._clock().date().isoformat()
        with closing(get_connection(self._db_path)) as conn:
            count = _number(conn, _INCREMENT, (counter.value, day, by), "count")
            conn.commit()
        return count

    def count_event(self, event: str) -> None:
        """Advance the counter an audit event stands for, if any.

        This is the callable bound to :func:`observability.bound`.

        Args:
            event: An audit event name.
        """
        counter = COUNTED_EVENTS.get(event)
        if counter is not None:
            self.increment(counter)

    def total(self, counter: Counter, days: int | None = None) -> int:
        """Read a counter.

        Args:
            counter: Which counter.
            days: Count only the last this-many days, today included.
                None counts everything.

        Returns:
            The count.
        """
        with closing(get_connection(self._db_path)) as conn:
            if days is None:
                return _number(conn, _TOTAL, (counter.value,), "total")
            since = self._clock().date() - dt.timedelta(days=days - 1)
            return _number(
                conn, _TOTAL_SINCE, (counter.value, since.isoformat()), "total"
            )

    # ------------------------------------------------------------------
    # Derived
    # ------------------------------------------------------------------

    def correction_rate(self) -> dict[str, Any]:
        """Report how often a member had to correct a proposal.

        Returns:
            ``decided`` (proposals with a decision), ``corrected``, and
            ``rate`` between 0 and 1, or None when nothing was decided.
        """
        with closing(get_connection(self._db_path)) as conn:
            rows = conn.execute(
                _DECISIONS, (ProposalDecision.PENDING.value,)
            ).fetchall()
        counts = {row["decision"]: int(row["n"]) for row in rows}
        decided = sum(counts.values())
        corrected = counts.get(ProposalDecision.CORRECTED.value, 0)
        return {
            "decided": decided,
            "corrected": corrected,
            "rate": round(corrected / decided, 4) if decided else None,
        }

    def stale_inventory(self) -> int:
        """Count items whose last observation has gone stale.

        Returns:
            The number of items past their freshness window.
        """
        ledger = InventoryLedger(self._db_path, clock=self._clock)
        return sum(1 for snapshot in ledger.snapshots() if snapshot.stale)

    def reconciliation_gaps(self) -> int:
        """Count what was handed off or ordered and never confirmed.

        Two kinds of gap, added together: a need marked ordered longer ago
        than the grace period and not yet received, and a handoff created
        longer ago than the grace period that no confirmed receipt points
        at.

        Returns:
            The number of gaps.
        """
        cutoff = (self._clock() - RECONCILIATION_GRACE).isoformat()
        with closing(get_connection(self._db_path)) as conn:
            needs = _number(conn, _GAPS, (NeedStatus.ORDERED.value, cutoff), "n")
            handoffs = _number(conn, _HANDOFF_GAPS, (cutoff,), "n")
        return needs + handoffs

    def snapshot(self) -> dict[str, Any]:
        """Read all five metrics at once.

        Returns:
            Each metric by name. Counters give ``total`` and ``last_7_days``.
        """
        return {
            "as_of": self._clock().isoformat(),
            "extraction_failures": self._counter_view(Counter.EXTRACTION_FAILURES),
            "adapter_failures": self._counter_view(Counter.ADAPTER_FAILURES),
            "correction_rate": self.correction_rate(),
            "stale_inventory": self.stale_inventory(),
            "reconciliation_gaps": self.reconciliation_gaps(),
        }

    def _counter_view(self, counter: Counter) -> dict[str, int]:
        """Summarise one counter.

        Args:
            counter: Which counter.

        Returns:
            Its all-time total and its last seven days.
        """
        return {
            "total": self.total(counter),
            "last_7_days": self.total(counter, days=7),
        }
