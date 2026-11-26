"""Tests for observation batches: proposals a member reviews (ADR 0009).

Backlog principle 1, "evidence before mutation": nothing a model proposes
reaches the ledger until a household member confirms or corrects it.
"""

from __future__ import annotations

import datetime as dt
import os
from contextlib import closing
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest
from pydantic import ValidationError

from grocery_butler.db import get_connection, init_db
from grocery_butler.household_items import HouseholdItemStore
from grocery_butler.inventory_ledger import InventoryLedger
from grocery_butler.models import (
    BatchStatus,
    DecisionAction,
    HouseholdItem,
    NeedStatus,
    NewProposal,
    ObservationSource,
    ProposalDecision,
    ProposalReview,
    StockState,
)
from grocery_butler.observation_batches import (
    BATCH_TTL,
    MAX_PROPOSALS,
    BatchNotFoundError,
    BatchNotOpenError,
    InvalidReviewError,
    ObservationBatches,
    ReviewsMissingError,
)
from grocery_butler.replenishment import ReplenishmentNeeds

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

NOW = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.UTC)
SUBMITTER = "geoff@rubotpaul"
REVIEWER = "jess@rubotpaul"


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
    path = str(tmp_path / "batches.db")
    init_db(path)
    return path


@pytest.fixture()
def clock() -> FakeClock:
    """Return a clock fixed at NOW."""
    return FakeClock()


@pytest.fixture()
def batches(db_path: str, clock: FakeClock) -> ObservationBatches:
    """Return a batch service driven by the fake clock."""
    return ObservationBatches(db_path, clock=clock)


@pytest.fixture()
def ledger(db_path: str, clock: FakeClock) -> InventoryLedger:
    """Return a ledger sharing the fake clock."""
    return InventoryLedger(db_path, clock=clock)


@pytest.fixture()
def eggs(db_path: str) -> HouseholdItem:
    """Return an existing household item."""
    return HouseholdItemStore(db_path).resolve_or_create("eggs", actor="t")


@pytest.fixture()
def rice(db_path: str) -> HouseholdItem:
    """Return another existing household item."""
    return HouseholdItemStore(db_path).resolve_or_create("rice", actor="t")


def _proposal(
    name: str, state: StockState = StockState.LOW, confidence: float = 0.9
) -> NewProposal:
    """Build a proposal."""
    return NewProposal(name=name, state=state, confidence=confidence)


def _review(
    proposal_id: int,
    action: DecisionAction,
    *,
    item_id: int | None = None,
    state: StockState | None = None,
) -> ProposalReview:
    """Build a review of one proposal."""
    return ProposalReview(
        proposal_id=proposal_id, action=action, item_id=item_id, state=state
    )


class TestReviewRules:
    """The review model refuses decisions that say nothing or too much."""

    def test_correction_must_change_something(self) -> None:
        """A correction with no item and no state corrects nothing."""
        with pytest.raises(ValidationError, match="right item_id"):
            _review(1, DecisionAction.CORRECT)

    @pytest.mark.parametrize("action", [DecisionAction.CONFIRM, DecisionAction.REJECT])
    def test_only_a_correction_carries_changes(self, action: DecisionAction) -> None:
        """Confirming with a different state would be a correction in disguise."""
        with pytest.raises(ValidationError, match="Only a correction"):
            _review(1, action, state=StockState.OUT)

    def test_correction_cannot_state_unknown(self) -> None:
        """Unknown is derived, never stated."""
        with pytest.raises(ValidationError, match="on_hand, low or out"):
            _review(1, DecisionAction.CORRECT, state=StockState.UNKNOWN)

    def test_proposal_cannot_state_unknown_or_bad_confidence(self) -> None:
        """A proposal says something, with a probability."""
        with pytest.raises(ValidationError, match="on_hand, low or out"):
            _proposal("eggs", StockState.UNKNOWN)
        with pytest.raises(ValidationError):
            _proposal("eggs", confidence=1.5)
        with pytest.raises(ValidationError):
            _proposal("  ".strip())


