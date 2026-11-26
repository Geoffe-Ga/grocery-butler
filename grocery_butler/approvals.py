"""Approval envelopes: approvals bound to exactly what was shown.

Implements ADR 0010. An envelope stores the full payload a household member
is asked to approve, its hash, the thing it is about, and a fingerprint of
the state it was built from. Approving means echoing the hash. Executing
reads the payload from the envelope, never from the caller, and is refused
if the envelope expired, was altered, or the state under it changed. So a
changed plan can't execute under an earlier approval.

The only scope is a handoff. Approving a purchase belongs in the retailer's
own surface (ADR 0001) and has no representation here.
"""

from __future__ import annotations

import datetime as dt
import enum
import hashlib
import json
from contextlib import closing
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from grocery_butler.db import get_connection
from grocery_butler.inventory_ledger import utc_now

if TYPE_CHECKING:
    from collections.abc import Callable

    from grocery_butler.db.adapter import DatabaseConnection

DEFAULT_TTL = dt.timedelta(minutes=30)

_SELECT_ENVELOPE = (
    "SELECT id, scope, kind, subject_type, subject_id, subject_version,"
    " subject_fingerprint, payload, payload_hash, status, created_by, created_at,"
    " expires_at, approved_by, approved_at, approval_binding, resolved_by,"
    " resolved_at, resolution_note FROM approval_envelopes WHERE id = ?"
)
_SELECT_FOR_SUBJECT = (
    "SELECT id, scope, kind, subject_type, subject_id, subject_version,"
    " subject_fingerprint, payload, payload_hash, status, created_by, created_at,"
    " expires_at, approved_by, approved_at, approval_binding, resolved_by,"
    " resolved_at, resolution_note FROM approval_envelopes"
    " WHERE subject_type = ? AND subject_id = ? ORDER BY created_at, id"
)


class ApprovalScope(enum.StrEnum):
    """What an envelope can approve. Deliberately one value."""

    HANDOFF = "handoff"


class EnvelopeStatus(enum.StrEnum):
    """Where an approval envelope stands."""

    DRAFT = "draft"
    APPROVED = "approved"
    EXECUTED = "executed"
    REJECTED = "rejected"
    EXPIRED = "expired"
    STALE = "stale"


_LIVE = (EnvelopeStatus.DRAFT, EnvelopeStatus.APPROVED)


class ApprovalEnvelope(BaseModel):
    """What a member is asked to approve, and what became of it."""

    id: int
    scope: ApprovalScope
    kind: str
    subject_type: str
    subject_id: int
    subject_version: int
    subject_fingerprint: str
    payload: dict[str, Any]
    payload_hash: str
    status: EnvelopeStatus
    created_by: str
    created_at: dt.datetime
    expires_at: dt.datetime
    approved_by: str | None
    approved_at: dt.datetime | None
    approval_binding: str | None
    resolved_by: str | None
    resolved_at: dt.datetime | None
    resolution_note: str


class EnvelopeNotFoundError(LookupError):
    """Raised when an approval envelope id does not exist."""

    def __init__(self, envelope_id: int) -> None:
        """Name the missing envelope.

        Args:
            envelope_id: The id that was looked up.
        """
        super().__init__(f"No approval envelope has id {envelope_id}.")
        self.envelope_id = envelope_id


class EnvelopeNotActionableError(ValueError):
    """Raised when an envelope's status doesn't allow the requested step."""

    def __init__(self, envelope_id: int, status: EnvelopeStatus, step: str) -> None:
        """Say what state the envelope is in and what to do.

        Args:
            envelope_id: The envelope.
            status: Its current status.
            step: The step that was attempted, e.g. ``approved``.
        """
        super().__init__(
            f"Approval envelope {envelope_id} is {status.value} and can't be "
            f"{step}. Create a new draft and show it to the member."
        )
        self.envelope_id = envelope_id
        self.status = status


class PayloadMismatchError(ValueError):
    """Raised when the hash sent with an approval isn't the envelope's."""

    def __init__(self, envelope_id: int) -> None:
        """Explain that the approver saw something else.

        Args:
            envelope_id: The envelope.
        """
        super().__init__(
            f"The payload hash sent does not match approval envelope "
            f"{envelope_id}. Fetch the envelope and show the member its "
            "current content before asking again."
        )
        self.envelope_id = envelope_id


class StaleEnvelopeError(ValueError):
    """Raised when the state an envelope was built from has changed."""

    def __init__(self, envelope_id: int) -> None:
        """Explain that a new draft is needed.

        Args:
            envelope_id: The envelope.
        """
        super().__init__(
            f"What approval envelope {envelope_id} was built from has changed "
            "since it was drafted. Create a new draft and show it to the member."
        )
        self.envelope_id = envelope_id


