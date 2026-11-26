"""Tests for approval envelopes (ADR 0010, backlog P0.9, issue #137).

The acceptance test: a changed plan can't execute under an earlier
confirmation.
"""

from __future__ import annotations

import datetime as dt
import os
from contextlib import closing
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from grocery_butler.approvals import (
    DEFAULT_TTL,
    ApprovalEnvelopes,
    ApprovalScope,
    EnvelopeNotActionableError,
    EnvelopeNotFoundError,
    EnvelopeStatus,
    EnvelopeTamperedError,
    PayloadMismatchError,
    StaleEnvelopeError,
    canonical_json,
    payload_hash,
)
from grocery_butler.db import get_connection, init_db

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from grocery_butler.approvals import ApprovalEnvelope

NOW = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.UTC)
MEMBER = "geoff@rubotpaul"
PAYLOAD: dict[str, Any] = {
    "plan_id": 12,
    "version": 3,
    "lines": [{"item_id": 5, "quantity": 2, "unit": "gal"}],
}
FINGERPRINT = "needs-v1"


class FakeClock:
    """A clock tests move by hand."""

    def __init__(self) -> None:
        """Start at NOW."""
        self.now = NOW

    def __call__(self) -> dt.datetime:
        """Return the current fake time."""
        return self.now

    def advance(self, **delta: float) -> None:
        """Move the clock forward."""
        self.now += dt.timedelta(**delta)


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a migrated temporary database path."""
    path = str(tmp_path / "approvals.db")
    init_db(path)
    return path


@pytest.fixture()
def clock() -> FakeClock:
    """Return a clock fixed at NOW."""
    return FakeClock()


@pytest.fixture()
def envelopes(db_path: str, clock: FakeClock) -> ApprovalEnvelopes:
    """Return an envelope service driven by the fake clock."""
    return ApprovalEnvelopes(db_path, clock=clock)


def _draft(envelopes: ApprovalEnvelopes, **overrides: Any) -> ApprovalEnvelope:
    """Draft an envelope for plan 12 version 3."""
    arguments: dict[str, Any] = {
        "kind": "plan_handoff",
        "payload": PAYLOAD,
        "subject_type": "shopping_plan",
        "subject_id": 12,
        "subject_version": 3,
        "subject_fingerprint": FINGERPRINT,
        "created_by": "rubotpaul",
    }
    arguments.update(overrides)
    return envelopes.draft(**arguments)


def _approved(envelopes: ApprovalEnvelopes) -> ApprovalEnvelope:
    """Draft and approve an envelope."""
    draft = _draft(envelopes)
    return envelopes.approve(
        draft.id,
        payload_hash=draft.payload_hash,
        actor=MEMBER,
        current_fingerprint=FINGERPRINT,
    )


class TestCanonicalPayload:
    """One payload has one hash, however its keys were ordered."""

    def test_key_order_and_spacing_do_not_change_the_hash(self) -> None:
        """Canonical JSON sorts keys and uses no optional whitespace."""
        reordered = {"version": 3, "lines": PAYLOAD["lines"], "plan_id": 12}

        assert canonical_json(reordered) == canonical_json(PAYLOAD)
        assert payload_hash(reordered) == payload_hash(PAYLOAD)
        assert " " not in canonical_json(PAYLOAD)

    @pytest.mark.parametrize(
        "changed",
        [
            {**PAYLOAD, "version": 4},
            {**PAYLOAD, "lines": [{"item_id": 5, "quantity": 3, "unit": "gal"}]},
            {**PAYLOAD, "extra": None},
        ],
        ids=["version", "quantity", "added-key"],
    )
    def test_any_change_to_the_content_changes_the_hash(
        self, changed: dict[str, Any]
    ) -> None:
        """A different payload can never share a hash."""
        assert payload_hash(changed) != payload_hash(PAYLOAD)

    def test_hash_is_sha256_hex(self) -> None:
        """Clients can recompute it."""
        digest = payload_hash(PAYLOAD)

        assert len(digest) == 64
        assert int(digest, 16) >= 0


class TestDraft:
    """A draft stores exactly what will be approved."""

    def test_draft_records_payload_hash_subject_and_deadline(
        self, envelopes: ApprovalEnvelopes
    ) -> None:
        """Everything needed to bind an approval is fixed at draft time."""
        draft = _draft(envelopes)

        assert draft.status is EnvelopeStatus.DRAFT
        assert draft.scope is ApprovalScope.HANDOFF
        assert draft.payload == PAYLOAD
        assert draft.payload_hash == payload_hash(PAYLOAD)
        assert (draft.subject_type, draft.subject_id, draft.subject_version) == (
            "shopping_plan",
            12,
            3,
        )
        assert draft.subject_fingerprint == FINGERPRINT
        assert draft.created_by == "rubotpaul"
        assert draft.created_at == NOW
        assert draft.expires_at == NOW + DEFAULT_TTL
        assert draft.approved_by is None

    def test_stored_payload_is_a_copy(self, envelopes: ApprovalEnvelopes) -> None:
        """Changing the caller's dict afterwards changes nothing stored."""
        lines = [{"item_id": 5}]
        payload: dict[str, Any] = {"plan_id": 1, "lines": lines}
        draft = _draft(envelopes, payload=payload)

        lines.append({"item_id": 99})

        stored = envelopes.get(draft.id)
        assert stored is not None
        assert stored.payload == {"plan_id": 1, "lines": [{"item_id": 5}]}

    def test_there_is_no_purchase_scope(self) -> None:
        """ADR 0001: purchase approval lives in the retailer's surface."""
        assert [scope.value for scope in ApprovalScope] == ["handoff"]

    def test_a_custom_deadline_is_kept_and_must_be_positive(
        self, envelopes: ApprovalEnvelopes
    ) -> None:
        """A draft always has a future deadline."""
        draft = _draft(envelopes, ttl=dt.timedelta(minutes=5))

        assert draft.expires_at == NOW + dt.timedelta(minutes=5)
        with pytest.raises(ValueError, match="deadline"):
            _draft(envelopes, ttl=dt.timedelta(0))

    def test_get_missing_envelope_returns_none(
        self, envelopes: ApprovalEnvelopes
    ) -> None:
        """A missing envelope is not an error for get."""
        assert envelopes.get(99999) is None