class TestSubmit:
    """Submitting a batch stores proposals and writes nothing else."""

    def test_batch_is_open_and_the_ledger_is_untouched(
        self,
        batches: ObservationBatches,
        ledger: InventoryLedger,
        eggs: HouseholdItem,
    ) -> None:
        """Evidence before mutation: a proposal is not an observation."""
        batch = batches.submit(
            ObservationSource.PHOTO,
            [_proposal("Eggs", StockState.OUT, 0.95)],
            submitted_by=SUBMITTER,
            evidence_ref="photo:fridge-door",
            notes="fridge door",
        )

        assert batch.status is BatchStatus.OPEN
        assert batch.source is ObservationSource.PHOTO
        assert batch.submitted_by == SUBMITTER
        assert batch.evidence_ref == "photo:fridge-door"
        assert batch.created_at == NOW
        assert batch.observed_at == NOW
        assert batch.expires_at == NOW + BATCH_TTL
        assert batch.resolved_at is None
        assert ledger.history(eggs.id) == []
        assert ledger.snapshot(eggs.id).state is StockState.UNKNOWN

    def test_names_are_matched_to_items_but_never_create_them(
        self, batches: ObservationBatches, eggs: HouseholdItem, db_path: str
    ) -> None:
        """A model's guess at a name must not invent a household item."""
        batch = batches.submit(
            ObservationSource.PHOTO,
            [_proposal("  EGGS "), _proposal("dragonfruit")],
            submitted_by=SUBMITTER,
        )

        matched, unmatched = batch.proposals
        assert matched.item_id == eggs.id
        assert matched.candidate_name == "  EGGS "
        assert matched.needs_review is False
        assert unmatched.item_id is None
        assert unmatched.needs_review is True
        assert HouseholdItemStore(db_path).resolve("dragonfruit") is None

    def test_low_confidence_needs_review(
        self, batches: ObservationBatches, eggs: HouseholdItem
    ) -> None:
        """Below the threshold a member has to look."""
        batch = batches.submit(
            ObservationSource.PHOTO,
            [_proposal("eggs", confidence=0.79), _proposal("eggs", confidence=0.8)],
            submitted_by=SUBMITTER,
        )

        assert [p.needs_review for p in batch.proposals] == [True, False]

    def test_observed_at_can_be_earlier_than_submission(
        self, batches: ObservationBatches
    ) -> None:
        """A photo taken this morning is dated this morning."""
        taken = NOW - dt.timedelta(hours=3)

        batch = batches.submit(
            ObservationSource.PHOTO,
            [_proposal("eggs")],
            submitted_by=SUBMITTER,
            observed_at=taken,
        )

        assert batch.observed_at == taken

    def test_empty_oversized_and_system_batches_are_refused(
        self, batches: ObservationBatches
    ) -> None:
        """A batch has 1 to MAX_PROPOSALS proposals from a real source."""
        with pytest.raises(ValueError, match="at least one"):
            batches.submit(ObservationSource.PHOTO, [], submitted_by=SUBMITTER)
        with pytest.raises(ValueError, match=str(MAX_PROPOSALS)):
            batches.submit(
                ObservationSource.PHOTO,
                [_proposal("eggs")] * (MAX_PROPOSALS + 1),
                submitted_by=SUBMITTER,
            )
        with pytest.raises(ValueError, match="system"):
            batches.submit(
                ObservationSource.SYSTEM, [_proposal("eggs")], submitted_by=SUBMITTER
            )


