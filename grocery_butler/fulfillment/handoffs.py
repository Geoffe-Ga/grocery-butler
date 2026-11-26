"""Handing an approved plan to an outside service, exactly once (ADR 0016).

A handoff to a service outside the household goes through three steps:

1. **Draft.** The exact content to approve is stored in an approval
   envelope (ADR 0010): the adapter, the plan version and its hash, the
   lines, every hard constraint the adapter can't enforce, and everything
   its translation leaves out.
2. **Approve.** A household member echoes the hash of what they were
   shown. That is also their acknowledgement of the unenforced constraints.
3. **Execute.** The envelope is claimed once, the adapter is called, and a
   fulfillment attempt records what happened.

An attempt is keyed by the request's idempotency key. Repeating a request
returns its attempt and sends nothing, unless the attempt failed with a
passing fault, in which case the same key tries again.

A handoff is not an order. Nothing here changes a need or the inventory.
"""

from __future__ import annotations

import datetime as dt
import logging
import time
from contextlib import closing, suppress
from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from grocery_butler.approvals import (
    ApprovalEnvelopes,
    EnvelopeNotActionableError,
    EnvelopeNotFoundError,
    StaleEnvelopeError,
)
from grocery_butler.db import get_connection
from grocery_butler.db.adapter import IntegrityError
from grocery_butler.fulfillment.base import (
    Authorization,
    HandoffStatus,
    unenforced_constraints,
)
from grocery_butler.fulfillment.runner import create_handoff
from grocery_butler.inventory_ledger import utc_now
from grocery_butler.observability import audit
from grocery_butler.retries import RetriesExhaustedError
from grocery_butler.shopping_plans import PlanNotFoundError, ShoppingPlans

if TYPE_CHECKING:
    from collections.abc import Callable

    from grocery_butler.approvals import ApprovalEnvelope
    from grocery_butler.db.adapter import DatabaseConnection
    from grocery_butler.fulfillment.base import FulfillmentAdapter, HandoffResult
    from grocery_butler.models import ShoppingPlan

logger = logging.getLogger(__name__)

HANDOFF_KIND = "plan_handoff"
SUBJECT_TYPE = "shopping_plan"

PROVIDER_UNAVAILABLE = "provider_unavailable"
ADAPTER_ERROR = "adapter_error"
NO_LINK = "invalid_response"

_INTEGRITY_ERRORS = (
    IntegrityError if isinstance(IntegrityError, tuple) else (IntegrityError,)
)
_SELECT_BY_ID = (
    "SELECT id, envelope_id, adapter, plan_version, plan_content_hash,"
    " idempotency_key, status, external_url, link_expires_at, error_code,"
    " retryable, human_action, created_by, created_at, updated_at"
    " FROM fulfillment_attempts WHERE id = ?"
)
_SELECT_BY_KEY = (
    "SELECT id, envelope_id, adapter, plan_version, plan_content_hash,"
    " idempotency_key, status, external_url, link_expires_at, error_code,"
    " retryable, human_action, created_by, created_at, updated_at"
    " FROM fulfillment_attempts WHERE idempotency_key = ?"
)
_SELECT_FOR_PLAN = (
    "SELECT id, envelope_id, adapter, plan_version, plan_content_hash,"
    " idempotency_key, status, external_url, link_expires_at, error_code,"
    " retryable, human_action, created_by, created_at, updated_at"
    " FROM fulfillment_attempts WHERE plan_version = ? ORDER BY id"
)
_INSERT = (
    "INSERT INTO fulfillment_attempts (envelope_id, adapter, plan_version,"
    " plan_content_hash, idempotency_key, status, created_by, created_at,"
    " updated_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)"
)
_DELETE_PENDING = "DELETE FROM fulfillment_attempts WHERE id = ? AND status = ?"
_RECLAIM = (
    "UPDATE fulfillment_attempts SET status = ?, updated_at = ?"
    " WHERE id = ? AND status = ? AND retryable = ?"
)
_FINISH = (
    "UPDATE fulfillment_attempts SET status = ?, external_url = ?,"
    " link_expires_at = ?, error_code = ?, retryable = ?, human_action = ?,"
    " updated_at = ? WHERE id = ?"
)


class AttemptStatus(StrEnum):
    """What became of a handoff attempt. There is no "ordered"."""

    PENDING = "pending"
    CREATED = "created"
    FAILED = "failed"
    EXPIRED = "expired"