class TestApprove:
    """An approval is bound to the payload, the actor and the moment."""

    def test_approval_records_who_when_and_a_binding(
        self, envelopes: ApprovalEnvelopes, clock: FakeClock
    ) -> None:
        """The approver echoes the hash of what they saw."""
        draft = _draft(envelopes)
        clock.advance(minutes=2)

        approved = envelopes.approve(
            draft.id,
            payload_hash=draft.payload_hash,
            actor=MEMBER,
            current_fingerprint=FINGERPRINT,
        )

        assert approved.status is EnvelopeStatus.APPROVED
        assert approved.approved_by == MEMBER
        assert approved.approved_at == NOW + dt.timedelta(minutes=2)
        assert approved.approval_binding is not None
        assert len(approved.approval_binding) == 64
        assert approved.payload == PAYLOAD

    def test_a_different_hash_is_refused_and_the_draft_stays(
        self, envelopes: ApprovalEnvelopes
    ) -> None:
        """You can only approve the content you were shown."""
        draft = _draft(envelopes)

        with pytest.raises(PayloadMismatchError, match="does not match"):
            envelopes.approve(
                draft.id,
                payload_hash=payload_hash({**PAYLOAD, "version": 4}),
                actor=MEMBER,
                current_fingerprint=FINGERPRINT,
            )

        after = envelopes.get(draft.id)
        assert after is not None
        assert after.status is EnvelopeStatus.DRAFT

    def test_changed_underlying_state_makes_the_draft_stale(
        self, envelopes: ApprovalEnvelopes
    ) -> None:
        """The needs under the plan moved; the member must see a new draft."""
        draft = _draft(envelopes)

        with pytest.raises(StaleEnvelopeError, match="changed"):
            envelopes.approve(
                draft.id,
                payload_hash=draft.payload_hash,
                actor=MEMBER,
                current_fingerprint="needs-v2",
            )

        after = envelopes.get(draft.id)
        assert after is not None
        assert after.status is EnvelopeStatus.STALE

    def test_expired_draft_cannot_be_approved(
        self, envelopes: ApprovalEnvelopes, clock: FakeClock
    ) -> None:
        """Past the deadline the draft is expired, not approvable."""
        draft = _draft(envelopes)
        clock.now = draft.expires_at + dt.timedelta(seconds=1)

        with pytest.raises(EnvelopeNotActionableError, match="expired"):
            envelopes.approve(
                draft.id,
                payload_hash=draft.payload_hash,
                actor=MEMBER,
                current_fingerprint=FINGERPRINT,
            )

        after = envelopes.get(draft.id)
        assert after is not None
        assert after.status is EnvelopeStatus.EXPIRED

    def test_draft_can_still_be_approved_at_its_deadline(
        self, envelopes: ApprovalEnvelopes, clock: FakeClock
    ) -> None:
        """The deadline instant is inside the window."""
        draft = _draft(envelopes)
        clock.now = draft.expires_at

        approved = envelopes.approve(
            draft.id,
            payload_hash=draft.payload_hash,
            actor=MEMBER,
            current_fingerprint=FINGERPRINT,
        )

        assert approved.status is EnvelopeStatus.APPROVED

    def test_an_envelope_is_approved_once(self, envelopes: ApprovalEnvelopes) -> None:
        """A second approval doesn't replace the first approver."""
        approved = _approved(envelopes)

        with pytest.raises(EnvelopeNotActionableError, match="approved"):
            envelopes.approve(
                approved.id,
                payload_hash=approved.payload_hash,
                actor="jess@rubotpaul",
                current_fingerprint=FINGERPRINT,
            )

        after = envelopes.get(approved.id)
        assert after is not None
        assert after.approved_by == MEMBER

    def test_missing_envelope_raises(self, envelopes: ApprovalEnvelopes) -> None:
        """You can only approve what exists."""
        with pytest.raises(EnvelopeNotFoundError, match="99999"):
            envelopes.approve(
                99999, payload_hash="x", actor=MEMBER, current_fingerprint=FINGERPRINT
            )


