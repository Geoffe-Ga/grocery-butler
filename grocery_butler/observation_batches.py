"""Observation batches: proposals that a household member reviews.

Implements ADR 0009's proposal rule and the backlog's first principle,
"evidence before mutation". A model (or a parser reading a chat message)
submits what it thinks it saw as a batch of proposals. Nothing reaches the
observation ledger until a member confirms, corrects or rejects each one,
and a whole review is applied in a single transaction or not at all.
"""

from __future__ import annotations

import datetime as dt
from contextlib import closing
from typing import TYPE_CHECKING, Any

from grocery_butler.db import get_connection
from grocery_butler.household_items import HouseholdItemStore
from grocery_butler.inventory_ledger import InventoryLedger, utc_now
from grocery_butler.models import (
    BatchStatus,
    DecisionAction,
    NewObservation,
    NewProposal,
    ObservationBatch,
    ObservationProposal,
    ObservationSource,
    ProposalDecision,
    ProposalReview,
    StockState,
)
from grocery_butler.replenishment import ReplenishmentNeeds

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from grocery_butler.db.adapter import DatabaseConnection

# How long a batch can wait for review. After that its proposals describe a
# kitchen that may have changed, and it can no longer be confirmed.
BATCH_TTL = dt.timedelta(hours=72)
MAX_PROPOSALS = 200

_SELECT_BATCH = (
    "SELECT id, status, source, submitted_by, evidence_ref, notes, observed_at,"
    " created_at, expires_at, resolved_at, resolved_by"
    " FROM observation_batches WHERE id = ?"
)
_SELECT_ALL_BATCHES = (
    "SELECT id, status, source, submitted_by, evidence_ref, notes, observed_at,"
    " created_at, expires_at, resolved_at, resolved_by"
    " FROM observation_batches ORDER BY created_at, id"
)
_SELECT_PROPOSALS = (
    "SELECT id, batch_id, candidate_name, item_id, state, confidence, notes,"
    " decision, decided_item_id, decided_state, observation_id"
    " FROM observation_proposals WHERE batch_id = ? ORDER BY id"
)


class BatchNotFoundError(LookupError):
    """Raised when an observation batch id does not exist."""

    def __init__(self, batch_id: int) -> None:
        """Name the missing batch.

        Args:
            batch_id: The id that was looked up.
        """
        super().__init__(f"No observation batch has id {batch_id}.")
        self.batch_id = batch_id


class BatchNotOpenError(ValueError):
    """Raised when a batch was already reviewed, rejected or has expired."""

    def __init__(self, batch_id: int, status: BatchStatus) -> None:
        """Say what state the batch is in.

        Args:
            batch_id: The batch that was to be reviewed.
            status: Its current status.
        """
        super().__init__(
            f"Observation batch {batch_id} is {status.value} and can't be reviewed. "
            "Submit a new batch from fresh evidence."
        )
        self.batch_id = batch_id
        self.status = status


class ReviewsMissingError(ValueError):
    """Raised when a review leaves some proposals undecided."""

    def __init__(self, batch_id: int, proposal_ids: list[int]) -> None:
        """List the proposals that still need a decision.

        Args:
            batch_id: The batch being reviewed.
            proposal_ids: Proposals with no decision.
        """
        listed = ", ".join(str(proposal_id) for proposal_id in proposal_ids)
        super().__init__(
            f"Observation batch {batch_id} still needs a decision on proposals: "
            f"{listed}. Confirm, correct or reject each one."
        )
        self.batch_id = batch_id
        self.proposal_ids = proposal_ids


class InvalidReviewError(ValueError):
    """Raised when a review can't be applied as written."""


def _row_to_proposal(row: Any) -> ObservationProposal:
    """Build an ObservationProposal from a database row.

    Args:
        row: A row from the proposals SELECT statement.

    Returns:
        The proposal.
    """
    decided_state = row["decided_state"]
    return ObservationProposal(
        id=row["id"],
        batch_id=row["batch_id"],
        candidate_name=row["candidate_name"],
        item_id=row["item_id"],
        state=StockState(row["state"]),
        confidence=row["confidence"],
        notes=row["notes"],
        decision=ProposalDecision(row["decision"]),
        decided_item_id=row["decided_item_id"],
        decided_state=StockState(decided_state) if decided_state else None,
        observation_id=row["observation_id"],
    )