class HandoffAttempt(BaseModel):
    """One try at handing an approved plan to an adapter.

    ``status`` is ``expired`` once a created link's expiry has passed; that
    is worked out on reading, not stored.
    """

    id: int
    envelope_id: int
    adapter: str
    plan_version: int
    plan_content_hash: str
    idempotency_key: str
    status: AttemptStatus
    external_url: str | None
    link_expires_at: dt.datetime | None
    error_code: str | None
    retryable: bool
    human_action: str
    created_by: str
    created_at: dt.datetime
    updated_at: dt.datetime


class HandoffNotAvailableError(ValueError):
    """Raised when a plan can't be handed off the way that was asked."""


class PlanNotCurrentError(ValueError):
    """Raised when a handoff is drafted for a plan that has gone stale."""

    def __init__(self, version: int) -> None:
        """Name the stale version.

        Args:
            version: The plan version that was asked for.
        """
        self.version = version
        super().__init__(
            f"Shopping plan {version} no longer matches the household's needs."
            " Build a new plan and hand that one off."
        )


class AttemptNotFoundError(LookupError):
    """Raised when a fulfillment attempt id does not exist."""

    def __init__(self, attempt_id: int) -> None:
        """Name the missing attempt.

        Args:
            attempt_id: The id that was looked up.
        """
        self.attempt_id = attempt_id
        super().__init__(f"No handoff attempt has id {attempt_id}.")


class KeyBelongsToAnotherHandoffError(ValueError):
    """Raised when an idempotency key is reused for a different approval."""

    def __init__(self, envelope_id: int) -> None:
        """Name the approval the key was reused for.

        Args:
            envelope_id: The envelope the repeated request named.
        """
        self.envelope_id = envelope_id
        super().__init__(
            "This idempotency key already belongs to a handoff of a different"
            f" approval, not approval {envelope_id}. Use a new key."
        )


def _needs_no_approval(adapter: str) -> HandoffNotAvailableError:
    """Build the error for an adapter that sends nothing outside.

    Args:
        adapter: The adapter's name.

    Returns:
        The error to raise.
    """
    return HandoffNotAvailableError(
        f'The "{adapter}" adapter sends nothing outside the household, so it'
        " needs no approval. Use the plan's export instead."
    )


def _not_a_handoff(envelope_id: int) -> HandoffNotAvailableError:
    """Build the error for an envelope that is not a plan handoff.

    Args:
        envelope_id: The envelope's id.

    Returns:
        The error to raise.
    """
    return HandoffNotAvailableError(
        f"Approval {envelope_id} is not a shopping plan handoff."
    )


def _when(value: str | None) -> dt.datetime | None:
    """Parse a stored timestamp.

    Args:
        value: ISO 8601 text, or None.

    Returns:
        The time, or None.
    """
    return dt.datetime.fromisoformat(value) if value else None


def _amount_text(amounts: list[Any]) -> list[str]:
    """Word a line's amounts for a person.

    Args:
        amounts: The line's plan amounts.

    Returns:
        For example ``["2 lb"]``.
    """
    return [f"{amount.quantity:g} {amount.unit.value}" for amount in amounts]


@dataclass(frozen=True)
class _Outcome:
    """What one call to an adapter came to, ready to record."""

    error: str | None
    retryable: bool
    url: str | None = None
    expires: dt.datetime | None = None
    action: str = ""

    @property
    def status(self) -> AttemptStatus:
        """Say whether the attempt created a handoff.

        Returns:
            ``created`` when there is no error, otherwise ``failed``.
        """
        return AttemptStatus.CREATED if self.error is None else AttemptStatus.FAILED


def _classify(result: HandoffResult) -> _Outcome:
    """Read an adapter's result as an outcome.

    A result counts as created only if it says so and carries a link.

    Args:
        result: What the adapter returned.

    Returns:
        The outcome to record.
    """
    action = result.human_action
    if result.status is not HandoffStatus.CREATED:
        return _Outcome(result.detail or ADAPTER_ERROR, retryable=False, action=action)
    if result.external_url is None:
        return _Outcome(NO_LINK, retryable=False, action=action)
    return _Outcome(
        None,
        retryable=False,
        url=result.external_url,
        expires=result.expires_at,
        action=action,
    )


