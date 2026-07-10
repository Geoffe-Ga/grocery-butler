"""Data access layer for staged pending actions.

The ``pending_actions`` table is the server-side staging area and audit
log for destructive operations (Safeway order submissions, brand and
preference changes). Rows are inserted as ``pending`` and resolved to
``approved``, ``denied``, or ``expired`` exactly once.

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
                "SELECT action_id, kind, payload, status, requester, "
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

    def mark_pending_approved(self, action_id: str) -> bool:
        """Resolve a pending action as approved.

        Args:
            action_id: Identifier of the action to approve.

        Returns:
            True if the action was pending and is now approved.
        """
        return self._resolve(action_id, PendingActionStatus.APPROVED)

    def mark_pending_denied(self, action_id: str) -> bool:
        """Resolve a pending action as denied.

        Args:
            action_id: Identifier of the action to deny.

        Returns:
            True if the action was pending and is now denied.
        """
        return self._resolve(action_id, PendingActionStatus.DENIED)

    def mark_pending_expired(self, action_id: str) -> bool:
        """Resolve a pending action as expired.

        Args:
            action_id: Identifier of the action to expire.

        Returns:
            True if the action was pending and is now expired.
        """
        return self._resolve(action_id, PendingActionStatus.EXPIRED)

    def _resolve(self, action_id: str, new_status: PendingActionStatus) -> bool:
        """Transition an action from pending to a terminal status.

        The UPDATE is guarded on ``status = 'pending'`` so an action can
        only be resolved once; later attempts (or unknown ids) fail.

        Args:
            action_id: Identifier of the action to resolve.
            new_status: Terminal status to apply.

        Returns:
            True if exactly one pending row was transitioned.
        """
        conn = self._connect()
        try:
            result = conn.execute(
                "UPDATE pending_actions SET status = ?, resolved_at = ? "
                "WHERE action_id = ? AND status = ?",
                (
                    new_status.value,
                    dt.datetime.now(dt.UTC).isoformat(),
                    action_id,
                    PendingActionStatus.PENDING.value,
                ),
            )
            conn.commit()
            return result.rowcount == 1
        finally:
            conn.close()