class TestResolve:
    """A member's review turns proposals into observations, all at once."""

    def test_confirmed_proposal_becomes_an_observation_with_its_provenance(
        self,
        batches: ObservationBatches,
        ledger: InventoryLedger,
        eggs: HouseholdItem,
    ) -> None:
        """The ledger row keeps the source, confidence, evidence and reviewer."""
        batch = batches.submit(
            ObservationSource.PHOTO,
            [_proposal("eggs", StockState.OUT, 0.92)],
            submitted_by=SUBMITTER,
            evidence_ref="photo:1",
            observed_at=NOW - dt.timedelta(hours=1),
        )

        resolved = batches.resolve(
            batch.id,
            [_review(batch.proposals[0].id, DecisionAction.CONFIRM)],
            actor=REVIEWER,
        )

        observation = ledger.history(eggs.id)[0]
        proposal = resolved.proposals[0]
        assert resolved.status is BatchStatus.CONFIRMED
        assert resolved.resolved_by == REVIEWER
        assert resolved.resolved_at == NOW
        assert proposal.decision is ProposalDecision.CONFIRMED
        assert proposal.observation_id == observation.id
        assert observation.state is StockState.OUT
        assert observation.source is ObservationSource.PHOTO
        assert observation.confidence == 0.92
        assert observation.actor == REVIEWER
        assert observation.evidence_ref == "photo:1"
        assert observation.observed_at == NOW - dt.timedelta(hours=1)
        assert ledger.snapshot(eggs.id).state is StockState.OUT

    def test_corrected_proposal_is_recorded_as_the_members_own_statement(
        self,
        batches: ObservationBatches,
        ledger: InventoryLedger,
        eggs: HouseholdItem,
        rice: HouseholdItem,
    ) -> None:
        """A correction is human evidence, and says what it corrected."""
        batch = batches.submit(
            ObservationSource.PHOTO,
            [_proposal("eggs", StockState.OUT, 0.6)],
            submitted_by=SUBMITTER,
        )

        resolved = batches.resolve(
            batch.id,
            [
                _review(
                    batch.proposals[0].id,
                    DecisionAction.CORRECT,
                    item_id=rice.id,
                    state=StockState.LOW,
                )
            ],
            actor=REVIEWER,
        )

        proposal = resolved.proposals[0]
        observation = ledger.history(rice.id)[0]
        assert proposal.decision is ProposalDecision.CORRECTED
        assert (proposal.decided_item_id, proposal.decided_state) == (
            rice.id,
            StockState.LOW,
        )
        assert observation.source is ObservationSource.HUMAN
        assert observation.confidence == 1.0
        assert observation.state is StockState.LOW
        assert "eggs" in observation.notes
        assert "out" in observation.notes
        assert ledger.history(eggs.id) == []

    def test_correcting_only_the_state_keeps_the_matched_item(
        self,
        batches: ObservationBatches,
        ledger: InventoryLedger,
        eggs: HouseholdItem,
    ) -> None:
        """One field corrected, the other kept."""
        batch = batches.submit(
            ObservationSource.PHOTO,
            [_proposal("eggs", StockState.OUT)],
            submitted_by=SUBMITTER,
        )

        batches.resolve(
            batch.id,
            [
                _review(
                    batch.proposals[0].id, DecisionAction.CORRECT, state=StockState.LOW
                )
            ],
            actor=REVIEWER,
        )

        assert ledger.snapshot(eggs.id).state is StockState.LOW

    def test_rejected_proposal_writes_nothing(
        self,
        batches: ObservationBatches,
        ledger: InventoryLedger,
        eggs: HouseholdItem,
        rice: HouseholdItem,
    ) -> None:
        """A mixed review writes only what was accepted."""
        batch = batches.submit(
            ObservationSource.PHOTO,
            [_proposal("eggs", StockState.OUT), _proposal("rice", StockState.LOW)],
            submitted_by=SUBMITTER,
        )
        first, second = batch.proposals

        resolved = batches.resolve(
            batch.id,
            [
                _review(first.id, DecisionAction.REJECT),
                _review(second.id, DecisionAction.CONFIRM),
            ],
            actor=REVIEWER,
        )

        assert [p.decision for p in resolved.proposals] == [
            ProposalDecision.REJECTED,
            ProposalDecision.CONFIRMED,
        ]
        assert ledger.history(eggs.id) == []
        assert ledger.snapshot(rice.id).state is StockState.LOW

    def test_every_proposal_rejected_makes_the_batch_rejected(
        self, batches: ObservationBatches, eggs: HouseholdItem
    ) -> None:
        """Nothing accepted, so the batch is rejected, not confirmed."""
        batch = batches.submit(
            ObservationSource.PHOTO, [_proposal("eggs")], submitted_by=SUBMITTER
        )

        resolved = batches.resolve(
            batch.id,
            [_review(batch.proposals[0].id, DecisionAction.REJECT)],
            actor=REVIEWER,
        )

        assert resolved.status is BatchStatus.REJECTED

    def test_confirmed_low_item_gets_a_suggested_need(
        self, batches: ObservationBatches, eggs: HouseholdItem, db_path: str
    ) -> None:
        """Needs follow the ledger after a review."""
        batch = batches.submit(
            ObservationSource.PHOTO,
            [_proposal("eggs", StockState.OUT)],
            submitted_by=SUBMITTER,
        )

        batches.resolve(
            batch.id,
            [_review(batch.proposals[0].id, DecisionAction.CONFIRM)],
            actor=REVIEWER,
        )

        need = ReplenishmentNeeds(db_path).open_need_for_item(eggs.id)
        assert need is not None
        assert need.status is NeedStatus.SUGGESTED

    def test_accept_high_confidence_confirms_only_what_needs_no_review(
        self,
        batches: ObservationBatches,
        ledger: InventoryLedger,
        eggs: HouseholdItem,
        rice: HouseholdItem,
    ) -> None:
        """Bulk confirm never covers a low-confidence or unmatched proposal."""
        batch = batches.submit(
            ObservationSource.PHOTO,
            [
                _proposal("eggs", StockState.OUT, 0.95),
                _proposal("rice", StockState.LOW, 0.5),
                _proposal("dragonfruit", StockState.LOW, 0.99),
            ],
            submitted_by=SUBMITTER,
        )

        with pytest.raises(ReviewsMissingError) as raised:
            batches.resolve(batch.id, [], actor=REVIEWER, accept_high_confidence=True)

        assert raised.value.proposal_ids == [
            batch.proposals[1].id,
            batch.proposals[2].id,
        ]
        assert ledger.history(eggs.id) == []

        resolved = batches.resolve(
            batch.id,
            [
                _review(batch.proposals[1].id, DecisionAction.REJECT),
                _review(batch.proposals[2].id, DecisionAction.REJECT),
            ],
            actor=REVIEWER,
            accept_high_confidence=True,
        )

        assert [p.decision for p in resolved.proposals] == [
            ProposalDecision.CONFIRMED,
            ProposalDecision.REJECTED,
            ProposalDecision.REJECTED,
        ]
        assert ledger.snapshot(eggs.id).state is StockState.OUT
        assert ledger.history(rice.id) == []

    def test_without_the_flag_every_proposal_needs_a_review(
        self, batches: ObservationBatches, eggs: HouseholdItem
    ) -> None:
        """Silence is not consent."""
        batch = batches.submit(
            ObservationSource.PHOTO,
            [_proposal("eggs", confidence=0.99)],
            submitted_by=SUBMITTER,
        )

        with pytest.raises(ReviewsMissingError):
            batches.resolve(batch.id, [], actor=REVIEWER)

    def test_an_invalid_review_writes_nothing_at_all(
        self,
        batches: ObservationBatches,
        ledger: InventoryLedger,
        eggs: HouseholdItem,
    ) -> None:
        """All or nothing: one bad decision leaves the batch open and untouched."""
        batch = batches.submit(
            ObservationSource.PHOTO,
            [_proposal("eggs", StockState.OUT), _proposal("dragonfruit")],
            submitted_by=SUBMITTER,
        )
        first, second = batch.proposals

        with pytest.raises(InvalidReviewError, match="matched no household item"):
            batches.resolve(
                batch.id,
                [
                    _review(first.id, DecisionAction.CONFIRM),
                    _review(second.id, DecisionAction.CONFIRM),
                ],
                actor=REVIEWER,
            )

        after = batches.get(batch.id)
        assert after is not None
        assert after.status is BatchStatus.OPEN
        assert [p.decision for p in after.proposals] == [ProposalDecision.PENDING] * 2
        assert ledger.history(eggs.id) == []

    @pytest.mark.parametrize(
        ("build", "message"),
        [
            (
                lambda p, other: [_review(99999, DecisionAction.REJECT)],
                "not in this batch",
            ),
            (
                lambda p, other: [
                    _review(p, DecisionAction.REJECT),
                    _review(p, DecisionAction.CONFIRM),
                ],
                "more than once",
            ),
            (
                lambda p, other: [_review(p, DecisionAction.CORRECT, item_id=99999)],
                "No household item",
            ),
            (
                lambda p, other: [_review(other, DecisionAction.REJECT)],
                "not in this batch",
            ),
        ],
        ids=["unknown-proposal", "duplicate", "unknown-item", "other-batch"],
    )
    def test_bad_reviews_are_refused(
        self,
        batches: ObservationBatches,
        eggs: HouseholdItem,
        build: object,
        message: str,
    ) -> None:
        """Each kind of bad review says what is wrong."""
        other = batches.submit(
            ObservationSource.PHOTO, [_proposal("eggs")], submitted_by=SUBMITTER
        )
        batch = batches.submit(
            ObservationSource.PHOTO, [_proposal("eggs")], submitted_by=SUBMITTER
        )
        assert callable(build)
        reviews = build(batch.proposals[0].id, other.proposals[0].id)

        with pytest.raises(InvalidReviewError, match=message):
            batches.resolve(batch.id, reviews, actor=REVIEWER)

    def test_correcting_only_the_state_of_an_unmatched_proposal_is_refused(
        self, batches: ObservationBatches, ledger: InventoryLedger, eggs: HouseholdItem
    ) -> None:
        """A correction still needs an item to be about."""
        batch = batches.submit(
            ObservationSource.PHOTO, [_proposal("dragonfruit")], submitted_by=SUBMITTER
        )

        with pytest.raises(InvalidReviewError, match="matched no household item"):
            batches.resolve(
                batch.id,
                [
                    _review(
                        batch.proposals[0].id,
                        DecisionAction.CORRECT,
                        state=StockState.OUT,
                    )
                ],
                actor=REVIEWER,
            )

        after = batches.get(batch.id)
        assert after is not None
        assert after.status is BatchStatus.OPEN

    def test_correction_onto_a_merged_item_lands_on_the_survivor(
        self,
        batches: ObservationBatches,
        ledger: InventoryLedger,
        eggs: HouseholdItem,
        db_path: str,
    ) -> None:
        """A corrected item id is followed to its canonical item."""
        items = HouseholdItemStore(db_path)
        dozen = items.resolve_or_create("dozen eggs", actor="t")
        items.merge(dozen.id, eggs.id, actor="t")
        batch = batches.submit(
            ObservationSource.PHOTO, [_proposal("mystery")], submitted_by=SUBMITTER
        )

        resolved = batches.resolve(
            batch.id,
            [_review(batch.proposals[0].id, DecisionAction.CORRECT, item_id=dozen.id)],
            actor=REVIEWER,
        )

        assert resolved.proposals[0].decided_item_id == eggs.id
        assert ledger.snapshot(eggs.id).state is StockState.LOW