class TestExecute:
    """Execution reads the stored payload, once, if nothing changed."""

    def test_execute_returns_the_stored_payload_and_closes_the_envelope(
        self, envelopes: ApprovalEnvelopes
    ) -> None:
        """The executor gets its content from the envelope, not the caller."""
        approved = _approved(envelopes)

        executed = envelopes.execute(
            approved.id, actor="rubotpaul", current_fingerprint=FINGERPRINT
        )

        assert executed.status is EnvelopeStatus.EXECUTED
        assert executed.payload == PAYLOAD
        assert executed.resolved_by == "rubotpaul"
        assert executed.resolved_at == NOW
        assert executed.approved_by == MEMBER

    def test_a_changed_plan_cannot_execute_under_an_earlier_approval(
        self, envelopes: ApprovalEnvelopes
    ) -> None:
        """The backlog's acceptance test for P0.9."""
        approved = _approved(envelopes)

        with pytest.raises(StaleEnvelopeError):
            envelopes.execute(
                approved.id, actor="rubotpaul", current_fingerprint="needs-v2"
            )

        after = envelopes.get(approved.id)
        assert after is not None
        assert after.status is EnvelopeStatus.STALE

    def test_an_unapproved_draft_cannot_execute(
        self, envelopes: ApprovalEnvelopes
    ) -> None:
        """Draft, confirm, execute: no skipping the middle."""
        draft = _draft(envelopes)

        with pytest.raises(EnvelopeNotActionableError, match="draft"):
            envelopes.execute(
                draft.id, actor="rubotpaul", current_fingerprint=FINGERPRINT
            )

    def test_an_envelope_executes_once(self, envelopes: ApprovalEnvelopes) -> None:
        """A retry can't hand the plan off twice."""
        approved = _approved(envelopes)
        envelopes.execute(
            approved.id, actor="rubotpaul", current_fingerprint=FINGERPRINT
        )

        with pytest.raises(EnvelopeNotActionableError, match="executed"):
            envelopes.execute(
                approved.id, actor="rubotpaul", current_fingerprint=FINGERPRINT
            )

    def test_an_approval_expires_too(
        self, envelopes: ApprovalEnvelopes, clock: FakeClock
    ) -> None:
        """An old approval can't be executed later."""
        approved = _approved(envelopes)
        clock.now = approved.expires_at + dt.timedelta(seconds=1)

        with pytest.raises(EnvelopeNotActionableError, match="expired"):
            envelopes.execute(
                approved.id, actor="rubotpaul", current_fingerprint=FINGERPRINT
            )

    @pytest.mark.parametrize(
        ("statement", "value"),
        [
            (
                "UPDATE approval_envelopes SET payload = ? WHERE id = ?",
                '{"plan_id":12,"version":3,"lines":[]}',
            ),
            ("UPDATE approval_envelopes SET approved_by = ? WHERE id = ?", "mallory"),
            ("UPDATE approval_envelopes SET payload_hash = ? WHERE id = ?", "0" * 64),
        ],
        ids=["payload", "approver", "hash"],
    )
    def test_a_row_altered_after_approval_is_refused(
        self, envelopes: ApprovalEnvelopes, db_path: str, statement: str, value: str
    ) -> None:
        """The binding is checked before anything executes."""
        approved = _approved(envelopes)
        with closing(get_connection(db_path)) as conn:
            conn.execute(statement, (value, approved.id))
            conn.commit()

        with pytest.raises(EnvelopeTamperedError, match="altered"):
            envelopes.execute(
                approved.id, actor="rubotpaul", current_fingerprint=FINGERPRINT
            )

        with closing(get_connection(db_path)) as conn:
            row = conn.execute(
                "SELECT status FROM approval_envelopes WHERE id = ?", (approved.id,)
            ).fetchall()[0]
        assert row["status"] == "approved"