class EnvelopeTamperedError(RuntimeError):
    """Raised when a stored envelope no longer matches its own binding."""

    def __init__(self, envelope_id: int) -> None:
        """Refuse to act on the envelope.

        Args:
            envelope_id: The envelope.
        """
        super().__init__(
            f"Approval envelope {envelope_id} was altered after it was approved "
            "and can't be executed."
        )
        self.envelope_id = envelope_id


def canonical_json(payload: dict[str, Any]) -> str:
    """Serialize a payload so equal content always gives equal text.

    Args:
        payload: A JSON-serializable object.

    Returns:
        JSON with sorted keys and no optional whitespace.
    """
    return json.dumps(payload, sort_keys=True, separators=(",", ":"))


def payload_hash(payload: dict[str, Any]) -> str:
    """Hash a payload's canonical JSON.

    Args:
        payload: A JSON-serializable object.

    Returns:
        The hex SHA-256 digest a client echoes to approve.
    """
    return hashlib.sha256(canonical_json(payload).encode()).hexdigest()


def _binding(envelope_id: int, digest: str, actor: str, approved_at: str) -> str:
    """Bind an approval to its envelope, payload, actor and moment.

    Args:
        envelope_id: The envelope.
        digest: The payload hash.
        actor: Who approved.
        approved_at: When, ISO 8601.

    Returns:
        A hex SHA-256 digest over the four.
    """
    material = "\n".join([str(envelope_id), digest, actor, approved_at])
    return hashlib.sha256(material.encode()).hexdigest()


def _when(value: str | None) -> dt.datetime | None:
    """Parse a stored ISO 8601 time.

    Args:
        value: The stored text, or None.

    Returns:
        The time, or None.
    """
    return dt.datetime.fromisoformat(value) if value else None


def _row_to_envelope(row: Any) -> ApprovalEnvelope:
    """Build an ApprovalEnvelope from a database row.

    Args:
        row: A row from one of the envelope SELECT statements.

    Returns:
        The envelope.
    """
    return ApprovalEnvelope(
        id=row["id"],
        scope=ApprovalScope(row["scope"]),
        kind=row["kind"],
        subject_type=row["subject_type"],
        subject_id=row["subject_id"],
        subject_version=row["subject_version"],
        subject_fingerprint=row["subject_fingerprint"],
        payload=json.loads(row["payload"]),
        payload_hash=row["payload_hash"],
        status=EnvelopeStatus(row["status"]),
        created_by=row["created_by"],
        created_at=dt.datetime.fromisoformat(row["created_at"]),
        expires_at=dt.datetime.fromisoformat(row["expires_at"]),
        approved_by=row["approved_by"],
        approved_at=_when(row["approved_at"]),
        approval_binding=row["approval_binding"],
        resolved_by=row["resolved_by"],
        resolved_at=_when(row["resolved_at"]),
        resolution_note=row["resolution_note"],
    )