class TestLifecycle:
    """A batch is reviewed once, or expires."""

    def test_a_resolved_batch_cannot_be_resolved_again(
        self,
        batches: ObservationBatches,
        ledger: InventoryLedger,
        eggs: HouseholdItem,
    ) -> None:
        """No double-apply."""
        batch = batches.submit(
            ObservationSource.PHOTO, [_proposal("eggs")], submitted_by=SUBMITTER
        )
        reviews = [_review(batch.proposals[0].id, DecisionAction.CONFIRM)]
        batches.resolve(batch.id, reviews, actor=REVIEWER)

        with pytest.raises(BatchNotOpenError, match="confirmed"):
            batches.resolve(batch.id, reviews, actor=REVIEWER)

        assert len(ledger.history(eggs.id)) == 1

    def test_a_second_review_racing_the_first_writes_nothing(
        self,
        batches: ObservationBatches,
        ledger: InventoryLedger,
        eggs: HouseholdItem,
    ) -> None:
        """Two reviews that both saw the batch open: only one is applied."""
        batch = batches.submit(
            ObservationSource.PHOTO,
            [_proposal("eggs", StockState.OUT)],
            submitted_by=SUBMITTER,
        )
        reviews = [_review(batch.proposals[0].id, DecisionAction.CONFIRM)]
        batches.resolve(batch.id, reviews, actor=REVIEWER)

        # The loser read the batch while it was still open.
        with (
            patch.object(ObservationBatches, "_require_open", return_value=batch),
            pytest.raises(BatchNotOpenError, match="confirmed"),
        ):
            batches.resolve(batch.id, reviews, actor="sam@rubotpaul")

        after = batches.get(batch.id)
        assert after is not None
        assert after.resolved_by == REVIEWER
        assert len(ledger.history(eggs.id)) == 1

    def test_a_reject_racing_a_review_changes_nothing(
        self,
        batches: ObservationBatches,
        ledger: InventoryLedger,
        eggs: HouseholdItem,
    ) -> None:
        """A late reject can't overwrite a review that already applied."""
        batch = batches.submit(
            ObservationSource.PHOTO,
            [_proposal("eggs", StockState.OUT)],
            submitted_by=SUBMITTER,
        )
        batches.resolve(
            batch.id,
            [_review(batch.proposals[0].id, DecisionAction.CONFIRM)],
            actor=REVIEWER,
        )

        with (
            patch.object(ObservationBatches, "_require_open", return_value=batch),
            pytest.raises(BatchNotOpenError, match="confirmed"),
        ):
            batches.reject(batch.id, actor="sam@rubotpaul")

        after = batches.get(batch.id)
        assert after is not None
        assert after.status is BatchStatus.CONFIRMED
        assert [p.decision for p in after.proposals] == [ProposalDecision.CONFIRMED]
        assert len(ledger.history(eggs.id)) == 1

    def test_a_review_racing_a_reject_writes_nothing(
        self,
        batches: ObservationBatches,
        ledger: InventoryLedger,
        eggs: HouseholdItem,
    ) -> None:
        """A late review can't revive a batch that was thrown away."""
        batch = batches.submit(
            ObservationSource.PHOTO,
            [_proposal("eggs", StockState.OUT)],
            submitted_by=SUBMITTER,
        )
        batches.reject(batch.id, actor=REVIEWER)

        with (
            patch.object(ObservationBatches, "_require_open", return_value=batch),
            pytest.raises(BatchNotOpenError, match="rejected"),
        ):
            batches.resolve(
                batch.id,
                [_review(batch.proposals[0].id, DecisionAction.CONFIRM)],
                actor="sam@rubotpaul",
            )

        assert ledger.history(eggs.id) == []

    def test_confirming_a_proposal_whose_item_was_merged_away_counts_for_the_survivor(
        self,
        batches: ObservationBatches,
        ledger: InventoryLedger,
        eggs: HouseholdItem,
        db_path: str,
    ) -> None:
        """A match made before a merge still lands on the surviving item."""
        items = HouseholdItemStore(db_path)
        dozen = items.resolve_or_create("dozen eggs", actor="t")
        batch = batches.submit(
            ObservationSource.PHOTO,
            [_proposal("dozen eggs", StockState.OUT)],
            submitted_by=SUBMITTER,
        )
        items.merge(dozen.id, eggs.id, actor="t")

        batches.resolve(
            batch.id,
            [_review(batch.proposals[0].id, DecisionAction.CONFIRM)],
            actor=REVIEWER,
        )

        assert ledger.snapshot(eggs.id).state is StockState.OUT

    def test_reject_closes_the_batch_and_writes_nothing(
        self,
        batches: ObservationBatches,
        ledger: InventoryLedger,
        eggs: HouseholdItem,
    ) -> None:
        """Throwing a batch away is one call."""
        batch = batches.submit(
            ObservationSource.PHOTO, [_proposal("eggs")], submitted_by=SUBMITTER
        )

        rejected = batches.reject(batch.id, actor=REVIEWER)

        assert rejected.status is BatchStatus.REJECTED
        assert rejected.resolved_by == REVIEWER
        assert [p.decision for p in rejected.proposals] == [ProposalDecision.REJECTED]
        assert ledger.history(eggs.id) == []
        with pytest.raises(BatchNotOpenError):
            batches.reject(batch.id, actor=REVIEWER)

    def test_an_unreviewed_batch_expires(
        self,
        batches: ObservationBatches,
        clock: FakeClock,
        eggs: HouseholdItem,
    ) -> None:
        """Old proposals can't be confirmed as if they were current."""
        batch = batches.submit(
            ObservationSource.PHOTO, [_proposal("eggs")], submitted_by=SUBMITTER
        )
        clock.now = batch.expires_at + dt.timedelta(seconds=1)

        with pytest.raises(BatchNotOpenError, match="expired"):
            batches.resolve(
                batch.id,
                [_review(batch.proposals[0].id, DecisionAction.CONFIRM)],
                actor=REVIEWER,
            )

        after = batches.get(batch.id)
        assert after is not None
        assert after.status is BatchStatus.EXPIRED

    def test_a_batch_is_still_open_at_its_expiry_instant(
        self, batches: ObservationBatches, clock: FakeClock, eggs: HouseholdItem
    ) -> None:
        """The deadline itself is inside the window."""
        batch = batches.submit(
            ObservationSource.PHOTO, [_proposal("eggs")], submitted_by=SUBMITTER
        )
        clock.now = batch.expires_at

        resolved = batches.resolve(
            batch.id,
            [_review(batch.proposals[0].id, DecisionAction.CONFIRM)],
            actor=REVIEWER,
        )

        assert resolved.status is BatchStatus.CONFIRMED

    def test_missing_batch(self, batches: ObservationBatches) -> None:
        """Unknown ids are reported."""
        assert batches.get(99999) is None
        with pytest.raises(BatchNotFoundError, match="99999"):
            batches.resolve(99999, [], actor=REVIEWER)
        with pytest.raises(BatchNotFoundError):
            batches.reject(99999, actor=REVIEWER)

    def test_list_filters_by_status_and_expires_stale_batches(
        self, batches: ObservationBatches, clock: FakeClock, eggs: HouseholdItem
    ) -> None:
        """Listing open batches never returns one past its deadline."""
        old = batches.submit(
            ObservationSource.PHOTO, [_proposal("eggs")], submitted_by=SUBMITTER
        )
        clock.advance(hours=1)
        closed = batches.submit(
            ObservationSource.PHOTO, [_proposal("eggs")], submitted_by=SUBMITTER
        )
        batches.reject(closed.id, actor=REVIEWER)
        clock.now = old.expires_at + dt.timedelta(minutes=1)
        fresh = batches.submit(
            ObservationSource.PHOTO, [_proposal("eggs")], submitted_by=SUBMITTER
        )

        assert [b.id for b in batches.list_batches(BatchStatus.OPEN)] == [fresh.id]
        assert [b.id for b in batches.list_batches(BatchStatus.EXPIRED)] == [old.id]
        assert [b.id for b in batches.list_batches()] == [old.id, closed.id, fresh.id]