class TestReject:
    """A member can turn a draft or an approval down."""

    def test_reject_closes_a_draft_with_a_note(
        self, envelopes: ApprovalEnvelopes
    ) -> None:
        """Rejection is recorded with who and why."""
        draft = _draft(envelopes)

        rejected = envelopes.reject(draft.id, actor=MEMBER, note="wrong store")

        assert rejected.status is EnvelopeStatus.REJECTED
        assert (rejected.resolved_by, rejected.resolution_note) == (
            MEMBER,
            "wrong store",
        )

    def test_an_approval_can_be_withdrawn_before_execution(
        self, envelopes: ApprovalEnvelopes
    ) -> None:
        """Changing your mind is allowed until it executes."""
        approved = _approved(envelopes)

        envelopes.reject(approved.id, actor=MEMBER)

        with pytest.raises(EnvelopeNotActionableError, match="rejected"):
            envelopes.execute(
                approved.id, actor="rubotpaul", current_fingerprint=FINGERPRINT
            )

    def test_an_executed_envelope_cannot_be_rejected(
        self, envelopes: ApprovalEnvelopes
    ) -> None:
        """What is done is done; that is reconciliation's job."""
        approved = _approved(envelopes)
        envelopes.execute(
            approved.id, actor="rubotpaul", current_fingerprint=FINGERPRINT
        )

        with pytest.raises(EnvelopeNotActionableError, match="executed"):
            envelopes.reject(approved.id, actor=MEMBER)


