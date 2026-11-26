"""Receipts: recording what actually arrived, on evidence (ADR 0018).

A handoff creates a link and nothing more. What the household really got is
known only from what it tells us afterwards: a receipt, typed in or read by
OpenClaw from a photo or an order confirmation. Each line says one of four
things happened to an item:

- ``ordered``: it was on the order. Nothing has arrived yet.
- ``received``: it arrived.
- ``substituted``: something else arrived in its place.
- ``refunded``: it was charged back or left off. It did not arrive.

A receipt is a proposal, like an observation batch. Nothing in it touches
the inventory or a need until a household member resolves it: confirming or
skipping every line, all at once or not at all. A line that names an item
we don't know waits in review and changes nothing.

This is provider-neutral. It knows about handoff attempts only as an id a
receipt may point at.
"""

from __future__ import annotations

import datetime as dt
from contextlib import closing
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel, Field, model_validator

from grocery_butler.db import get_connection
from grocery_butler.household_items import HouseholdItemStore, ItemNotFoundError
from grocery_butler.inventory_ledger import InventoryLedger, utc_now
from grocery_butler.models import (
    NeedStatus,
    NewObservation,
    ObservationSource,
    StockState,
    Unit,
)
from grocery_butler.observability import audit
from grocery_butler.replenishment import ReplenishmentNeeds

if TYPE_CHECKING:
    from collections.abc import Callable, Sequence

    from grocery_butler.db.adapter import DatabaseConnection

MAX_LINES = 200
RECONCILE_ACTOR_NOTE = "Receipt {receipt_id}"

_SELECT_RECEIPT = (
    "SELECT id, status, submitted_by, attempt_id, plan_version, evidence_ref,"
    " notes, purchased_at, created_at, resolved_at, resolved_by"
    " FROM receipts WHERE id = ?"
)
_SELECT_RECEIPTS = (
    "SELECT id, status, submitted_by, attempt_id, plan_version, evidence_ref,"
    " notes, purchased_at, created_at, resolved_at, resolved_by"
    " FROM receipts ORDER BY id"
)
_SELECT_LINES = (
    "SELECT id, receipt_id, name, outcome, quantity, unit, item_id,"
    " substitute_name, substitute_item_id, notes, decision, fulfils_need,"
    " observation_id FROM receipt_lines WHERE receipt_id = ? ORDER BY id"
)
_SELECT_ATTEMPT = "SELECT plan_version FROM fulfillment_attempts WHERE id = ?"

# The path a need walks toward a status when a receipt is the evidence.
_FORWARD = (
    NeedStatus.APPROVED,
    NeedStatus.PLANNED,
    NeedStatus.ORDERED,
    NeedStatus.RECEIVED,
)
_BEFORE_APPROVED = (NeedStatus.SUGGESTED, NeedStatus.DEFERRED)


class LineOutcome(StrEnum):
    """What a receipt says happened to one item."""

    ORDERED = "ordered"
    RECEIVED = "received"
    SUBSTITUTED = "substituted"
    REFUNDED = "refunded"


class ReceiptStatus(StrEnum):
    """Where a receipt stands."""

    OPEN = "open"
    CONFIRMED = "confirmed"
    REJECTED = "rejected"


class LineDecision(StrEnum):
    """What a member decided about one receipt line."""

    PENDING = "pending"
    CONFIRMED = "confirmed"
    SKIPPED = "skipped"


class LineAction(StrEnum):
    """What a member can do with a line when resolving a receipt."""

    CONFIRM = "confirm"
    SKIP = "skip"