def _row_to_batch(row: Any, proposals: list[ObservationProposal]) -> ObservationBatch:
    """Build an ObservationBatch from its row and its proposals.

    Args:
        row: A row from one of the batch SELECT statements.
        proposals: The batch's proposals.

    Returns:
        The batch.
    """
    resolved_at = row["resolved_at"]
    return ObservationBatch(
        id=row["id"],
        status=BatchStatus(row["status"]),
        source=ObservationSource(row["source"]),
        submitted_by=row["submitted_by"],
        evidence_ref=row["evidence_ref"],
        notes=row["notes"],
        observed_at=dt.datetime.fromisoformat(row["observed_at"]),
        created_at=dt.datetime.fromisoformat(row["created_at"]),
        expires_at=dt.datetime.fromisoformat(row["expires_at"]),
        resolved_at=dt.datetime.fromisoformat(resolved_at) if resolved_at else None,
        resolved_by=row["resolved_by"],
        proposals=proposals,
    )


class ObservationBatches:
    """Submit proposed observations and apply a member's review of them.

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
        self._needs = ReplenishmentNeeds(db_path, clock=clock)

    # -- reads ---------------------------------------------------------

    def get(self, batch_id: int) -> ObservationBatch | None:
        """Return one batch with its proposals.

        An open batch past its deadline is marked expired first.

        Args:
            batch_id: The batch's id.

        Returns:
            The batch, or None if there is none.
        """
        self._expire_overdue()
        with closing(get_connection(self._db_path)) as conn:
            return self._load(conn, batch_id)

    def list_batches(self, status: BatchStatus | None = None) -> list[ObservationBatch]:
        """List batches, oldest first.

        Open batches past their deadline are marked expired first.

        Args:
            status: Only batches in this status. Defaults to all.

        Returns:
            The matching batches with their proposals.
        """
        self._expire_overdue()
        with closing(get_connection(self._db_path)) as conn:
            rows = conn.execute(_SELECT_ALL_BATCHES).fetchall()
            return [
                _row_to_batch(row, self._proposals(conn, row["id"]))
                for row in rows
                if status is None or row["status"] == status.value
            ]

    # -- writes --------------------------------------------------------

    def submit(
        self,
        source: ObservationSource,
        proposals: Sequence[NewProposal],
        *,
        submitted_by: str,
        evidence_ref: str | None = None,
        notes: str = "",
        observed_at: dt.datetime | None = None,
    ) -> ObservationBatch:
        """Store a batch of proposed observations for review.

        Each proposal's name is matched to a household item by alias. A
        name that matches nothing is stored unmatched; it never creates an
        item. Nothing is written to the observation ledger.

        Args:
            source: What kind of evidence the proposals came from.
            proposals: What was proposed, 1 to ``MAX_PROPOSALS`` entries.
            submitted_by: Who or what submitted the batch.
            evidence_ref: A reference to the evidence (a photo id, say).
            notes: Context for the reviewer.
            observed_at: When the evidence was captured. Defaults to now.

        Returns:
            The stored, open batch.

        Raises:
            ValueError: If the batch is empty or too large, or its source
                is ``system``.
        """
        if not proposals:
            msg = "An observation batch needs at least one proposal."
            raise ValueError(msg)
        if len(proposals) > MAX_PROPOSALS:
            msg = f"An observation batch can hold at most {MAX_PROPOSALS} proposals."
            raise ValueError(msg)
        if source is ObservationSource.SYSTEM:
            msg = "A batch can't come from the system source; say what the evidence is."
            raise ValueError(msg)

        now = self._clock()
        matches = [self._items.resolve(proposal.name) for proposal in proposals]
        with closing(get_connection(self._db_path)) as conn:
            cursor = conn.execute(
                "INSERT INTO observation_batches"
                " (status, source, submitted_by, evidence_ref, notes, observed_at,"
                "  created_at, expires_at) VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    BatchStatus.OPEN.value,
                    source.value,
                    submitted_by,
                    evidence_ref,
                    notes,
                    (observed_at or now).astimezone(dt.UTC).isoformat(),
                    now.isoformat(),
                    (now + BATCH_TTL).isoformat(),
                ),
            )
            batch_id = cursor.lastrowid
            if batch_id is None:  # pragma: no cover - both backends return the id
                raise BatchNotFoundError(0)
            for proposal, match in zip(proposals, matches, strict=True):
                conn.execute(
                    "INSERT INTO observation_proposals"
                    " (batch_id, candidate_name, item_id, state, confidence, notes)"
                    " VALUES (?, ?, ?, ?, ?, ?)",
                    (
                        batch_id,
                        proposal.name,
                        match.id if match else None,
                        proposal.state.value,
                        proposal.confidence,
                        proposal.notes,
                    ),
                )
            conn.commit()
            return self._require(conn, batch_id)

    def resolve(
        self,
        batch_id: int,
        reviews: Sequence[ProposalReview],
        *,
        actor: str,
        accept_high_confidence: bool = False,
    ) -> ObservationBatch:
        """Apply a member's review of a batch, all at once.

        Every proposal needs a decision. A confirmed proposal becomes an
        observation carrying the batch's source, evidence and the
        proposal's confidence. A corrected proposal becomes the member's
        own statement. A rejected proposal writes nothing. If anything in
        the review is wrong, nothing is written and the batch stays open.
        The batch is closed in the same transaction that writes the
        observations, so of two reviews sent at once only one applies.

        Args:
            batch_id: The batch to review.
            reviews: The member's decisions.
            actor: The reviewing member.
            accept_high_confidence: Confirm every proposal the review
                leaves out, provided it matched an item and its confidence
                is at or above the review threshold.

        Returns:
            The batch after the review: ``confirmed``, or ``rejected`` when
            every proposal was rejected.

        Raises:
            BatchNotFoundError: If the batch does not exist.
            BatchNotOpenError: If it was already reviewed or has expired.
            ReviewsMissingError: If some proposals have no decision.
            InvalidReviewError: If a decision can't be applied.
        """
        batch = self._require_open(batch_id)
        decisions = self._complete_reviews(batch, reviews, accept_high_confidence)
        writes = [
            (proposal, review, self._observation_for(batch, proposal, review, actor))
            for proposal, review in decisions
        ]
        now = self._clock().isoformat()
        accepted = [new for _, _, new in writes if new is not None]
        status = BatchStatus.CONFIRMED if accepted else BatchStatus.REJECTED
        with closing(get_connection(self._db_path)) as conn:
            self._claim(conn, batch_id, status, actor, now)
            for proposal, review, new in writes:
                self._apply(conn, proposal, review, new)
            conn.commit()
            resolved = self._require(conn, batch_id)
        for item_id in sorted({new.item_id for new in accepted}):
            self._needs.sync_item(item_id)
        return resolved

    def reject(self, batch_id: int, *, actor: str) -> ObservationBatch:
        """Throw a whole batch away. Nothing is written to the ledger.

        Args:
            batch_id: The batch to reject.
            actor: The member rejecting it.

        Returns:
            The rejected batch.

        Raises:
            BatchNotFoundError: If the batch does not exist.
            BatchNotOpenError: If it was already reviewed or has expired.
        """
        self._require_open(batch_id)
        with closing(get_connection(self._db_path)) as conn:
            self._claim(
                conn, batch_id, BatchStatus.REJECTED, actor, self._clock().isoformat()
            )
            conn.execute(
                "UPDATE observation_proposals SET decision = ? WHERE batch_id = ?",
                (ProposalDecision.REJECTED.value, batch_id),
            )
            conn.commit()
            return self._require(conn, batch_id)

    # -- internals -----------------------------------------------------

    def _claim(
        self,
        conn: DatabaseConnection,
        batch_id: int,
        status: BatchStatus,
        actor: str,
        now: str,
    ) -> None:
        """Close an open batch as the first step of the write transaction.

        The update only matches a batch that is still open, and takes the
        row's write lock. Two reviews that both read the batch as open
        therefore can't both apply: the second finds no open row, and its
        transaction ends without writing anything.

        Args:
            conn: The write transaction's connection; the caller commits.
            batch_id: The batch being closed.
            status: The status it is closed with.
            actor: Who is closing it.
            now: The current time, ISO 8601.

        Raises:
            BatchNotOpenError: If another review or a reject got there first.
        """
        claimed = conn.execute(
            "UPDATE observation_batches"
            " SET status = ?, resolved_at = ?, resolved_by = ?"
            " WHERE id = ? AND status = ?",
            (status.value, now, actor, batch_id, BatchStatus.OPEN.value),
        )
        if claimed.rowcount:
            return
        current = self._require(conn, batch_id)
        raise BatchNotOpenError(batch_id, current.status)

    def _complete_reviews(
        self,
        batch: ObservationBatch,
        reviews: Sequence[ProposalReview],
        accept_high_confidence: bool,
    ) -> list[tuple[ObservationProposal, ProposalReview]]:
        """Pair every proposal with a decision, or say what is missing.

        Args:
            batch: The open batch.
            reviews: The member's decisions.
            accept_high_confidence: Fill in a confirmation for each
                undecided proposal that needs no individual review.

        Returns:
            Each proposal with its decision, in proposal order.

        Raises:
            InvalidReviewError: If a decision names a proposal outside the
                batch, or names one twice.
            ReviewsMissingError: If some proposals are left undecided.
        """
        decided = self._index_reviews(batch, reviews)
        if accept_high_confidence:
            decided.update(
                {
                    proposal.id: ProposalReview(
                        proposal_id=proposal.id, action=DecisionAction.CONFIRM
                    )
                    for proposal in batch.proposals
                    if proposal.id not in decided and not proposal.needs_review
                }
            )
        missing = [p.id for p in batch.proposals if p.id not in decided]
        if missing:
            raise ReviewsMissingError(batch.id, missing)
        return [(proposal, decided[proposal.id]) for proposal in batch.proposals]

    @staticmethod
    def _index_reviews(
        batch: ObservationBatch, reviews: Sequence[ProposalReview]
    ) -> dict[int, ProposalReview]:
        """Index a member's decisions by proposal, refusing strays and repeats.

        Args:
            batch: The open batch.
            reviews: The member's decisions.

        Returns:
            Each decision keyed by its proposal's id.

        Raises:
            InvalidReviewError: If a decision names a proposal outside the
                batch, or names one twice.
        """
        known = {proposal.id for proposal in batch.proposals}
        decided: dict[int, ProposalReview] = {}
        for review in reviews:
            if review.proposal_id not in known:
                msg = f"Proposal {review.proposal_id} is not in this batch."
                raise InvalidReviewError(msg)
            if review.proposal_id in decided:
                msg = f"Proposal {review.proposal_id} is decided more than once."
                raise InvalidReviewError(msg)
            decided[review.proposal_id] = review
        return decided

    def _observation_for(
        self,
        batch: ObservationBatch,
        proposal: ObservationProposal,
        review: ProposalReview,
        actor: str,
    ) -> NewObservation | None:
        """Work out the ledger row a decision calls for.

        Args:
            batch: The batch under review.
            proposal: The proposal.
            review: The member's decision about it.
            actor: The reviewing member.

        Returns:
            The observation to write, or None for a rejection.

        Raises:
            InvalidReviewError: If a confirmation has no matched item, or a
                correction names an item that does not exist.
        """
        if review.action is DecisionAction.REJECT:
            return None
        if review.action is DecisionAction.CONFIRM:
            if proposal.item_id is None:
                msg = (
                    f'Proposal {proposal.id} ("{proposal.candidate_name}") matched '
                    "no household item, so it can't be confirmed as it stands. "
                    "Correct it with the right item_id, or reject it."
                )
                raise InvalidReviewError(msg)
            return NewObservation(
                item_id=proposal.item_id,
                state=proposal.state,
                source=batch.source,
                actor=actor,
                confidence=proposal.confidence,
                observed_at=batch.observed_at,
                evidence_ref=batch.evidence_ref,
                notes=proposal.notes,
            )
        return self._correction(batch, proposal, review, actor)

    def _correction(
        self,
        batch: ObservationBatch,
        proposal: ObservationProposal,
        review: ProposalReview,
        actor: str,
    ) -> NewObservation:
        """Build the member's own statement that replaces a proposal.

        Args:
            batch: The batch under review.
            proposal: The proposal being corrected.
            review: The correction.
            actor: The reviewing member.

        Returns:
            A human observation of the corrected item and state.

        Raises:
            InvalidReviewError: If no item can be determined, or the item
                named does not exist.
        """
        target_id = review.item_id if review.item_id is not None else proposal.item_id
        if target_id is None:
            msg = (
                f'Proposal {proposal.id} ("{proposal.candidate_name}") matched no '
                "household item. Correct it with the right item_id, or reject it."
            )
            raise InvalidReviewError(msg)
        target = self._items.get(target_id)
        if target is None:
            msg = f"No household item has id {target_id}."
            raise InvalidReviewError(msg)
        return NewObservation(
            item_id=target.merged_into_id or target.id,
            state=review.state or proposal.state,
            source=ObservationSource.HUMAN,
            actor=actor,
            observed_at=batch.observed_at,
            evidence_ref=batch.evidence_ref,
            notes=(
                f'Corrected from a {batch.source.value} proposal: "'
                f'{proposal.candidate_name}" was proposed as {proposal.state.value}.'
            ),
        )

    def _apply(
        self,
        conn: DatabaseConnection,
        proposal: ObservationProposal,
        review: ProposalReview,
        new: NewObservation | None,
    ) -> None:
        """Write one decision and its observation on the open transaction.

        Args:
            conn: The review's connection; the caller commits.
            proposal: The proposal.
            review: The decision about it.
            new: The observation to write, or None for a rejection.
        """
        decision = {
            DecisionAction.CONFIRM: ProposalDecision.CONFIRMED,
            DecisionAction.CORRECT: ProposalDecision.CORRECTED,
            DecisionAction.REJECT: ProposalDecision.REJECTED,
        }[review.action]
        observation_id = self._ledger.insert_into(conn, new) if new else None
        corrected = review.action is DecisionAction.CORRECT and new is not None
        conn.execute(
            "UPDATE observation_proposals SET decision = ?, decided_item_id = ?,"
            " decided_state = ?, observation_id = ? WHERE id = ?",
            (
                decision.value,
                new.item_id if corrected and new else None,
                new.state.value if corrected and new else None,
                observation_id,
                proposal.id,
            ),
        )

    def _expire_overdue(self) -> None:
        """Mark open batches past their deadline as expired."""
        with closing(get_connection(self._db_path)) as conn:
            conn.execute(
                "UPDATE observation_batches SET status = ?"
                " WHERE status = ? AND expires_at < ?",
                (
                    BatchStatus.EXPIRED.value,
                    BatchStatus.OPEN.value,
                    self._clock().isoformat(),
                ),
            )
            conn.commit()

    def _require_open(self, batch_id: int) -> ObservationBatch:
        """Fetch a batch that can still be reviewed.

        Args:
            batch_id: The batch's id.

        Returns:
            The open batch.

        Raises:
            BatchNotFoundError: If the batch does not exist.
            BatchNotOpenError: If it was already reviewed or has expired.
        """
        batch = self.get(batch_id)
        if batch is None:
            raise BatchNotFoundError(batch_id)
        if batch.status is not BatchStatus.OPEN:
            raise BatchNotOpenError(batch_id, batch.status)
        return batch

    def _require(self, conn: DatabaseConnection, batch_id: int) -> ObservationBatch:
        """Fetch one batch on an open connection, or raise.

        Args:
            conn: Open connection.
            batch_id: The batch's id.

        Returns:
            The batch.

        Raises:
            BatchNotFoundError: If the batch does not exist.
        """
        batch = self._load(conn, batch_id)
        if batch is None:
            raise BatchNotFoundError(batch_id)
        return batch

    def _load(self, conn: DatabaseConnection, batch_id: int) -> ObservationBatch | None:
        """Read one batch with its proposals.

        Args:
            conn: Open connection.
            batch_id: The batch's id.

        Returns:
            The batch, or None.
        """
        row = conn.execute(_SELECT_BATCH, (batch_id,)).fetchone()
        return _row_to_batch(row, self._proposals(conn, batch_id)) if row else None

    def _proposals(
        self, conn: DatabaseConnection, batch_id: int
    ) -> list[ObservationProposal]:
        """Read a batch's proposals in order.

        Args:
            conn: Open connection.
            batch_id: The batch's id.

        Returns:
            The proposals.
        """
        rows = conn.execute(_SELECT_PROPOSALS, (batch_id,)).fetchall()
        return [_row_to_proposal(row) for row in rows]