class TestConcurrentCallers:
    """Of two callers that both read an envelope as live, one wins."""

    def test_second_approval_racing_the_first_is_refused(
        self, envelopes: ApprovalEnvelopes
    ) -> None:
        """The first approver stays the approver."""
        draft = _draft(envelopes)
        _ = envelopes.approve(
            draft.id,
            payload_hash=draft.payload_hash,
            actor=MEMBER,
            current_fingerprint=FINGERPRINT,
        )

        with (
            patch.object(ApprovalEnvelopes, "_require_status", return_value=draft),
            pytest.raises(EnvelopeNotActionableError, match="approved"),
        ):
            envelopes.approve(
                draft.id,
                payload_hash=draft.payload_hash,
                actor="jess@rubotpaul",
                current_fingerprint=FINGERPRINT,
            )

        after = envelopes.get(draft.id)
        assert after is not None
        assert after.approved_by == MEMBER

    def test_second_execution_racing_the_first_is_refused(
        self, envelopes: ApprovalEnvelopes
    ) -> None:
        """A handoff can't be claimed twice."""
        approved = _approved(envelopes)
        envelopes.execute(
            approved.id, actor="worker-1", current_fingerprint=FINGERPRINT
        )

        with (
            patch.object(ApprovalEnvelopes, "_require_status", return_value=approved),
            pytest.raises(EnvelopeNotActionableError, match="executed"),
        ):
            envelopes.execute(
                approved.id, actor="worker-2", current_fingerprint=FINGERPRINT
            )

        after = envelopes.get(approved.id)
        assert after is not None
        assert after.resolved_by == "worker-1"

    def test_reject_racing_an_execution_is_refused(
        self, envelopes: ApprovalEnvelopes
    ) -> None:
        """A late reject can't claim to have stopped what already ran."""
        approved = _approved(envelopes)
        envelopes.execute(
            approved.id, actor="worker-1", current_fingerprint=FINGERPRINT
        )

        with (
            patch.object(ApprovalEnvelopes, "get", return_value=approved),
            pytest.raises(EnvelopeNotActionableError, match="executed"),
        ):
            envelopes.reject(approved.id, actor=MEMBER)

    def test_reject_of_a_missing_envelope_raises(
        self, envelopes: ApprovalEnvelopes
    ) -> None:
        """You can only reject what exists."""
        with pytest.raises(EnvelopeNotFoundError):
            envelopes.reject(99999, actor=MEMBER)


class TestListing:
    """Envelopes can be listed for a subject."""

    def test_list_for_subject_is_oldest_first_and_expires_overdue(
        self, envelopes: ApprovalEnvelopes, clock: FakeClock
    ) -> None:
        """A plan's envelopes, with past-deadline ones shown as expired."""
        old = _draft(envelopes)
        other = _draft(envelopes, subject_id=13)
        clock.now = old.expires_at + dt.timedelta(minutes=1)
        fresh = _draft(envelopes)

        listed = envelopes.list_for_subject("shopping_plan", 12)

        assert [e.id for e in listed] == [old.id, fresh.id]
        assert [e.status for e in listed] == [
            EnvelopeStatus.EXPIRED,
            EnvelopeStatus.DRAFT,
        ]
        assert other.id not in {e.id for e in listed}


_PG_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.mark.skipif(not _PG_URL, reason="TEST_DATABASE_URL not set")
class TestPostgres:
    """Envelopes behave the same on Postgres."""

    @pytest.fixture()
    def pg_envelopes(self) -> Iterator[ApprovalEnvelopes]:
        """Yield a service on the Postgres test database, cleaning its rows."""
        init_db(_PG_URL)
        self._clean()
        yield ApprovalEnvelopes(_PG_URL)
        self._clean()

    def _clean(self) -> None:
        """Delete this suite's envelopes."""
        with closing(get_connection(_PG_URL)) as conn:
            conn.execute(
                "DELETE FROM approval_envelopes WHERE kind = ?", ("pg_plan_handoff",)
            )
            conn.commit()

    def test_draft_approve_execute_once(self, pg_envelopes: ApprovalEnvelopes) -> None:
        """The lifecycle and the single-execution claim work on Postgres."""
        draft = _draft(pg_envelopes, kind="pg_plan_handoff")
        approved = pg_envelopes.approve(
            draft.id,
            payload_hash=draft.payload_hash,
            actor=MEMBER,
            current_fingerprint=FINGERPRINT,
        )
        executed = pg_envelopes.execute(
            approved.id, actor="rubotpaul", current_fingerprint=FINGERPRINT
        )

        assert executed.status is EnvelopeStatus.EXECUTED
        assert executed.payload == PAYLOAD
        with pytest.raises(EnvelopeNotActionableError):
            pg_envelopes.execute(
                approved.id, actor="rubotpaul", current_fingerprint=FINGERPRINT
            )