class NewReceiptLine(BaseModel):
    """One line of a receipt being submitted.

    ``name`` is the item as the receipt words it; it is matched to a
    household item by alias and never creates one. A substituted line also
    says what arrived instead.
    """

    name: str = Field(min_length=1, max_length=200)
    outcome: LineOutcome
    quantity: float | None = Field(default=None, gt=0)
    unit: Unit | None = None
    substitute_name: str | None = Field(default=None, min_length=1, max_length=200)
    notes: str = Field(default="", max_length=500)

    @model_validator(mode="after")
    def _substitute_goes_with_substituted(self) -> NewReceiptLine:
        """Require a substitute name exactly when the line was substituted.

        Returns:
            The line.

        Raises:
            ValueError: If a substituted line names no substitute, or
                another line names one.
        """
        substituted = self.outcome is LineOutcome.SUBSTITUTED
        if substituted != (self.substitute_name is not None):
            msg = (
                "Give substitute_name for a substituted line, and only for a"
                " substituted line."
            )
            raise ValueError(msg)
        return self


class ReceiptLine(BaseModel):
    """A stored receipt line, its match and what was decided.

    ``needs_review`` is true when a member has to look before confirming:
    the name matched no item, the line is a substitution, or the substitute
    matched no item.
    """

    id: int
    receipt_id: int
    name: str
    outcome: LineOutcome
    quantity: float | None
    unit: Unit | None
    item_id: int | None
    substitute_name: str | None
    substitute_item_id: int | None
    notes: str
    decision: LineDecision
    fulfils_need: bool | None
    observation_id: int | None
    needs_review: bool


class Receipt(BaseModel):
    """What the household says happened after a shop, and its review."""

    id: int
    status: ReceiptStatus
    submitted_by: str
    attempt_id: int | None
    plan_version: int | None
    evidence_ref: str | None
    notes: str
    purchased_at: dt.datetime
    created_at: dt.datetime
    resolved_at: dt.datetime | None
    resolved_by: str | None
    lines: list[ReceiptLine]


class LineReview(BaseModel):
    """A member's decision about one receipt line.

    ``item_id`` names the item when the receipt's wording matched none, or
    the wrong one. For a substituted line, ``substitute_item_id`` names
    what arrived and ``fulfils_need`` says whether it will do: true closes
    the original item's need, false leaves the original still wanted.
    """

    line_id: int
    action: LineAction
    item_id: int | None = None
    substitute_item_id: int | None = None
    fulfils_need: bool | None = None


class ReceiptNotFoundError(LookupError):
    """Raised when a receipt id does not exist."""

    def __init__(self, receipt_id: int) -> None:
        """Name the missing receipt.

        Args:
            receipt_id: The id that was looked up.
        """
        self.receipt_id = receipt_id
        super().__init__(f"No receipt has id {receipt_id}.")


class ReceiptNotOpenError(ValueError):
    """Raised when a receipt was already resolved or rejected."""

    def __init__(self, receipt_id: int, status: ReceiptStatus) -> None:
        """Say what state the receipt is in.

        Args:
            receipt_id: The receipt.
            status: Its current status.
        """
        self.receipt_id = receipt_id
        self.status = status
        super().__init__(
            f"Receipt {receipt_id} is already {status.value}. Submit a new one"
            " to record anything further."
        )


class ReceiptInvalidError(ValueError):
    """Raised when a receipt can't be stored as submitted."""


class ReceiptReviewError(ValueError):
    """Raised when a review can't be applied. Nothing was changed."""


def _when(value: str | None) -> dt.datetime | None:
    """Parse a stored timestamp.

    Args:
        value: ISO 8601 text, or None.

    Returns:
        The time, or None.
    """
    return dt.datetime.fromisoformat(value) if value else None


def _row_to_line(row: Any) -> ReceiptLine:
    """Build a line from its row.

    Args:
        row: A ``receipt_lines`` row.

    Returns:
        The line, with ``needs_review`` worked out.
    """
    outcome = LineOutcome(row["outcome"])
    substituted = outcome is LineOutcome.SUBSTITUTED
    fulfils = row["fulfils_need"]
    return ReceiptLine(
        id=row["id"],
        receipt_id=row["receipt_id"],
        name=row["name"],
        outcome=outcome,
        quantity=row["quantity"],
        unit=Unit(row["unit"]) if row["unit"] else None,
        item_id=row["item_id"],
        substitute_name=row["substitute_name"],
        substitute_item_id=row["substitute_item_id"],
        notes=row["notes"],
        decision=LineDecision(row["decision"]),
        fulfils_need=None if fulfils is None else bool(fulfils),
        observation_id=row["observation_id"],
        needs_review=row["item_id"] is None or substituted,
    )


