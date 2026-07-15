"""Data access layer for staged pending actions.

The ``pending_actions`` table is the server-side staging area and audit
log for destructive operations (Safeway order submissions, brand and
preference changes). Rows are inserted as ``pending`` and resolved to
``approved``, ``denied``, or ``expired`` exactly once; an ``approved``
row may additionally transition to ``failed`` if execution errors out
after being claimed.

Uses the same connection-per-operation pattern as
:class:`grocery_butler.recipe_store.RecipeStore`, so it works against
both SQLite and PostgreSQL through the adapter layer.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

from grocery_butler.db import get_connection
from grocery_butler.models import PendingAction, PendingActionStatus

if TYPE_CHECKING:
    from grocery_butler.db.adapter import DatabaseConnection, DictRow


def _row_to_pending_action(row: DictRow) -> PendingAction:
    """Convert a database row to a PendingAction model.

    SQLite returns the JSON payload as a string and timestamps as ISO
    strings; PostgreSQL returns a dict (JSONB) and datetime objects.
    Both shapes are normalized here (pydantic parses ISO datetimes).

    Args:
        row: Row from the pending_actions table.

    Returns:
        PendingAction model instance.
    """
    payload = row["payload"]
    if isinstance(payload, str):
        payload = json.loads(payload)
    return PendingAction(
        action_id=row["action_id"],
        kind=row["kind"],
        payload=payload,
        status=PendingActionStatus(row["status"]),
        requester=row["requester"],
        resolver=row["resolver"],
        expires_at=row["expires_at"],
        created_at=row["created_at"],
        resolved_at=row["resolved_at"],
    )


class PendingActionsStore:
    """CRUD operations for the pending_actions staging table."""

    def __init__(self, db_path: str) -> None:
        """Initialize the store with a database path or URL.

        Args:
            db_path: SQLite file path or PostgreSQL URL.
        """
        self.db_path = db_path

    def _connect(self) -> DatabaseConnection:
        """Open a new database connection.

        Returns:
            Configured DatabaseConnection.
        """
        return get_connection(self.db_path)

    def insert_pending_action(
        self,
        *,
        action_id: str,
        kind: str,
        payload: dict[str, Any],
        expires_at: dt.datetime,
        requester: str = "rubotpaul",
    ) -> PendingAction:
        """Stage a new pending action.

        Args:
            action_id: Unique identifier (typically a UUID4 string).
            kind: Action kind, e.g. ``safeway_order_submit``.
            payload: JSON-serializable action payload.
            expires_at: Deadline after which the action should not run.
            requester: Origin of the request (defaults to ``rubotpaul``).

        Returns:
            The stored row as a PendingAction (including DB defaults).

        Raises:
            IntegrityError: If ``action_id`` already exists.
            RuntimeError: If the inserted row cannot be read back.
        """
        conn = self._connect()
        try:
            result = conn.execute(
                "INSERT INTO pending_actions "
                "(action_id, kind, payload, status, requester, expires_at) "
                "VALUES (?, ?, ?, ?, ?, ?) RETURNING action_id",
                (
                    action_id,
                    kind,
                    json.dumps(payload),
                    PendingActionStatus.PENDING.value,
                    requester,
                    expires_at.isoformat(),
                ),
            )
            result.fetchall()  # drain RETURNING so commit sees no open cursor
            conn.commit()
        finally:
            conn.close()
        inserted = self.get_pending_action(action_id)
        if inserted is None:
            raise RuntimeError(f"pending action {action_id} vanished after insert")
        return inserted

    def get_pending_action(self, action_id: str) -> PendingAction | None:
        """Fetch a pending action by id.

        Args:
            action_id: Identifier to look up.

        Returns:
            The PendingAction, or None if not found.
        """
        conn = self._connect()
        try:
            result = conn.execute(
                "SELECT action_id, kind, payload, status, requester, resolver, "
                "expires_at, created_at, resolved_at "
                "FROM pending_actions WHERE action_id = ?",
                (action_id,),
            )
            row = result.fetchone()
        finally:
            conn.close()
        if row is None:
            return None
        return _row_to_pending_action(row)

    def mark_pending_approved(
        self, action_id: str, *, resolver: str | None = None
    ) -> bool:
        """Resolve a pending action as approved.

        Args:
            action_id: Identifier of the action to approve.
            resolver: The caller_id that approved the action, or None to
                leave any previously stamped resolver untouched.

        Returns:
            True if the action was pending and is now approved.
        """
        return self._transition(
            action_id,
            from_status=PendingActionStatus.PENDING,
            to_status=PendingActionStatus.APPROVED,
            resolver=resolver,
        )

    def mark_pending_denied(
        self, action_id: str, *, resolver: str | None = None
    ) -> bool:
        """Resolve a pending action as denied.

        Args:
            action_id: Identifier of the action to deny.
            resolver: The caller_id that denied the action, or None to
                leave any previously stamped resolver untouched.

        Returns:
            True if the action was pending and is now denied.
        """
        return self._transition(
            action_id,
            from_status=PendingActionStatus.PENDING,
            to_status=PendingActionStatus.DENIED,
            resolver=resolver,
        )

    def mark_pending_expired(self, action_id: str) -> bool:
        """Resolve a pending action as expired.

        Expiry is always system-initiated (a TTL deadline, not a human
        decision), so no resolver is ever stamped by this method.

        Args:
            action_id: Identifier of the action to expire.

        Returns:
            True if the action was pending and is now expired.
        """
        return self._transition(
            action_id,
            from_status=PendingActionStatus.PENDING,
            to_status=PendingActionStatus.EXPIRED,
        )

    def mark_pending_failed(self, action_id: str) -> bool:
        """Resolve an approved action as failed after a post-claim error.

        Guarded on ``status = 'approved'`` (not ``'pending'``): this
        transition only ever applies to a row that was already claimed
        by :meth:`mark_pending_approved` and then failed during
        execution (e.g. the Safeway submission raised or reported
        failure). The resolver already stamped by the approval is left
        untouched.

        Args:
            action_id: Identifier of the action to mark failed.

        Returns:
            True if the action was approved and is now failed.
        """
        return self._transition(
            action_id,
            from_status=PendingActionStatus.APPROVED,
            to_status=PendingActionStatus.FAILED,
        )

    def _transition(
        self,
        action_id: str,
        *,
        from_status: PendingActionStatus,
        to_status: PendingActionStatus,
        resolver: str | None = None,
    ) -> bool:
        """Transition a row from one status to another, guarded exactly once.

        The UPDATE is guarded on ``status = from_status`` so a given
        transition can only ever apply once; later attempts (unknown
        ids, or rows already in a different status) report failure
        rather than raising. When ``resolver`` is omitted (None), any
        resolver already stamped on the row is preserved rather than
        cleared, via ``COALESCE`` -- this lets system-initiated
        transitions (expiry, post-claim failure) leave the audit trail
        of who originally resolved the row intact.

        Args:
            action_id: Identifier of the action to transition.
            from_status: Required current status for the transition to
                apply.
            to_status: New status to apply.
            resolver: Caller who resolved the action, or None to leave
                any existing resolver value untouched.

        Returns:
            True if exactly one row in ``from_status`` was transitioned.
        """
        conn = self._connect()
        try:
            result = conn.execute(
                "UPDATE pending_actions SET status = ?, "
                "resolver = COALESCE(?, resolver), resolved_at = ? "
                "WHERE action_id = ? AND status = ?",
                (
                    to_status.value,
                    resolver,
                    dt.datetime.now(dt.UTC).isoformat(),
                    action_id,
                    from_status.value,
                ),
            )
            conn.commit()
            return result.rowcount == 1
        finally:
            conn.close()

    def sweep_expired(self, now: dt.datetime | None = None) -> int:
        """Resolve every past-due pending row as expired in one bulk update.

        Rows are never auto-resolved just by being read (see
        :meth:`PendingAction.is_expired`); only an explicit sweep (or a
        direct resolution) transitions them. This is system-initiated,
        so ``resolver`` is left untouched (it is already NULL for any
        row that reaches this method, since only pending rows qualify).

        Args:
            now: Reference time to compare deadlines against; defaults
                to the current UTC time.

        Returns:
            Number of rows transitioned to expired.
        """
        reference = now if now is not None else dt.datetime.now(dt.UTC)
        conn = self._connect()
        try:
            result = conn.execute(
                "UPDATE pending_actions SET status = ?, resolved_at = ? "
                "WHERE status = ? AND expires_at < ?",
                (
                    PendingActionStatus.EXPIRED.value,
                    dt.datetime.now(dt.UTC).isoformat(),
                    PendingActionStatus.PENDING.value,
                    reference.isoformat(),
                ),
            )
            conn.commit()
            return result.rowcount
        finally:
            conn.close()

    def list_pending_actions(
        self, *, limit: int, status: PendingActionStatus | None = None
    ) -> list[PendingAction]:
        """List staged/resolved actions, newest first.

        Args:
            limit: Maximum number of rows to return.
            status: If given, only rows with this status are returned;
                None (the default) returns rows regardless of status.

        Returns:
            List of PendingAction models ordered by created_at
            descending (ties broken by action_id descending for
            deterministic pagination).
        """
        conn = self._connect()
        try:
            if status is None:
                result = conn.execute(
                    "SELECT action_id, kind, payload, status, requester, resolver, "
                    "expires_at, created_at, resolved_at FROM pending_actions "
                    "ORDER BY created_at DESC, action_id DESC LIMIT ?",
                    (limit,),
                )
            else:
                result = conn.execute(
                    "SELECT action_id, kind, payload, status, requester, resolver, "
                    "expires_at, created_at, resolved_at FROM pending_actions "
                    "WHERE status = ? "
                    "ORDER BY created_at DESC, action_id DESC LIMIT ?",
                    (status.value, limit),
                )
            rows = result.fetchall()
        finally:
            conn.close()
        return [_row_to_pending_action(row) for row in rows]