class ApprovalEnvelopes:
    """Draft, approve, execute and reject approval envelopes.

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

    # -- reads ---------------------------------------------------------

    def get(self, envelope_id: int) -> ApprovalEnvelope | None:
        """Return one envelope. A live one past its deadline reads as expired.

        Args:
            envelope_id: The envelope's id.

        Returns:
            The envelope, or None if there is none.
        """
        self._expire_overdue()
        with closing(get_connection(self._db_path)) as conn:
            row = conn.execute(_SELECT_ENVELOPE, (envelope_id,)).fetchone()
        return _row_to_envelope(row) if row else None

    def list_for_subject(
        self, subject_type: str, subject_id: int
    ) -> list[ApprovalEnvelope]:
        """List the envelopes drafted for one subject, oldest first.

        Args:
            subject_type: The kind of thing, e.g. ``shopping_plan``.
            subject_id: Its id.

        Returns:
            The subject's envelopes.
        """
        self._expire_overdue()
        with closing(get_connection(self._db_path)) as conn:
            rows = conn.execute(
                _SELECT_FOR_SUBJECT, (subject_type, subject_id)
            ).fetchall()
        return [_row_to_envelope(row) for row in rows]

    # -- writes --------------------------------------------------------

    def draft(
        self,
        *,
        kind: str,
        payload: dict[str, Any],
        subject_type: str,
        subject_id: int,
        subject_version: int,
        subject_fingerprint: str,
        created_by: str,
        ttl: dt.timedelta = DEFAULT_TTL,
    ) -> ApprovalEnvelope:
        """Store exactly what a member will be asked to approve.

        Args:
            kind: What the approval is for, e.g. ``plan_handoff``.
            payload: The full content to approve. Stored as canonical JSON.
            subject_type: The kind of thing the payload is about.
            subject_id: Its id.
            subject_version: Its version when the payload was built.
            subject_fingerprint: A digest of the state the payload was
                built from. The same digest must be supplied to approve
                and to execute.
            created_by: Who or what drafted it.
            ttl: How long the envelope stays actionable.

        Returns:
            The stored draft.

        Raises:
            ValueError: If the deadline would not be in the future.
        """
        if ttl <= dt.timedelta(0):
            msg = "An approval envelope's deadline must be in the future."
            raise ValueError(msg)
        now = self._clock()
        with closing(get_connection(self._db_path)) as conn:
            cursor = conn.execute(
                "INSERT INTO approval_envelopes"
                " (scope, kind, subject_type, subject_id, subject_version,"
                "  subject_fingerprint, payload, payload_hash, status, created_by,"
                "  created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ApprovalScope.HANDOFF.value,
                    kind,
                    subject_type,
                    subject_id,
                    subject_version,
                    subject_fingerprint,
                    canonical_json(payload),
                    payload_hash(payload),
                    EnvelopeStatus.DRAFT.value,
                    created_by,
                    now.isoformat(),
                    (now + ttl).isoformat(),
                ),
            )
            conn.commit()
            return self._require(conn, cursor.lastrowid)

    def approve(
        self,
        envelope_id: int,
        *,
        payload_hash: str,
        actor: str,
        current_fingerprint: str,
    ) -> ApprovalEnvelope:
        """Record a member's approval of exactly the stored payload.

        Args:
            envelope_id: The draft to approve.
            payload_hash: The hash of the content the member was shown.
            actor: The approving member.
            current_fingerprint: The subject's fingerprint now.

        Returns:
            The approved envelope.

        Raises:
            EnvelopeNotFoundError: If the envelope does not exist.
            EnvelopeNotActionableError: If it is not a live draft.
            PayloadMismatchError: If the hash is not the envelope's.
            StaleEnvelopeError: If the subject's state has changed.
        """
        envelope = self._require_status(envelope_id, EnvelopeStatus.DRAFT, "approved")
        if payload_hash != envelope.payload_hash:
            raise PayloadMismatchError(envelope_id)
        self._refuse_if_stale(envelope, current_fingerprint, actor)
        approved_at = self._clock().isoformat()
        binding = _binding(envelope_id, envelope.payload_hash, actor, approved_at)
        with closing(get_connection(self._db_path)) as conn:
            claimed = conn.execute(
                "UPDATE approval_envelopes SET status = ?, approved_by = ?,"
                " approved_at = ?, approval_binding = ? WHERE id = ? AND status = ?",
                (
                    EnvelopeStatus.APPROVED.value,
                    actor,
                    approved_at,
                    binding,
                    envelope_id,
                    EnvelopeStatus.DRAFT.value,
                ),
            )
            conn.commit()
            current = self._require(conn, envelope_id)
        if not claimed.rowcount:
            raise EnvelopeNotActionableError(envelope_id, current.status, "approved")
        return current

    def execute(
        self, envelope_id: int, *, actor: str, current_fingerprint: str
    ) -> ApprovalEnvelope:
        """Claim an approved envelope for execution, exactly once.

        The caller then acts on the returned envelope's ``payload`` and on
        nothing else.

        Args:
            envelope_id: The approved envelope.
            actor: Who or what is executing it.
            current_fingerprint: The subject's fingerprint now.

        Returns:
            The executed envelope, carrying the payload to act on.

        Raises:
            EnvelopeNotFoundError: If the envelope does not exist.
            EnvelopeNotActionableError: If it is not a live approval, or
                another caller executed it first.
            EnvelopeTamperedError: If the stored row no longer matches its
                approval binding.
            StaleEnvelopeError: If the subject's state has changed.
        """
        envelope = self._require_status(
            envelope_id, EnvelopeStatus.APPROVED, "executed"
        )
        self._refuse_if_altered(envelope)
        self._refuse_if_stale(envelope, current_fingerprint, actor)
        with closing(get_connection(self._db_path)) as conn:
            claimed = conn.execute(
                "UPDATE approval_envelopes SET status = ?, resolved_by = ?,"
                " resolved_at = ? WHERE id = ? AND status = ?",
                (
                    EnvelopeStatus.EXECUTED.value,
                    actor,
                    self._clock().isoformat(),
                    envelope_id,
                    EnvelopeStatus.APPROVED.value,
                ),
            )
            conn.commit()
            current = self._require(conn, envelope_id)
        if not claimed.rowcount:
            raise EnvelopeNotActionableError(envelope_id, current.status, "executed")
        return current

    def reject(
        self, envelope_id: int, *, actor: str, note: str = ""
    ) -> ApprovalEnvelope:
        """Turn down a draft, or withdraw an approval before it executes.

        Args:
            envelope_id: The envelope.
            actor: The member rejecting it.
            note: Why, if known.

        Returns:
            The rejected envelope.

        Raises:
            EnvelopeNotFoundError: If the envelope does not exist.
            EnvelopeNotActionableError: If it is no longer live.
        """
        envelope = self.get(envelope_id)
        if envelope is None:
            raise EnvelopeNotFoundError(envelope_id)
        if envelope.status not in _LIVE:
            raise EnvelopeNotActionableError(envelope_id, envelope.status, "rejected")
        closed = self._close(envelope_id, EnvelopeStatus.REJECTED, actor, note)
        if closed.status is not EnvelopeStatus.REJECTED:
            # Another caller executed or closed it between the read and the
            # conditional update, which then matched nothing.
            raise EnvelopeNotActionableError(envelope_id, closed.status, "rejected")
        return closed

    # -- internals -----------------------------------------------------

    def _refuse_if_stale(
        self, envelope: ApprovalEnvelope, current_fingerprint: str, actor: str
    ) -> None:
        """Mark an envelope stale and refuse it if its subject changed.

        Args:
            envelope: The live envelope.
            current_fingerprint: The subject's fingerprint now.
            actor: Who was acting when the change was noticed.

        Raises:
            StaleEnvelopeError: If the fingerprint differs from the one
                stored at draft time.
        """
        if current_fingerprint == envelope.subject_fingerprint:
            return
        self._close(
            envelope.id,
            EnvelopeStatus.STALE,
            actor,
            "The state this was built from changed.",
        )
        raise StaleEnvelopeError(envelope.id)

    @staticmethod
    def _refuse_if_altered(envelope: ApprovalEnvelope) -> None:
        """Check a stored approval against its own hash and binding.

        Args:
            envelope: The approved envelope as read from the database.

        Raises:
            EnvelopeTamperedError: If the payload no longer hashes to the
                stored hash, or the binding no longer matches the approval.
        """
        approved_at = envelope.approved_at.isoformat() if envelope.approved_at else ""
        expected = _binding(
            envelope.id, envelope.payload_hash, envelope.approved_by or "", approved_at
        )
        if (
            payload_hash(envelope.payload) != envelope.payload_hash
            or envelope.approval_binding != expected
        ):
            raise EnvelopeTamperedError(envelope.id)

    def _close(
        self, envelope_id: int, status: EnvelopeStatus, actor: str, note: str
    ) -> ApprovalEnvelope:
        """Move a live envelope to a closed status.

        Args:
            envelope_id: The envelope.
            status: The closed status.
            actor: Who closed it.
            note: Why.

        Returns:
            The envelope after the change.
        """
        with closing(get_connection(self._db_path)) as conn:
            conn.execute(
                "UPDATE approval_envelopes SET status = ?, resolved_by = ?,"
                " resolved_at = ?, resolution_note = ?"
                " WHERE id = ? AND status IN (?, ?)",
                (
                    status.value,
                    actor,
                    self._clock().isoformat(),
                    note,
                    envelope_id,
                    EnvelopeStatus.DRAFT.value,
                    EnvelopeStatus.APPROVED.value,
                ),
            )
            conn.commit()
            return self._require(conn, envelope_id)

    def _expire_overdue(self) -> None:
        """Mark live envelopes past their deadline as expired."""
        with closing(get_connection(self._db_path)) as conn:
            conn.execute(
                "UPDATE approval_envelopes SET status = ?"
                " WHERE status IN (?, ?) AND expires_at < ?",
                (
                    EnvelopeStatus.EXPIRED.value,
                    EnvelopeStatus.DRAFT.value,
                    EnvelopeStatus.APPROVED.value,
                    self._clock().isoformat(),
                ),
            )
            conn.commit()

    def _require_status(
        self, envelope_id: int, wanted: EnvelopeStatus, step: str
    ) -> ApprovalEnvelope:
        """Fetch an envelope that is in the status a step needs.

        Args:
            envelope_id: The envelope's id.
            wanted: The status the step requires.
            step: The step being attempted, for the error message.

        Returns:
            The envelope.

        Raises:
            EnvelopeNotFoundError: If the envelope does not exist.
            EnvelopeNotActionableError: If it is in any other status.
        """
        envelope = self.get(envelope_id)
        if envelope is None:
            raise EnvelopeNotFoundError(envelope_id)
        if envelope.status is not wanted:
            raise EnvelopeNotActionableError(envelope_id, envelope.status, step)
        return envelope

    def _require(
        self, conn: DatabaseConnection, envelope_id: int | None
    ) -> ApprovalEnvelope:
        """Fetch one envelope on an open connection, or raise.

        Args:
            conn: Open connection.
            envelope_id: The envelope's id.

        Returns:
            The envelope.

        Raises:
            EnvelopeNotFoundError: If the envelope does not exist.
        """
        row = conn.execute(_SELECT_ENVELOPE, (envelope_id,)).fetchone()
        if row is None:
            raise EnvelopeNotFoundError(envelope_id or 0)
        return _row_to_envelope(row)