def _row_to_receipt(row: Any, lines: list[ReceiptLine]) -> Receipt:
    """Build a receipt from its row and lines.

    Args:
        row: A ``receipts`` row.
        lines: Its lines.

    Returns:
        The receipt.
    """
    return Receipt(
        id=row["id"],
        status=ReceiptStatus(row["status"]),
        submitted_by=row["submitted_by"],
        attempt_id=row["attempt_id"],
        plan_version=row["plan_version"],
        evidence_ref=row["evidence_ref"],
        notes=row["notes"],
        purchased_at=dt.datetime.fromisoformat(row["purchased_at"]),
        created_at=dt.datetime.fromisoformat(row["created_at"]),
        resolved_at=_when(row["resolved_at"]),
        resolved_by=row["resolved_by"],
        lines=lines,
    )


class _Effect(BaseModel):
    """What confirming one line will do, worked out before anything is written."""

    line_id: int
    decision: LineDecision
    observe_item_id: int | None = None
    need_item_id: int | None = None
    need_target: NeedStatus | None = None
    fulfils_need: bool | None = None
    item_id: int | None = None
    substitute_item_id: int | None = None


class Reconciliations:
    """Stores receipts and applies a member's review of them.

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
        self._items = HouseholdItemStore(db_path)
        self._ledger = InventoryLedger(db_path, clock=clock)
        self._needs = ReplenishmentNeeds(db_path, clock=clock)

    # ------------------------------------------------------------------
    # Reads
    # ------------------------------------------------------------------

    def _load(self, conn: DatabaseConnection, row: Any) -> Receipt:
        """Build a receipt with its lines.

        Args:
            conn: An open connection.
            row: The receipt's row.

        Returns:
            The receipt.
        """
        lines = conn.execute(_SELECT_LINES, (row["id"],)).fetchall()
        return _row_to_receipt(row, [_row_to_line(line) for line in lines])

    def _require(self, conn: DatabaseConnection, receipt_id: int) -> Receipt:
        """Fetch a receipt or raise.

        Args:
            conn: An open connection.
            receipt_id: The receipt's id.

        Returns:
            The receipt.

        Raises:
            ReceiptNotFoundError: If there is no such receipt.
        """
        row = conn.execute(_SELECT_RECEIPT, (receipt_id,)).fetchone()
        if row is None:
            raise ReceiptNotFoundError(receipt_id)
        return self._load(conn, row)

    def get(self, receipt_id: int) -> Receipt | None:
        """Return one receipt.

        Args:
            receipt_id: The receipt's id.

        Returns:
            The receipt, or None if there is no such id.
        """
        with closing(get_connection(self._db_path)) as conn:
            row = conn.execute(_SELECT_RECEIPT, (receipt_id,)).fetchone()
            return self._load(conn, row) if row is not None else None

    def list_receipts(self, status: ReceiptStatus | None = None) -> list[Receipt]:
        """List receipts, oldest first.

        Args:
            status: Only receipts in this status, if given.

        Returns:
            The receipts.
        """
        with closing(get_connection(self._db_path)) as conn:
            rows = conn.execute(_SELECT_RECEIPTS).fetchall()
            receipts = [self._load(conn, row) for row in rows]
        return [r for r in receipts if status is None or r.status is status]

    # ------------------------------------------------------------------
    # Submit
    # ------------------------------------------------------------------

    def _plan_version_of(
        self, conn: DatabaseConnection, attempt_id: int | None
    ) -> int | None:
        """Look up the plan version behind a handoff attempt.

        Args:
            conn: An open connection.
            attempt_id: The attempt the receipt reconciles, if any.

        Returns:
            The attempt's plan version, or None when no attempt was named.

        Raises:
            ReceiptInvalidError: If no attempt has that id.
        """
        if attempt_id is None:
            return None
        row = conn.execute(_SELECT_ATTEMPT, (attempt_id,)).fetchone()
        if row is None:
            msg = f"No handoff attempt has id {attempt_id}."
            raise ReceiptInvalidError(msg)
        return int(row["plan_version"])

    def _match(self, name: str | None) -> int | None:
        """Match receipt wording to a household item by alias.

        Args:
            name: The wording, or None.

        Returns:
            The canonical item's id, or None when nothing matches. No item
            is ever created from a receipt.
        """
        if name is None:
            return None
        item = self._items.resolve(name)
        return item.id if item is not None else None

    def submit(
        self,
        lines: Sequence[NewReceiptLine],
        *,
        submitted_by: str,
        attempt_id: int | None = None,
        evidence_ref: str | None = None,
        notes: str = "",
        purchased_at: dt.datetime | None = None,
    ) -> Receipt:
        """Store a receipt for review. Nothing else changes.

        Args:
            lines: The receipt's lines, 1 to ``MAX_LINES``.
            submitted_by: Who or what submitted it.
            attempt_id: The handoff attempt this receipt reconciles, if any.
            evidence_ref: A reference to the evidence (a photo id, say).
            notes: Context for the reviewer.
            purchased_at: When the shop happened. Defaults to now.

        Returns:
            The stored, open receipt, with each line matched where it can be.

        Raises:
            ReceiptInvalidError: If there are no lines, too many, or the
                attempt does not exist.
        """
        if not lines:
            msg = "A receipt needs at least one line."
            raise ReceiptInvalidError(msg)
        if len(lines) > MAX_LINES:
            msg = (
                f"A receipt holds at most {MAX_LINES} lines; this one has {len(lines)}."
            )
            raise ReceiptInvalidError(msg)
        now = self._clock()
        matches = [
            (self._match(entry.name), self._match(entry.substitute_name))
            for entry in lines
        ]
        with closing(get_connection(self._db_path)) as conn:
            plan_version = self._plan_version_of(conn, attempt_id)
            receipt_id = conn.execute(
                "INSERT INTO receipts (status, submitted_by, attempt_id,"
                " plan_version, evidence_ref, notes, purchased_at, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    ReceiptStatus.OPEN.value,
                    submitted_by,
                    attempt_id,
                    plan_version,
                    evidence_ref,
                    notes,
                    (purchased_at or now).astimezone(dt.UTC).isoformat(),
                    now.isoformat(),
                ),
            ).lastrowid
            for line, (item_id, substitute_id) in zip(lines, matches, strict=True):
                conn.execute(
                    "INSERT INTO receipt_lines (receipt_id, name, outcome, quantity,"
                    " unit, item_id, substitute_name, substitute_item_id, notes)"
                    " VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?)",
                    (
                        receipt_id,
                        line.name,
                        line.outcome.value,
                        line.quantity,
                        line.unit.value if line.unit else None,
                        item_id,
                        line.substitute_name,
                        substitute_id,
                        line.notes,
                    ),
                )
            conn.commit()
            receipt = self._require(conn, receipt_id or 0)
        audit(
            "receipt.submitted",
            receipt_id=receipt.id,
            attempt_id=attempt_id,
            lines=len(receipt.lines),
            needs_review=sum(1 for line in receipt.lines if line.needs_review),
            actor=submitted_by,
        )
        return receipt

    # ------------------------------------------------------------------
    # Resolve
    # ------------------------------------------------------------------

    def _existing_item(self, item_id: int, line: ReceiptLine, role: str) -> int:
        """Check that an item named in a review exists, and return its canonical id.

        Args:
            item_id: The id the review or the match gave.
            line: The line being reviewed.
            role: ``item`` or ``substitute``, for the message.

        Returns:
            The canonical item's id.

        Raises:
            ReceiptReviewError: If no item has that id.
        """
        try:
            return self._items.canonical(item_id).id
        except ItemNotFoundError as err:
            msg = f"Line {line.id}: no household item has id {item_id} ({role})."
            raise ReceiptReviewError(msg) from err

    def _item_for(self, line: ReceiptLine, review: LineReview) -> int:
        """Work out which item a confirmed line is about.

        Args:
            line: The line.
            review: The member's decision.

        Returns:
            The canonical item's id.

        Raises:
            ReceiptReviewError: If the line matched no item and the review
                names none.
        """
        chosen = review.item_id if review.item_id is not None else line.item_id
        if chosen is None:
            msg = (
                f'Line {line.id} ("{line.name}") matched no household item. Give'
                " its item_id, or skip the line."
            )
            raise ReceiptReviewError(msg)
        return self._existing_item(chosen, line, "item")

    def _substitution(
        self, line: ReceiptLine, review: LineReview, item_id: int
    ) -> _Effect:
        """Work out what confirming a substituted line will do.

        Args:
            line: The substituted line.
            review: The member's decision.
            item_id: The item that was wanted.

        Returns:
            The effect: the substitute is on hand, and the wanted item's
            need is closed or reopened as the member said.

        Raises:
            ReceiptReviewError: If the substitute is not identified, or the
                member did not say whether it fulfils the need.
        """
        chosen = (
            review.substitute_item_id
            if review.substitute_item_id is not None
            else line.substitute_item_id
        )
        if chosen is None:
            msg = (
                f'Line {line.id}: "{line.substitute_name}" matched no household'
                " item. Give substitute_item_id, or skip the line."
            )
            raise ReceiptReviewError(msg)
        if review.fulfils_need is None:
            msg = (
                f"Line {line.id} is a substitution. Say whether it will do:"
                " fulfils_need true or false."
            )
            raise ReceiptReviewError(msg)
        substitute_id = self._existing_item(chosen, line, "substitute")
        return _Effect(
            line_id=line.id,
            decision=LineDecision.CONFIRMED,
            observe_item_id=substitute_id,
            need_item_id=item_id,
            need_target=(
                NeedStatus.RECEIVED if review.fulfils_need else NeedStatus.APPROVED
            ),
            fulfils_need=review.fulfils_need,
            item_id=item_id,
            substitute_item_id=substitute_id,
        )

    def _effect(self, line: ReceiptLine, review: LineReview) -> _Effect:
        """Work out what one decision will do. Nothing is written.

        Args:
            line: The line.
            review: The member's decision about it.

        Returns:
            The effect to apply.

        Raises:
            ReceiptReviewError: If the decision can't be applied.
        """
        if review.action is LineAction.SKIP:
            return _Effect(line_id=line.id, decision=LineDecision.SKIPPED)
        item_id = self._item_for(line, review)
        if line.outcome is LineOutcome.SUBSTITUTED:
            return self._substitution(line, review, item_id)
        received = line.outcome is LineOutcome.RECEIVED
        target = {
            LineOutcome.RECEIVED: NeedStatus.RECEIVED,
            LineOutcome.ORDERED: NeedStatus.ORDERED,
            LineOutcome.REFUNDED: NeedStatus.APPROVED,
        }[line.outcome]
        return _Effect(
            line_id=line.id,
            decision=LineDecision.CONFIRMED,
            observe_item_id=item_id if received else None,
            need_item_id=item_id,
            need_target=target,
            item_id=item_id,
        )

    def _effects(
        self, receipt: Receipt, reviews: Sequence[LineReview]
    ) -> list[tuple[ReceiptLine, _Effect]]:
        """Check a whole review and work out every line's effect.

        Args:
            receipt: The open receipt.
            reviews: One decision per line.

        Returns:
            Each line with its effect, in line order.

        Raises:
            ReceiptReviewError: If a line has no decision or two, a
                decision names a line outside the receipt, or any decision
                can't be applied.
        """
        lines = {line.id: line for line in receipt.lines}
        seen: dict[int, LineReview] = {}
        for review in reviews:
            if review.line_id not in lines:
                msg = f"Line {review.line_id} is not on receipt {receipt.id}."
                raise ReceiptReviewError(msg)
            if review.line_id in seen:
                msg = f"Line {review.line_id} has two decisions. Send one."
                raise ReceiptReviewError(msg)
            seen[review.line_id] = review
        missing = sorted(set(lines) - set(seen))
        if missing:
            listed = ", ".join(str(line_id) for line_id in missing)
            msg = f"These lines have no decision: {listed}. Confirm or skip each."
            raise ReceiptReviewError(msg)
        return [(line, self._effect(line, seen[line.id])) for line in receipt.lines]

    def _claim(
        self,
        conn: DatabaseConnection,
        receipt_id: int,
        status: ReceiptStatus,
        actor: str,
    ) -> None:
        """Close an open receipt as the first step of the write transaction.

        The update matches only a receipt that is still open, so of two
        reviews sent at once only one applies.

        Args:
            conn: The write transaction's connection; the caller commits.
            receipt_id: The receipt being closed.
            status: The status it is closed with.
            actor: Who is closing it.

        Raises:
            ReceiptNotOpenError: If another review or a reject got there first.
        """
        claimed = conn.execute(
            "UPDATE receipts SET status = ?, resolved_at = ?, resolved_by = ?"
            " WHERE id = ? AND status = ?",
            (
                status.value,
                self._clock().isoformat(),
                actor,
                receipt_id,
                ReceiptStatus.OPEN.value,
            ),
        )
        if not claimed.rowcount:
            raise ReceiptNotOpenError(
                receipt_id, self._require(conn, receipt_id).status
            )

    def _write(
        self,
        conn: DatabaseConnection,
        receipt: Receipt,
        line: ReceiptLine,
        effect: _Effect,
        actor: str,
    ) -> None:
        """Write one line's decision, and its observation if it has one.

        Args:
            conn: The write transaction's connection; the caller commits.
            receipt: The receipt.
            line: The line.
            effect: What the decision does.
            actor: The reviewing member.
        """
        observation_id = None
        if effect.observe_item_id is not None:
            substituted = line.outcome is LineOutcome.SUBSTITUTED
            observation_id = self._ledger.insert_into(
                conn,
                NewObservation(
                    item_id=effect.observe_item_id,
                    state=StockState.ON_HAND,
                    source=ObservationSource.RECEIPT,
                    actor=actor,
                    # A substitute's amount is not the amount that was asked for.
                    quantity=None if substituted else line.quantity,
                    unit=None if substituted or not line.unit else line.unit.value,
                    confidence=1.0,
                    observed_at=receipt.purchased_at,
                    evidence_ref=receipt.evidence_ref,
                    notes=f"Receipt {receipt.id}, line {line.id}: {line.outcome.value}",
                ),
            )
        conn.execute(
            "UPDATE receipt_lines SET decision = ?, item_id = ?,"
            " substitute_item_id = ?, fulfils_need = ?, observation_id = ?"
            " WHERE id = ?",
            (
                effect.decision.value,
                effect.item_id if effect.item_id is not None else line.item_id,
                (
                    effect.substitute_item_id
                    if effect.substitute_item_id is not None
                    else line.substitute_item_id
                ),
                effect.fulfils_need,
                observation_id,
                line.id,
            ),
        )

    def _move_need(
        self, item_id: int, target: NeedStatus, actor: str, note: str
    ) -> None:
        """Move an item's open need to where the receipt's evidence puts it.

        ``received`` and ``ordered`` walk the need forward, one allowed
        step at a time, from wherever it is. ``approved`` means "still
        wanted": a planned or ordered need goes back to approved, and one
        not yet that far is left alone. An item with no open need was
        simply bought, and nothing moves.

        Args:
            item_id: The canonical item.
            target: Where the evidence says the need stands.
            actor: The reviewing member.
            note: What to record on each step.
        """
        need = self._needs.open_need_for_item(item_id)
        if need is None:
            return
        if target is NeedStatus.APPROVED:
            if need.status in (NeedStatus.PLANNED, NeedStatus.ORDERED):
                self._needs.transition(need.id, target, actor=actor, note=note)
            return
        if need.status in _BEFORE_APPROVED:
            self._needs.transition(need.id, NeedStatus.APPROVED, actor=actor, note=note)
            start = 1
        else:
            start = _FORWARD.index(need.status) + 1
        for status in _FORWARD[start : _FORWARD.index(target) + 1]:
            self._needs.transition(need.id, status, actor=actor, note=note)

    def resolve(
        self, receipt_id: int, reviews: Sequence[LineReview], *, actor: str
    ) -> Receipt:
        """Apply a member's review of a receipt, all at once.

        Every line needs a decision. A confirmed ``received`` line records
        the item as on hand, from a receipt, and marks its need received. A
        confirmed substitution records the substitute as on hand and closes
        or reopens the wanted item's need, as the member says. ``ordered``
        moves the need to ordered. ``refunded`` puts it back to approved. A
        skipped line changes nothing. If anything in the review is wrong,
        nothing is written and the receipt stays open.

        Args:
            receipt_id: The receipt.
            reviews: One decision per line.
            actor: The reviewing member.

        Returns:
            The confirmed receipt.

        Raises:
            ReceiptNotFoundError: If there is no such receipt.
            ReceiptNotOpenError: If it was already resolved or rejected.
            ReceiptReviewError: If the review can't be applied.
        """
        with closing(get_connection(self._db_path)) as conn:
            receipt = self._require(conn, receipt_id)
        if receipt.status is not ReceiptStatus.OPEN:
            raise ReceiptNotOpenError(receipt_id, receipt.status)
        effects = self._effects(receipt, reviews)
        with closing(get_connection(self._db_path)) as conn:
            self._claim(conn, receipt_id, ReceiptStatus.CONFIRMED, actor)
            for line, effect in effects:
                self._write(conn, receipt, line, effect, actor)
            conn.commit()
            resolved = self._require(conn, receipt_id)
        self._after_commit(receipt, [effect for _, effect in effects], actor)
        return resolved

    def _after_commit(
        self, receipt: Receipt, effects: list[_Effect], actor: str
    ) -> None:
        """Move needs and record the review, once the receipt is stored.

        Args:
            receipt: The receipt as it was before the review.
            effects: What each line's decision does.
            actor: The reviewing member.
        """
        note = RECONCILE_ACTOR_NOTE.format(receipt_id=receipt.id)
        for effect in effects:
            if effect.need_item_id is not None and effect.need_target is not None:
                self._move_need(effect.need_item_id, effect.need_target, actor, note)
        observed = {e.observe_item_id for e in effects if e.observe_item_id is not None}
        for item_id in sorted(observed):
            self._needs.sync_item(item_id)
        decisions = [effect.decision for effect in effects]
        audit(
            "receipt.resolved",
            receipt_id=receipt.id,
            attempt_id=receipt.attempt_id,
            confirmed=decisions.count(LineDecision.CONFIRMED),
            skipped=decisions.count(LineDecision.SKIPPED),
            actor=actor,
        )

    def reject(self, receipt_id: int, *, actor: str) -> Receipt:
        """Throw a whole receipt away. Nothing else changes.

        Args:
            receipt_id: The receipt.
            actor: The member rejecting it.

        Returns:
            The rejected receipt.

        Raises:
            ReceiptNotFoundError: If there is no such receipt.
            ReceiptNotOpenError: If it was already resolved or rejected.
        """
        with closing(get_connection(self._db_path)) as conn:
            self._require(conn, receipt_id)
            self._claim(conn, receipt_id, ReceiptStatus.REJECTED, actor)
            conn.execute(
                "UPDATE receipt_lines SET decision = ? WHERE receipt_id = ?",
                (LineDecision.SKIPPED.value, receipt_id),
            )
            conn.commit()
            rejected = self._require(conn, receipt_id)
        audit("receipt.rejected", receipt_id=receipt_id, actor=actor)
        return rejected