class Handoffs:
    """Drafts, approves and executes handoffs of shopping plans.

    Args:
        db_path: Database file path or PostgreSQL URL.
        adapter_for: Returns the adapter registered under a name, or raises
            ``LookupError``. Passed in so this module never constructs an
            adapter itself.
        clock: Returns the current UTC time; injectable for tests.
        sleep: Waits between provider retries; tests pass a recorder.
    """

    def __init__(
        self,
        db_path: str,
        adapter_for: Callable[[str], FulfillmentAdapter],
        clock: Callable[[], dt.datetime] = utc_now,
        sleep: Callable[[float], None] = time.sleep,
    ) -> None:
        """Bind to a database and a source of adapters.

        Args:
            db_path: Database file path or PostgreSQL URL.
            adapter_for: Looks an adapter up by name.
            clock: Returns the current UTC time.
            sleep: Waits between provider retries.
        """
        self._db_path = db_path
        self._adapter_for = adapter_for
        self._clock = clock
        self._sleep = sleep
        self._plans = ShoppingPlans(db_path, clock=clock)
        self._envelopes = ApprovalEnvelopes(db_path, clock=clock)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _to_attempt(self, row: Any) -> HandoffAttempt:
        """Build an attempt from its row, working out expiry.

        Args:
            row: A ``fulfillment_attempts`` row.

        Returns:
            The attempt.
        """
        status = AttemptStatus(row["status"])
        expires = _when(row["link_expires_at"])
        if (
            status is AttemptStatus.CREATED
            and expires is not None
            and expires <= self._clock()
        ):
            status = AttemptStatus.EXPIRED
        return HandoffAttempt(
            id=row["id"],
            envelope_id=row["envelope_id"],
            adapter=row["adapter"],
            plan_version=row["plan_version"],
            plan_content_hash=row["plan_content_hash"],
            idempotency_key=row["idempotency_key"],
            status=status,
            external_url=row["external_url"],
            link_expires_at=expires,
            error_code=row["error_code"],
            retryable=bool(row["retryable"]),
            human_action=row["human_action"],
            created_by=row["created_by"],
            created_at=dt.datetime.fromisoformat(row["created_at"]),
            updated_at=dt.datetime.fromisoformat(row["updated_at"]),
        )

    def _one(
        self, conn: DatabaseConnection, sql: str, value: Any
    ) -> HandoffAttempt | None:
        """Read one attempt.

        Args:
            conn: An open connection.
            sql: A select taking one parameter.
            value: The parameter.

        Returns:
            The attempt, or None.
        """
        row = conn.execute(sql, (value,)).fetchone()
        return self._to_attempt(row) if row is not None else None

    def get_attempt(self, attempt_id: int) -> HandoffAttempt | None:
        """Return one attempt.

        Args:
            attempt_id: The attempt's id.

        Returns:
            The attempt, or None if there is no such id.
        """
        with closing(get_connection(self._db_path)) as conn:
            return self._one(conn, _SELECT_BY_ID, attempt_id)

    def attempts_for_plan(self, version: int) -> list[HandoffAttempt]:
        """List the attempts made for a plan version, oldest first.

        Args:
            version: The plan version.

        Returns:
            The attempts.
        """
        with closing(get_connection(self._db_path)) as conn:
            rows = conn.execute(_SELECT_FOR_PLAN, (version,)).fetchall()
        return [self._to_attempt(row) for row in rows]

    def approval(self, envelope_id: int) -> ApprovalEnvelope:
        """Return a handoff's approval envelope.

        Args:
            envelope_id: The envelope's id.

        Returns:
            The envelope.

        Raises:
            EnvelopeNotFoundError: If there is no such envelope.
            HandoffNotAvailableError: If it approves something else.
        """
        return self._handoff_envelope(envelope_id)

    def _handoff_envelope(self, envelope_id: int) -> ApprovalEnvelope:
        """Fetch an envelope and check it is a plan handoff.

        Args:
            envelope_id: The envelope's id.

        Returns:
            The envelope.

        Raises:
            EnvelopeNotFoundError: If there is no such envelope.
            HandoffNotAvailableError: If it approves something else.
        """
        envelope = self._envelopes.get(envelope_id)
        if envelope is None:
            raise EnvelopeNotFoundError(envelope_id)
        if envelope.kind != HANDOFF_KIND or envelope.subject_type != SUBJECT_TYPE:
            raise _not_a_handoff(envelope_id)
        return envelope

    # ------------------------------------------------------------------
    # Draft, approve, reject
    # ------------------------------------------------------------------

    def _payload(
        self, plan: ShoppingPlan, name: str, adapter: FulfillmentAdapter
    ) -> dict[str, Any]:
        """Build exactly what a member is asked to approve.

        Args:
            plan: The plan version.
            name: The adapter's registered name.
            adapter: The adapter.

        Returns:
            The payload: the adapter, the plan, its lines, and everything
            the handoff won't enforce or carry.
        """
        description = adapter.describe()
        unenforced = [
            {
                "item_id": entry.item_id,
                "display_name": entry.display_name,
                "kind": entry.kind.value,
                "value": entry.value,
            }
            for entry in unenforced_constraints(description, plan)
        ]
        return {
            "adapter": name,
            "adapter_display_name": description.display_name,
            "plan_version": plan.version,
            "plan_content_hash": plan.content_hash,
            "lines": [
                {
                    "item_id": line.item_id,
                    "display_name": line.display_name,
                    "amounts": _amount_text(line.amounts),
                }
                for line in plan.lines
            ],
            "unenforced_constraints": unenforced,
            "issues": list(adapter.preview_issues(plan)),
            "approving_acknowledges_unenforced_constraints": bool(unenforced),
        }

    def draft(
        self, plan_version: int, adapter_name: str, *, actor: str
    ) -> ApprovalEnvelope:
        """Draft a handoff of a plan version for a member to approve.

        Nothing is sent. The returned envelope holds the content to show
        the member and the hash they must echo to approve it.

        Args:
            plan_version: The plan version to hand off.
            adapter_name: The adapter to hand it to.
            actor: Who is drafting.

        Returns:
            The draft envelope.

        Raises:
            PlanNotFoundError: If there is no such plan version.
            LookupError: If no adapter has that name.
            HandoffNotAvailableError: If the adapter sends nothing outside
                the household, or the plan has no lines.
            PlanNotCurrentError: If the needs under the plan have changed.
        """
        plan = self._plans.get(plan_version)
        if plan is None:
            raise PlanNotFoundError(plan_version)
        adapter = self._adapter_for(adapter_name)
        if adapter.describe().authorization is Authorization.NONE:
            raise _needs_no_approval(adapter_name)
        if not plan.lines:
            msg = f"Shopping plan {plan_version} has no lines to hand off."
            raise HandoffNotAvailableError(msg)
        if self._plans.current_fingerprint(plan_version) != plan.content_hash:
            raise PlanNotCurrentError(plan_version)
        envelope = self._envelopes.draft(
            kind=HANDOFF_KIND,
            payload=self._payload(plan, adapter_name, adapter),
            subject_type=SUBJECT_TYPE,
            subject_id=plan.id,
            subject_version=plan.version,
            subject_fingerprint=plan.content_hash,
            created_by=actor,
        )
        audit(
            "handoff.drafted",
            envelope_id=envelope.id,
            adapter=adapter_name,
            plan_version=plan.version,
            actor=actor,
        )
        return envelope

    def approve(
        self, envelope_id: int, shown_hash: str, *, actor: str
    ) -> ApprovalEnvelope:
        """Approve a drafted handoff by echoing the hash of what was shown.

        Args:
            envelope_id: The draft.
            shown_hash: The hash of the content the member was shown.
            actor: The approving member.

        Returns:
            The approved envelope.

        Raises:
            EnvelopeNotFoundError: If there is no such envelope.
            HandoffNotAvailableError: If it is not a plan handoff.
            EnvelopeNotActionableError: If it is not a live draft.
            PayloadMismatchError: If the hash is not the envelope's.
            StaleEnvelopeError: If the plan's needs have changed.
        """
        envelope = self._handoff_envelope(envelope_id)
        approved = self._envelopes.approve(
            envelope_id,
            shown_hash=shown_hash,
            actor=actor,
            current_fingerprint=self._plans.current_fingerprint(
                envelope.subject_version
            ),
        )
        audit("handoff.approved", envelope_id=envelope_id, actor=actor)
        return approved

    def reject(
        self, envelope_id: int, *, actor: str, note: str = ""
    ) -> ApprovalEnvelope:
        """Turn a drafted or approved handoff down. Nothing is sent.

        Args:
            envelope_id: The envelope.
            actor: The member rejecting it.
            note: Why, if known.

        Returns:
            The rejected envelope.

        Raises:
            EnvelopeNotFoundError: If there is no such envelope.
            HandoffNotAvailableError: If it is not a plan handoff.
            EnvelopeNotActionableError: If it is already closed.
        """
        self._handoff_envelope(envelope_id)
        rejected = self._envelopes.reject(envelope_id, actor=actor, note=note)
        audit("handoff.rejected", envelope_id=envelope_id, actor=actor)
        return rejected

    # ------------------------------------------------------------------
    # Execute
    # ------------------------------------------------------------------

    def _claim_new(
        self, envelope: ApprovalEnvelope, key: str, actor: str
    ) -> tuple[HandoffAttempt, bool]:
        """Record a pending attempt under an idempotency key.

        Args:
            envelope: The handoff's envelope.
            key: The request's idempotency key.
            actor: Who is executing.

        Returns:
            The attempt, and whether this call created it. False means
            another request holding the same key got there first.
        """
        now = self._clock().isoformat()
        payload = envelope.payload
        with closing(get_connection(self._db_path)) as conn:
            try:
                attempt_id = conn.execute(
                    _INSERT,
                    (
                        envelope.id,
                        payload["adapter"],
                        payload["plan_version"],
                        payload["plan_content_hash"],
                        key,
                        AttemptStatus.PENDING.value,
                        actor,
                        now,
                        now,
                    ),
                ).lastrowid
                conn.commit()
            except _INTEGRITY_ERRORS:
                existing = self._existing(key, envelope.id)
                return existing, False
            created = self._one(conn, _SELECT_BY_ID, attempt_id)
        if created is None:  # pragma: no cover - the row was just inserted
            raise AttemptNotFoundError(attempt_id or 0)
        return created, True

    def _existing(self, key: str, envelope_id: int) -> HandoffAttempt:
        """Read the attempt that already holds an idempotency key.

        Args:
            key: The idempotency key.
            envelope_id: The envelope the current request names.

        Returns:
            The attempt.

        Raises:
            KeyBelongsToAnotherHandoffError: If the key's attempt is for a
                different envelope.
        """
        with closing(get_connection(self._db_path)) as conn:
            attempt = self._one(conn, _SELECT_BY_KEY, key)
        if attempt is None or attempt.envelope_id != envelope_id:
            raise KeyBelongsToAnotherHandoffError(envelope_id)
        return attempt

    def _reclaim(self, attempt: HandoffAttempt) -> bool:
        """Take a failed, retryable attempt back to pending, once.

        Args:
            attempt: The failed attempt.

        Returns:
            True if this call took it; False if another request did.
        """
        with closing(get_connection(self._db_path)) as conn:
            claimed = conn.execute(
                _RECLAIM,
                (
                    AttemptStatus.PENDING.value,
                    self._clock().isoformat(),
                    attempt.id,
                    AttemptStatus.FAILED.value,
                    True,
                ),
            )
            conn.commit()
        return bool(claimed.rowcount)

    def _discard_pending(self, attempt_id: int) -> None:
        """Remove a pending attempt whose approval turned out unusable.

        Args:
            attempt_id: The pending attempt.
        """
        with closing(get_connection(self._db_path)) as conn:
            conn.execute(_DELETE_PENDING, (attempt_id, AttemptStatus.PENDING.value))
            conn.commit()

    def _refuse_stale_retry(self, envelope: ApprovalEnvelope) -> None:
        """Refuse to retry under an approval that no longer holds.

        Args:
            envelope: The already executed envelope.

        Raises:
            EnvelopeNotActionableError: If the approval's time has run out.
            StaleEnvelopeError: If the plan's needs have changed since.
        """
        if envelope.expires_at <= self._clock():
            raise EnvelopeNotActionableError(envelope.id, envelope.status, "retried")
        current = self._plans.current_fingerprint(envelope.subject_version)
        if current != envelope.subject_fingerprint:
            raise StaleEnvelopeError(envelope.id)

    def _outcome(self, adapter_name: str, plan: ShoppingPlan) -> _Outcome:
        """Call the adapter and classify what happened.

        Args:
            adapter_name: The adapter's registered name.
            plan: The plan version to hand off.

        Returns:
            What to record on the attempt.
        """
        try:
            adapter = self._adapter_for(adapter_name)
            # The member approved a payload listing every unenforced
            # constraint; that approval is the acknowledgement.
            result = create_handoff(
                adapter, plan, constraints_acknowledged=True, sleep=self._sleep
            )
        except RetriesExhaustedError:
            return _Outcome(error=PROVIDER_UNAVAILABLE, retryable=True)
        except Exception:
            logger.exception("Handoff adapter %s failed", adapter_name)
            return _Outcome(error=ADAPTER_ERROR, retryable=False)
        return _classify(result)

    def _send(
        self, attempt: HandoffAttempt, envelope: ApprovalEnvelope
    ) -> HandoffAttempt:
        """Call the adapter for a pending attempt and record the outcome.

        Args:
            attempt: The pending attempt.
            envelope: Its executed envelope.

        Returns:
            The finished attempt.
        """
        plan = self._plans.get(envelope.subject_version)
        if plan is None:  # pragma: no cover - plan versions are never deleted
            raise PlanNotFoundError(envelope.subject_version)
        outcome = self._outcome(attempt.adapter, plan)
        with closing(get_connection(self._db_path)) as conn:
            conn.execute(
                _FINISH,
                (
                    outcome.status.value,
                    outcome.url,
                    outcome.expires.isoformat() if outcome.expires else None,
                    outcome.error,
                    outcome.retryable,
                    outcome.action,
                    self._clock().isoformat(),
                    attempt.id,
                ),
            )
            conn.commit()
            finished = self._one(conn, _SELECT_BY_ID, attempt.id)
        if finished is None:  # pragma: no cover - the row was just updated
            raise AttemptNotFoundError(attempt.id)
        audit(
            "handoff.attempt",
            attempt_id=finished.id,
            envelope_id=envelope.id,
            adapter=finished.adapter,
            plan_version=finished.plan_version,
            status=finished.status.value,
            error=finished.error_code,
            retryable=finished.retryable,
        )
        return finished

    def execute(
        self, envelope_id: int, *, actor: str, idempotency_key: str
    ) -> HandoffAttempt:
        """Carry out an approved handoff, at most once per idempotency key.

        Args:
            envelope_id: The approved envelope.
            actor: Who is executing.
            idempotency_key: The request's key. Repeating it returns the
                same attempt; only an attempt that failed with a passing
                fault is tried again.

        Returns:
            The attempt: ``created`` with a link, ``failed`` with a stable
            error code, or ``pending`` if another request holding the same
            key is still working.

        Raises:
            EnvelopeNotFoundError: If there is no such envelope.
            HandoffNotAvailableError: If it is not a plan handoff.
            EnvelopeNotActionableError: If it is not approved, has expired,
                or was already executed under another key.
            StaleEnvelopeError: If the plan's needs have changed.
            EnvelopeTamperedError: If the stored approval was altered.
            KeyBelongsToAnotherHandoffError: If the key was used for a
                different approval.
        """
        envelope = self._handoff_envelope(envelope_id)
        with closing(get_connection(self._db_path)) as conn:
            known = self._one(conn, _SELECT_BY_KEY, idempotency_key)
        if known is not None:
            return self._repeat(known, envelope)
        attempt, mine = self._claim_new(envelope, idempotency_key, actor)
        if not mine:
            return attempt
        try:
            executed = self._envelopes.execute(
                envelope_id,
                actor=actor,
                current_fingerprint=self._plans.current_fingerprint(
                    envelope.subject_version
                ),
            )
        except Exception:
            with suppress(Exception):
                self._discard_pending(attempt.id)
            raise
        return self._send(attempt, executed)

    def _repeat(
        self, known: HandoffAttempt, envelope: ApprovalEnvelope
    ) -> HandoffAttempt:
        """Handle a request whose idempotency key already has an attempt.

        Args:
            known: The attempt holding the key.
            envelope: The envelope the request names.

        Returns:
            The same attempt, or its retried outcome if it had failed with
            a passing fault.

        Raises:
            KeyBelongsToAnotherHandoffError: If the key's attempt is for a
                different envelope.
            EnvelopeNotActionableError: If the approval has since run out.
            StaleEnvelopeError: If the plan's needs have changed since.
        """
        if known.envelope_id != envelope.id:
            raise KeyBelongsToAnotherHandoffError(envelope.id)
        if known.status is not AttemptStatus.FAILED or not known.retryable:
            return known
        self._refuse_stale_retry(envelope)
        if not self._reclaim(known):
            return self._existing(known.idempotency_key, envelope.id)
        return self._send(known, envelope)