_PG_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.mark.skipif(not _PG_URL, reason="TEST_DATABASE_URL not set")
class TestPostgres:
    """Batches behave the same on Postgres."""

    @pytest.fixture()
    def pg_item(self) -> Iterator[HouseholdItem]:
        """Yield a fresh item on the Postgres test database, then remove it."""
        init_db(_PG_URL)
        self._clean()
        yield HouseholdItemStore(_PG_URL).resolve_or_create("pgb eggs", actor="t")
        self._clean()

    def _clean(self) -> None:
        """Delete this suite's batches, item, needs and ledger rows."""
        with closing(get_connection(_PG_URL)) as conn:
            for statement in (
                "DELETE FROM observation_proposals WHERE candidate_name LIKE 'pgb %'",
                "DELETE FROM observation_batches WHERE notes = 'pgb'",
            ):
                conn.execute(statement)
            ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM household_items WHERE name = ?", ("pgb eggs",)
                ).fetchall()
            ]
            for item_id in ids:
                for statement in (
                    "DELETE FROM replenishment_need_reasons WHERE need_id IN"
                    " (SELECT id FROM replenishment_needs WHERE item_id = ?)",
                    "DELETE FROM replenishment_need_events WHERE need_id IN"
                    " (SELECT id FROM replenishment_needs WHERE item_id = ?)",
                    "DELETE FROM replenishment_needs WHERE item_id = ?",
                    "DELETE FROM inventory_observations WHERE item_id = ?",
                    "DELETE FROM household_item_aliases WHERE item_id = ?",
                    "DELETE FROM household_item_events WHERE item_id = ?",
                    "DELETE FROM household_items WHERE id = ?",
                ):
                    conn.execute(statement, (item_id,))
            conn.commit()

    def test_submit_and_resolve_round_trip(self, pg_item: HouseholdItem) -> None:
        """Proposals become observations in one transaction on Postgres."""
        batches = ObservationBatches(_PG_URL)
        batch = batches.submit(
            ObservationSource.PHOTO,
            [
                NewProposal(name="pgb eggs", state=StockState.OUT, confidence=0.9),
                NewProposal(name="pgb nothing", state=StockState.LOW, confidence=0.9),
            ],
            submitted_by=SUBMITTER,
            notes="pgb",
        )

        resolved = batches.resolve(
            batch.id,
            [
                _review(batch.proposals[0].id, DecisionAction.CONFIRM),
                _review(batch.proposals[1].id, DecisionAction.REJECT),
            ],
            actor=REVIEWER,
        )

        assert resolved.status is BatchStatus.CONFIRMED
        assert InventoryLedger(_PG_URL).snapshot(pg_item.id).state is StockState.OUT

    def test_a_second_review_racing_the_first_writes_nothing(
        self, pg_item: HouseholdItem
    ) -> None:
        """The atomic claim holds on Postgres too."""
        batches = ObservationBatches(_PG_URL)
        batch = batches.submit(
            ObservationSource.PHOTO,
            [NewProposal(name="pgb eggs", state=StockState.OUT, confidence=0.9)],
            submitted_by=SUBMITTER,
            notes="pgb",
        )
        reviews = [_review(batch.proposals[0].id, DecisionAction.CONFIRM)]
        batches.resolve(batch.id, reviews, actor=REVIEWER)

        with (
            patch.object(ObservationBatches, "_require_open", return_value=batch),
            pytest.raises(BatchNotOpenError),
        ):
            batches.resolve(batch.id, reviews, actor="sam@rubotpaul")

        assert len(InventoryLedger(_PG_URL).history(pg_item.id)) == 1
