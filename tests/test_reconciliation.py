"""Tests for receipts and reconciliation (ADR 0018, #150).

Receipts here are fixtures typed in by the tests. No model is called.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import threading
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest
from pydantic import ValidationError

from grocery_butler.db import get_connection, init_db
from grocery_butler.fulfillment.handoffs import AttemptStatus, Handoffs
from grocery_butler.fulfillment.instacart.adapter import InstacartAdapter
from grocery_butler.fulfillment.instacart.transport import FakeTransport
from grocery_butler.household_items import HouseholdItemStore
from grocery_butler.inventory_ledger import InventoryLedger
from grocery_butler.models import (
    HouseholdItemUpdate,
    ItemEventType,
    NeedReasonKind,
    NeedStatus,
    ObservationSource,
    StockState,
    Unit,
)
from grocery_butler.observability import AUDIT_LOGGER
from grocery_butler.reconciliation import (
    MAX_LINES,
    LineAction,
    LineDecision,
    LineOutcome,
    LineReview,
    NewReceiptLine,
    ReceiptInvalidError,
    ReceiptNotFoundError,
    ReceiptNotOpenError,
    ReceiptReviewError,
    ReceiptStatus,
    Reconciliations,
)
from grocery_butler.replenishment import ReplenishmentNeeds
from grocery_butler.shopping_plans import ShoppingPlans

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from grocery_butler.fulfillment.base import FulfillmentAdapter
    from grocery_butler.reconciliation import Receipt

NOW = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.UTC)
ACTOR = "geoff@test"


def _clock() -> dt.datetime:
    """Return a fixed time."""
    return NOW


class Household:
    """A household with planned needs for a few items."""

    def __init__(self, db_path: str, *planned: str) -> None:
        """Create the items, request each and build the plan.

        Args:
            db_path: A migrated database.
            *planned: Names of items that should have a planned need.
        """
        self.db_path = db_path
        self.items = HouseholdItemStore(db_path)
        self.needs = ReplenishmentNeeds(db_path, clock=_clock)
        self.ledger = InventoryLedger(db_path, clock=_clock)
        self.receipts = Reconciliations(db_path, clock=_clock)
        self.ids: dict[str, int] = {}
        for name in planned:
            self.ids[name] = self.items.resolve_or_create(name, actor=ACTOR).id
            self.needs.add_reason(
                self.ids[name],
                NeedReasonKind.MANUAL_REQUEST,
                source_key="test",
                actor=ACTOR,
            )
        self.plan = ShoppingPlans(db_path, clock=_clock).build([], actor=ACTOR)

    def item(self, name: str) -> int:
        """Create an item with no need; return its id."""
        self.ids[name] = self.items.resolve_or_create(name, actor=ACTOR).id
        return self.ids[name]

    def need_status(self, name: str) -> NeedStatus | None:
        """Return the status of the item's most recent need."""
        history = [n for n in self.needs.list_needs() if n.item_id == self.ids[name]]
        if history:
            return history[-1].status
        with closing(get_connection(self.db_path)) as conn:
            rows = conn.execute(
                "SELECT status FROM replenishment_needs WHERE item_id = ? ORDER BY id",
                (self.ids[name],),
            ).fetchall()
        return NeedStatus(rows[-1]["status"]) if rows else None

    def state(self, name: str) -> StockState:
        """Return what we believe about the item's stock."""
        return self.ledger.snapshot(self.ids[name]).state

    def submit(self, *lines: NewReceiptLine, **extra: Any) -> Receipt:
        """Submit a receipt."""
        return self.receipts.submit(list(lines), submitted_by=ACTOR, **extra)

    def confirm_all(self, receipt: Receipt, **per_line: Any) -> Receipt:
        """Confirm every line of a receipt."""
        reviews = [
            LineReview(line_id=line.id, action=LineAction.CONFIRM, **per_line)
            for line in receipt.lines
        ]
        return self.receipts.resolve(receipt.id, reviews, actor=ACTOR)

    def observation_count(self) -> int:
        """Count rows in the observation ledger."""
        with closing(get_connection(self.db_path)) as conn:
            return len(conn.execute("SELECT id FROM inventory_observations").fetchall())


def _line(name: str, outcome: LineOutcome, **extra: Any) -> NewReceiptLine:
    """Build a receipt line."""
    return NewReceiptLine(name=name, outcome=outcome, **extra)


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a migrated temporary database path."""
    path = str(tmp_path / "receipts.db")
    init_db(path)
    return path


@pytest.fixture()
def home(db_path: str) -> Household:
    """Return a household with carrots and oat milk on the plan."""
    return Household(db_path, "carrots", "oat milk")


class _ListHandler(logging.Handler):
    """Collect audit records as parsed JSON."""

    def __init__(self) -> None:
        """Start with no records."""
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Parse and keep one audit line."""
        self.events.append(json.loads(record.getMessage()))


@pytest.fixture()
def events() -> Iterator[list[dict[str, Any]]]:
    """Capture every audit event emitted during the test."""
    handler = _ListHandler()
    logger = logging.getLogger(AUDIT_LOGGER)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield handler.events
    logger.removeHandler(handler)
    logger.setLevel(previous)


class TestSubmit:
    """Submitting a receipt stores it for review and changes nothing else."""

    def test_a_receipt_is_stored_open_and_matched(self, home: Household) -> None:
        """Lines are matched to items by alias; the receipt waits for review."""
        receipt = home.submit(
            _line("Carrots", LineOutcome.RECEIVED, quantity=2, unit=Unit.LB),
            _line("OAT MILK", LineOutcome.ORDERED),
            evidence_ref="photo-77",
            notes="Saturday shop",
        )

        assert receipt.status is ReceiptStatus.OPEN
        assert receipt.submitted_by == ACTOR
        assert receipt.evidence_ref == "photo-77"
        assert receipt.purchased_at == NOW
        assert [line.item_id for line in receipt.lines] == [
            home.ids["carrots"],
            home.ids["oat milk"],
        ]
        assert [line.needs_review for line in receipt.lines] == [False, False]
        assert [line.decision for line in receipt.lines] == [LineDecision.PENDING] * 2
        assert (receipt.lines[0].quantity, receipt.lines[0].unit) == (2.0, Unit.LB)
        assert home.receipts.get(receipt.id) == receipt

    def test_submitting_changes_no_inventory_and_no_need(self, home: Household) -> None:
        """A receipt nobody has confirmed is only a proposal."""
        before = home.observation_count()

        home.submit(_line("carrots", LineOutcome.RECEIVED))

        assert home.observation_count() == before
        assert home.state("carrots") is StockState.UNKNOWN
        assert home.need_status("carrots") is NeedStatus.PLANNED

    def test_an_unknown_item_waits_in_review_and_creates_nothing(
        self, home: Household
    ) -> None:
        """Receipt wording that matches no item never makes one."""
        before = len(home.items.list_items())

        receipt = home.submit(_line("Dragonfruit", LineOutcome.RECEIVED))

        assert receipt.lines[0].item_id is None
        assert receipt.lines[0].needs_review is True
        assert len(home.items.list_items()) == before

    def test_a_substitution_always_needs_review(self, home: Household) -> None:
        """Even when both names match, a person decides if a substitute will do."""
        home.item("almond milk")

        receipt = home.submit(
            _line("oat milk", LineOutcome.SUBSTITUTED, substitute_name="almond milk")
        )

        line = receipt.lines[0]
        assert line.needs_review is True
        assert line.item_id == home.ids["oat milk"]
        assert line.substitute_item_id == home.ids["almond milk"]

    def test_purchased_at_can_be_given(self, home: Household) -> None:
        """The shop's own time is kept, in UTC."""
        when = dt.datetime(2026, 11, 25, 18, 30, tzinfo=dt.UTC)

        receipt = home.submit(_line("carrots", LineOutcome.RECEIVED), purchased_at=when)

        assert receipt.purchased_at == when

    def test_an_empty_receipt_is_refused(self, home: Household) -> None:
        """A receipt needs at least one line."""
        with pytest.raises(ReceiptInvalidError, match="at least one line"):
            home.submit()

    def test_too_many_lines_are_refused(self, home: Household) -> None:
        """There is a ceiling on what one receipt holds."""
        lines = [_line("carrots", LineOutcome.RECEIVED)] * (MAX_LINES + 1)

        with pytest.raises(ReceiptInvalidError, match=f"at most {MAX_LINES}"):
            home.submit(*lines)

    def test_an_unknown_attempt_is_refused(self, home: Household) -> None:
        """A receipt can't point at a handoff that doesn't exist."""
        with pytest.raises(ReceiptInvalidError, match="No handoff attempt"):
            home.submit(_line("carrots", LineOutcome.RECEIVED), attempt_id=999)

        assert home.receipts.list_receipts() == []

    @pytest.mark.parametrize(
        "fields",
        [
            {"name": "", "outcome": "received"},
            {"name": "carrots", "outcome": "delivered"},
            {"name": "carrots", "outcome": "received", "quantity": 0},
            {"name": "carrots", "outcome": "received", "unit": "armful"},
            {"name": "carrots", "outcome": "substituted"},
            {"name": "carrots", "outcome": "received", "substitute_name": "parsnips"},
        ],
    )
    def test_invalid_lines_are_refused(self, fields: dict[str, Any]) -> None:
        """A line must name an item and a real outcome, and substitute sensibly."""
        with pytest.raises(ValidationError):
            NewReceiptLine.model_validate(fields)


class TestOutcomes:
    """Ordered, received, substituted and refunded are four different things."""

    def test_received_puts_the_item_on_hand_from_a_receipt(
        self, home: Household
    ) -> None:
        """The plan had carrots; the receipt confirms them; they are on hand."""
        receipt = home.submit(
            _line("Organic Carrots", LineOutcome.RECEIVED, quantity=2, unit=Unit.LB),
            _line("oat milk", LineOutcome.RECEIVED),
            evidence_ref="photo-77",
        )
        carrots_line = receipt.lines[0]
        reviews = [
            LineReview(
                line_id=carrots_line.id,
                action=LineAction.CONFIRM,
                item_id=home.ids["carrots"],
            ),
            LineReview(line_id=receipt.lines[1].id, action=LineAction.CONFIRM),
        ]

        resolved = home.receipts.resolve(receipt.id, reviews, actor=ACTOR)

        snapshot = home.ledger.snapshot(home.ids["carrots"])
        assert resolved.status is ReceiptStatus.CONFIRMED
        assert resolved.resolved_by == ACTOR
        assert snapshot.state is StockState.ON_HAND
        assert snapshot.observation is not None
        assert snapshot.observation.source is ObservationSource.RECEIPT
        assert snapshot.observation.actor == ACTOR
        assert snapshot.observation.evidence_ref == "photo-77"
        assert (snapshot.observation.quantity, snapshot.observation.unit) == (2.0, "lb")
        assert resolved.lines[0].observation_id == snapshot.observation.id
        assert resolved.lines[0].item_id == home.ids["carrots"]
        assert home.need_status("carrots") is NeedStatus.RECEIVED

    def test_received_walks_the_need_through_ordered(self, home: Household) -> None:
        """Evidence of receipt is also evidence of an order; both steps are kept."""
        home.confirm_all(home.submit(_line("carrots", LineOutcome.RECEIVED)))

        need_id = home.ledger.snapshot(home.ids["carrots"]).item_id
        with closing(get_connection(home.db_path)) as conn:
            row = conn.execute(
                "SELECT id FROM replenishment_needs WHERE item_id = ?", (need_id,)
            ).fetchall()[-1]
        steps = [(e.to_status, e.note) for e in home.needs.history(row["id"])][-2:]
        assert [status for status, _ in steps] == [
            NeedStatus.ORDERED,
            NeedStatus.RECEIVED,
        ]
        assert all(note.startswith("Receipt ") for _, note in steps)

    def test_ordered_moves_the_need_and_leaves_inventory_alone(
        self, home: Household
    ) -> None:
        """An order confirmation is not an arrival."""
        before = home.observation_count()

        home.confirm_all(home.submit(_line("carrots", LineOutcome.ORDERED)))

        assert home.need_status("carrots") is NeedStatus.ORDERED
        assert home.state("carrots") is StockState.UNKNOWN
        assert home.observation_count() == before

    def test_refunded_makes_the_item_wanted_again(self, home: Household) -> None:
        """A refund means it didn't come; the need goes back to approved."""
        before = home.observation_count()

        home.confirm_all(home.submit(_line("carrots", LineOutcome.REFUNDED)))

        assert home.need_status("carrots") is NeedStatus.APPROVED
        assert home.state("carrots") is StockState.UNKNOWN
        assert home.observation_count() == before

    def test_a_substitute_that_will_do_closes_the_need(self, home: Household) -> None:
        """Almond milk came instead of oat milk, and that is fine."""
        home.item("almond milk")
        receipt = home.submit(
            _line("oat milk", LineOutcome.SUBSTITUTED, substitute_name="almond milk")
        )

        resolved = home.confirm_all(receipt, fulfils_need=True)

        assert home.state("almond milk") is StockState.ON_HAND
        assert home.state("oat milk") is StockState.UNKNOWN
        assert home.need_status("oat milk") is NeedStatus.RECEIVED
        assert resolved.lines[0].fulfils_need is True

    def test_a_substitute_that_wont_do_leaves_the_item_wanted(
        self, home: Household
    ) -> None:
        """The substitute is in the house, and oat milk is still needed."""
        home.item("almond milk")
        receipt = home.submit(
            _line("oat milk", LineOutcome.SUBSTITUTED, substitute_name="almond milk")
        )

        home.confirm_all(receipt, fulfils_need=False)

        assert home.state("almond milk") is StockState.ON_HAND
        assert home.need_status("oat milk") is NeedStatus.APPROVED

    def test_a_substitutes_amount_is_not_the_amount_asked_for(
        self, home: Household
    ) -> None:
        """The quantity on the line was for the original item, so it isn't copied."""
        home.item("almond milk")
        receipt = home.submit(
            _line(
                "oat milk",
                LineOutcome.SUBSTITUTED,
                substitute_name="almond milk",
                quantity=2,
                unit=Unit.L,
            )
        )

        home.confirm_all(receipt, fulfils_need=True)

        observation = home.ledger.snapshot(home.ids["almond milk"]).observation
        assert observation is not None
        assert (observation.quantity, observation.unit) == (None, None)

    def test_the_four_outcomes_are_distinguishable(self, db_path: str) -> None:
        """Each outcome leaves its own combination of need status and stock."""
        home = Household(db_path, "apples", "bread", "cheese", "dates")
        home.item("dried figs")
        receipt = home.submit(
            _line("apples", LineOutcome.ORDERED),
            _line("bread", LineOutcome.RECEIVED),
            _line("cheese", LineOutcome.REFUNDED),
            _line("dates", LineOutcome.SUBSTITUTED, substitute_name="dried figs"),
        )
        reviews = [
            LineReview(
                line_id=line.id,
                action=LineAction.CONFIRM,
                fulfils_need=(
                    True if line.outcome is LineOutcome.SUBSTITUTED else None
                ),
            )
            for line in receipt.lines
        ]

        resolved = home.receipts.resolve(receipt.id, reviews, actor=ACTOR)

        outcomes = {
            name: (home.need_status(name), home.state(name))
            for name in ("apples", "bread", "cheese", "dates")
        }
        assert outcomes == {
            "apples": (NeedStatus.ORDERED, StockState.UNKNOWN),
            "bread": (NeedStatus.RECEIVED, StockState.ON_HAND),
            "cheese": (NeedStatus.APPROVED, StockState.UNKNOWN),
            "dates": (NeedStatus.RECEIVED, StockState.UNKNOWN),
        }
        assert home.state("dried figs") is StockState.ON_HAND
        assert [line.outcome for line in resolved.lines] == [
            LineOutcome.ORDERED,
            LineOutcome.RECEIVED,
            LineOutcome.REFUNDED,
            LineOutcome.SUBSTITUTED,
        ]


class TestNeedsElsewhere:
    """A receipt meets a need wherever it stands."""

    def test_something_bought_without_a_need_is_just_on_hand(
        self, home: Household
    ) -> None:
        """An impulse buy is recorded and raises nothing."""
        home.item("chocolate")

        home.confirm_all(home.submit(_line("chocolate", LineOutcome.RECEIVED)))

        assert home.state("chocolate") is StockState.ON_HAND
        assert home.need_status("chocolate") is None

    def test_a_suggested_need_is_closed_by_a_receipt(self, home: Household) -> None:
        """Bought before anyone approved the suggestion: the need is received."""
        rice = home.item("rice")
        home.needs.add_reason(
            rice, NeedReasonKind.LOW_STOCK, source_key="inventory", actor="system"
        )
        assert home.need_status("rice") is NeedStatus.SUGGESTED

        home.confirm_all(home.submit(_line("rice", LineOutcome.RECEIVED)))

        assert home.need_status("rice") is NeedStatus.RECEIVED

    def test_a_refund_leaves_an_unplanned_need_where_it_is(
        self, home: Household
    ) -> None:
        """A need that never got as far as a plan has nothing to go back from."""
        rice = home.item("rice")
        home.needs.add_reason(
            rice, NeedReasonKind.LOW_STOCK, source_key="inventory", actor="system"
        )

        home.confirm_all(home.submit(_line("rice", LineOutcome.REFUNDED)))

        assert home.need_status("rice") is NeedStatus.SUGGESTED

    def test_an_ordered_need_can_then_be_received(self, home: Household) -> None:
        """Two receipts: the order confirmation, then the delivery."""
        home.confirm_all(home.submit(_line("carrots", LineOutcome.ORDERED)))

        home.confirm_all(home.submit(_line("carrots", LineOutcome.RECEIVED)))

        assert home.need_status("carrots") is NeedStatus.RECEIVED
        assert home.state("carrots") is StockState.ON_HAND


class TestReview:
    """A review is applied whole, or not at all."""

    def test_an_unknown_item_can_not_be_confirmed_as_it_stands(
        self, home: Household
    ) -> None:
        """It waits in review, and nothing changes."""
        receipt = home.submit(
            _line("Dragonfruit", LineOutcome.RECEIVED),
            _line("carrots", LineOutcome.RECEIVED),
        )
        before = home.observation_count()

        with pytest.raises(ReceiptReviewError, match="matched no household item"):
            home.confirm_all(receipt)

        still = home.receipts.get(receipt.id)
        assert still is not None
        assert still.status is ReceiptStatus.OPEN
        assert home.observation_count() == before
        assert home.need_status("carrots") is NeedStatus.PLANNED

    def test_an_unknown_item_can_be_named_by_the_member(self, home: Household) -> None:
        """The member says which item the receipt meant."""
        receipt = home.submit(_line("Org Carr 2LB", LineOutcome.RECEIVED))

        home.receipts.resolve(
            receipt.id,
            [
                LineReview(
                    line_id=receipt.lines[0].id,
                    action=LineAction.CONFIRM,
                    item_id=home.ids["carrots"],
                )
            ],
            actor=ACTOR,
        )

        assert home.state("carrots") is StockState.ON_HAND

    def test_a_skipped_line_changes_nothing(self, home: Household) -> None:
        """Skipping is a decision; the rest of the receipt still applies."""
        receipt = home.submit(
            _line("Dragonfruit", LineOutcome.RECEIVED),
            _line("carrots", LineOutcome.RECEIVED),
        )
        reviews = [
            LineReview(line_id=receipt.lines[0].id, action=LineAction.SKIP),
            LineReview(line_id=receipt.lines[1].id, action=LineAction.CONFIRM),
        ]

        resolved = home.receipts.resolve(receipt.id, reviews, actor=ACTOR)

        assert [line.decision for line in resolved.lines] == [
            LineDecision.SKIPPED,
            LineDecision.CONFIRMED,
        ]
        assert resolved.lines[0].observation_id is None
        assert home.state("carrots") is StockState.ON_HAND

    def test_a_substitution_needs_to_know_if_it_will_do(self, home: Household) -> None:
        """Without fulfils_need the substitution is not applied."""
        home.item("almond milk")
        receipt = home.submit(
            _line("oat milk", LineOutcome.SUBSTITUTED, substitute_name="almond milk")
        )

        with pytest.raises(ReceiptReviewError, match="fulfils_need true or false"):
            home.confirm_all(receipt)

        assert home.state("almond milk") is StockState.UNKNOWN

    def test_an_unknown_substitute_must_be_named(self, home: Household) -> None:
        """A substitute we don't know can't be put on hand until it is named."""
        receipt = home.submit(
            _line("oat milk", LineOutcome.SUBSTITUTED, substitute_name="Moo Juice")
        )

        with pytest.raises(ReceiptReviewError, match="Moo Juice"):
            home.confirm_all(receipt, fulfils_need=True)

        almond = home.item("almond milk")
        home.confirm_all(receipt, fulfils_need=True, substitute_item_id=almond)
        assert home.state("almond milk") is StockState.ON_HAND

    @pytest.mark.parametrize("field", ["item_id", "substitute_item_id"])
    def test_an_item_that_does_not_exist_is_refused(
        self, home: Household, field: str
    ) -> None:
        """A review can't point a line at a made-up item."""
        home.item("almond milk")
        receipt = home.submit(
            _line("oat milk", LineOutcome.SUBSTITUTED, substitute_name="almond milk")
        )

        with pytest.raises(ReceiptReviewError, match="no household item has id 99999"):
            home.confirm_all(receipt, fulfils_need=True, **{field: 99999})

    def test_every_line_needs_a_decision(self, home: Household) -> None:
        """A partial review is refused and names what is missing."""
        receipt = home.submit(
            _line("carrots", LineOutcome.RECEIVED),
            _line("oat milk", LineOutcome.RECEIVED),
        )
        partial = [LineReview(line_id=receipt.lines[0].id, action=LineAction.CONFIRM)]

        with pytest.raises(ReceiptReviewError, match=str(receipt.lines[1].id)):
            home.receipts.resolve(receipt.id, partial, actor=ACTOR)

        assert home.state("carrots") is StockState.UNKNOWN

    def test_a_line_decided_twice_is_refused(self, home: Household) -> None:
        """One decision per line."""
        receipt = home.submit(_line("carrots", LineOutcome.RECEIVED))
        twice = [
            LineReview(line_id=receipt.lines[0].id, action=LineAction.CONFIRM),
            LineReview(line_id=receipt.lines[0].id, action=LineAction.SKIP),
        ]

        with pytest.raises(ReceiptReviewError, match="two decisions"):
            home.receipts.resolve(receipt.id, twice, actor=ACTOR)

    def test_a_line_from_another_receipt_is_refused(self, home: Household) -> None:
        """A review only speaks for its own receipt."""
        first = home.submit(_line("carrots", LineOutcome.RECEIVED))
        second = home.submit(_line("oat milk", LineOutcome.RECEIVED))
        foreign = [LineReview(line_id=first.lines[0].id, action=LineAction.CONFIRM)]

        with pytest.raises(ReceiptReviewError, match="is not on receipt"):
            home.receipts.resolve(second.id, foreign, actor=ACTOR)

    def test_one_bad_line_means_nothing_is_written(self, home: Household) -> None:
        """The good line is not applied when another line is wrong."""
        receipt = home.submit(
            _line("carrots", LineOutcome.RECEIVED),
            _line("Dragonfruit", LineOutcome.RECEIVED),
        )
        before = home.observation_count()

        with pytest.raises(ReceiptReviewError):
            home.confirm_all(receipt)

        assert home.observation_count() == before
        assert home.state("carrots") is StockState.UNKNOWN
        assert home.need_status("carrots") is NeedStatus.PLANNED

    def test_a_receipt_is_resolved_once(self, home: Household) -> None:
        """A second review of the same receipt is refused."""
        receipt = home.submit(_line("carrots", LineOutcome.RECEIVED))
        home.confirm_all(receipt)

        with pytest.raises(ReceiptNotOpenError, match="already confirmed"):
            home.confirm_all(receipt)

    def test_reject_throws_the_receipt_away(self, home: Household) -> None:
        """A rejected receipt changes nothing and can't be resolved later."""
        receipt = home.submit(_line("carrots", LineOutcome.RECEIVED))

        rejected = home.receipts.reject(receipt.id, actor=ACTOR)

        assert rejected.status is ReceiptStatus.REJECTED
        assert [line.decision for line in rejected.lines] == [LineDecision.SKIPPED]
        assert home.state("carrots") is StockState.UNKNOWN
        with pytest.raises(ReceiptNotOpenError, match="already rejected"):
            home.confirm_all(receipt)
        with pytest.raises(ReceiptNotOpenError):
            home.receipts.reject(receipt.id, actor=ACTOR)

    def test_unknown_receipts(self, home: Household) -> None:
        """A missing id is reported by name."""
        assert home.receipts.get(999) is None
        with pytest.raises(ReceiptNotFoundError, match="No receipt has id 999"):
            home.receipts.resolve(999, [], actor=ACTOR)
        with pytest.raises(ReceiptNotFoundError):
            home.receipts.reject(999, actor=ACTOR)

    def test_two_reviews_at_once_apply_once(self, home: Household) -> None:
        """Of several simultaneous reviews, exactly one writes."""
        receipt = home.submit(_line("carrots", LineOutcome.RECEIVED))
        before = home.observation_count()
        barrier = threading.Barrier(4)
        results: list[Any] = []

        def review() -> None:
            barrier.wait()
            try:
                results.append(home.confirm_all(receipt))
            except ReceiptNotOpenError as err:
                results.append(err)

        threads = [threading.Thread(target=review) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        applied = [r for r in results if not isinstance(r, Exception)]
        assert len(results) == 4
        assert len(applied) == 1
        assert home.observation_count() == before + 1


UPC = "012345678905"


class TestVerifiedUpc:
    """A product code is learned only from a receipt line a member confirms.

    The code is the receipt's, the confirmation is a person's, and saving
    it is their choice on that line (ADR 0023).
    """

    def test_a_receipt_line_keeps_the_code_it_came_with(self, home: Household) -> None:
        """Submitting stores the code on the line and nowhere else."""
        receipt = home.submit(_line("carrots", LineOutcome.RECEIVED, upc=UPC))

        assert receipt.lines[0].upc == UPC
        assert home.items.get(home.ids["carrots"]).verified_upc is None

    @pytest.mark.parametrize(
        "code", ["12345", "01234567890x", "0123456789012", " 012345678905"]
    )
    def test_a_code_that_is_not_12_or_14_digits_is_refused(self, code: str) -> None:
        """A malformed code never reaches a receipt, so it can't be learned."""
        with pytest.raises(ValidationError, match="12 or 14 digits"):
            _line("carrots", LineOutcome.RECEIVED, upc=code)

    def test_confirming_without_asking_saves_nothing(self, home: Household) -> None:
        """Remembering the code is opt-in, line by line."""
        receipt = home.submit(_line("carrots", LineOutcome.RECEIVED, upc=UPC))

        home.confirm_all(receipt)

        assert home.items.get(home.ids["carrots"]).verified_upc is None

    def test_confirming_and_asking_saves_the_code_with_who_and_when(
        self, home: Household
    ) -> None:
        """The item records the code, the member and the time."""
        receipt = home.submit(_line("carrots", LineOutcome.RECEIVED, upc=UPC))

        home.confirm_all(receipt, remember_upc=True)

        item = home.items.get(home.ids["carrots"])
        assert item.verified_upc == UPC
        assert item.upc_verified_by == ACTOR
        assert item.upc_verified_at == NOW.isoformat()
        history = home.items.history(item.id)
        assert history[-1].event_type is ItemEventType.UPC_VERIFIED
        assert history[-1].detail == {
            "upc": UPC,
            "receipt_id": receipt.id,
            "line_id": receipt.lines[0].id,
        }

    def test_the_code_goes_on_the_item_the_member_named(self, home: Household) -> None:
        """A line the receipt worded oddly is saved against the corrected item."""
        receipt = home.submit(_line("ORG CARROT 2LB", LineOutcome.RECEIVED, upc=UPC))
        review = LineReview(
            line_id=receipt.lines[0].id,
            action=LineAction.CONFIRM,
            item_id=home.ids["carrots"],
            remember_upc=True,
        )

        home.receipts.resolve(receipt.id, [review], actor=ACTOR)

        assert home.items.get(home.ids["carrots"]).verified_upc == UPC

    @pytest.mark.parametrize(
        ("line", "reason"),
        [
            (_line("carrots", LineOutcome.RECEIVED), "has no product code"),
            (
                _line("carrots", LineOutcome.ORDERED, upc=UPC),
                "only a line that was received",
            ),
            (
                _line(
                    "carrots",
                    LineOutcome.SUBSTITUTED,
                    substitute_name="parsnips",
                    upc=UPC,
                ),
                "only a line that was received",
            ),
        ],
        ids=["no-code", "only-ordered", "substituted"],
    )
    def test_a_code_can_only_be_learned_from_a_received_line_that_has_one(
        self, home: Household, line: NewReceiptLine, reason: str
    ) -> None:
        """Anything else is refused, and the whole review changes nothing."""
        receipt = home.submit(line)
        review = LineReview(
            line_id=receipt.lines[0].id,
            action=LineAction.CONFIRM,
            remember_upc=True,
            substitute_item_id=None,
            fulfils_need=None,
        )

        with pytest.raises(ReceiptReviewError, match=reason):
            home.receipts.resolve(receipt.id, [review], actor=ACTOR)

        assert home.items.get(home.ids["carrots"]).verified_upc is None
        assert home.receipts.get(receipt.id).status is ReceiptStatus.OPEN

    def test_a_skipped_line_can_not_teach_a_code(self, home: Household) -> None:
        """Skipping means the member did not vouch for the line."""
        receipt = home.submit(_line("carrots", LineOutcome.RECEIVED, upc=UPC))
        review = LineReview(
            line_id=receipt.lines[0].id, action=LineAction.SKIP, remember_upc=True
        )

        with pytest.raises(ReceiptReviewError, match="confirm the line"):
            home.receipts.resolve(receipt.id, [review], actor=ACTOR)

    def test_a_member_can_clear_a_code(self, home: Household) -> None:
        """Packaging changes; a wrong code must be removable."""
        receipt = home.submit(_line("carrots", LineOutcome.RECEIVED, upc=UPC))
        home.confirm_all(receipt, remember_upc=True)

        cleared = home.items.update(
            home.ids["carrots"], HouseholdItemUpdate(verified_upc=None), actor=ACTOR
        )

        assert (cleared.verified_upc, cleared.upc_verified_by) == (None, None)
        assert cleared.upc_verified_at is None

    def test_a_code_can_not_be_typed_in(self) -> None:
        """The only way in is a confirmed receipt."""
        with pytest.raises(ValidationError):
            HouseholdItemUpdate.model_validate({"verified_upc": UPC})


class TestListing:
    """Receipts can be listed, and filtered by where they stand."""

    def test_list_and_filter(self, home: Household) -> None:
        """Open receipts are what still needs someone's attention."""
        open_one = home.submit(_line("carrots", LineOutcome.ORDERED))
        done = home.confirm_all(home.submit(_line("oat milk", LineOutcome.ORDERED)))

        assert [r.id for r in home.receipts.list_receipts()] == [open_one.id, done.id]
        assert [r.id for r in home.receipts.list_receipts(ReceiptStatus.OPEN)] == [
            open_one.id
        ]
        assert [r.id for r in home.receipts.list_receipts(ReceiptStatus.CONFIRMED)] == [
            done.id
        ]


class TestWithAHandoff:
    """A handoff alone changes nothing; a confirmed receipt does."""

    def _handoff(self, home: Household) -> int:
        """Create a handoff attempt for the household's plan; return its id."""
        adapters: dict[str, FulfillmentAdapter] = {
            "instacart": InstacartAdapter(
                FakeTransport.ok("https://example.invalid/l/abc"), clock=_clock
            )
        }
        handoffs = Handoffs(home.db_path, adapters.__getitem__, clock=_clock)
        draft = handoffs.draft(home.plan.version, "instacart", actor=ACTOR)
        handoffs.approve(draft.id, draft.payload_hash, actor=ACTOR)
        attempt = handoffs.execute(draft.id, actor=ACTOR, idempotency_key="k-000001")
        assert attempt.status is AttemptStatus.CREATED
        return attempt.id

    def test_a_created_handoff_without_a_receipt_leaves_inventory_unchanged(
        self, home: Household
    ) -> None:
        """A link is not an arrival: no observation, no ordered, no received."""
        before = home.observation_count()

        self._handoff(home)

        assert home.observation_count() == before
        assert home.state("carrots") is StockState.UNKNOWN
        assert home.need_status("carrots") is NeedStatus.PLANNED

    def test_an_unconfirmed_receipt_for_the_handoff_still_changes_nothing(
        self, home: Household
    ) -> None:
        """Even with a receipt on file, nothing moves until a member confirms."""
        attempt_id = self._handoff(home)
        before = home.observation_count()

        receipt = home.submit(
            _line("carrots", LineOutcome.RECEIVED), attempt_id=attempt_id
        )

        assert receipt.attempt_id == attempt_id
        assert receipt.plan_version == home.plan.version
        assert home.observation_count() == before
        assert home.state("carrots") is StockState.UNKNOWN

    def test_the_confirmed_receipt_is_what_changes_inventory(
        self, home: Household
    ) -> None:
        """The member's confirmation is the evidence."""
        attempt_id = self._handoff(home)
        receipt = home.submit(
            _line("carrots", LineOutcome.RECEIVED), attempt_id=attempt_id
        )

        resolved = home.confirm_all(receipt)

        assert resolved.attempt_id == attempt_id
        assert home.state("carrots") is StockState.ON_HAND
        assert home.need_status("carrots") is NeedStatus.RECEIVED
        assert home.need_status("oat milk") is NeedStatus.PLANNED


class TestAudit:
    """Each step leaves an audit event with counts, never contents."""

    def test_submit_and_resolve_are_audited(
        self, home: Household, events: list[dict[str, Any]]
    ) -> None:
        """The trail has how many lines, how many needed review, and who."""
        receipt = home.submit(
            _line("carrots", LineOutcome.RECEIVED),
            _line("Dragonfruit", LineOutcome.RECEIVED),
            evidence_ref="photo-77",
        )
        home.receipts.resolve(
            receipt.id,
            [
                LineReview(line_id=receipt.lines[0].id, action=LineAction.CONFIRM),
                LineReview(line_id=receipt.lines[1].id, action=LineAction.SKIP),
            ],
            actor=ACTOR,
        )

        submitted = next(e for e in events if e["event"] == "receipt.submitted")
        resolved = next(e for e in events if e["event"] == "receipt.resolved")
        assert (submitted["lines"], submitted["needs_review"]) == (2, 1)
        assert (resolved["confirmed"], resolved["skipped"]) == (1, 1)
        assert resolved["actor"] == ACTOR
        assert "Dragonfruit" not in json.dumps(events)
        assert "photo-77" not in json.dumps(events)

    def test_reject_is_audited(
        self, home: Household, events: list[dict[str, Any]]
    ) -> None:
        """Throwing a receipt away is recorded too."""
        receipt = home.submit(_line("carrots", LineOutcome.RECEIVED))

        home.receipts.reject(receipt.id, actor=ACTOR)

        assert [
            e["receipt_id"] for e in events if e["event"] == "receipt.rejected"
        ] == [receipt.id]


_PG_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.mark.skipif(not _PG_URL, reason="TEST_DATABASE_URL not set")
class TestPostgres:
    """Receipts behave the same on Postgres."""

    @pytest.fixture()
    def clean(self) -> Iterator[None]:
        """Remove this suite's receipts before and after."""
        init_db(_PG_URL)
        self._clean()
        yield
        self._clean()

    def _clean(self) -> None:
        """Delete this suite's receipts and their lines."""
        with closing(get_connection(_PG_URL)) as conn:
            conn.execute(
                "DELETE FROM receipt_lines WHERE receipt_id IN"
                " (SELECT id FROM receipts WHERE submitted_by = ?)",
                ("pg-receipt-test",),
            )
            conn.execute(
                "DELETE FROM receipts WHERE submitted_by = ?", ("pg-receipt-test",)
            )
            conn.commit()

    @pytest.mark.usefixtures("clean")
    def test_submit_and_resolve(self) -> None:
        """A received line puts a fresh item on hand through Postgres."""
        actor = "pg-receipt-test"
        name = f"pg receipt item {os.urandom(4).hex()}"
        item = HouseholdItemStore(_PG_URL).resolve_or_create(name, actor=actor)
        receipts = Reconciliations(_PG_URL)

        receipt = receipts.submit(
            [
                NewReceiptLine(
                    name=name, outcome=LineOutcome.RECEIVED, quantity=1, unit=Unit.EACH
                ),
                NewReceiptLine(name="pg nothing known", outcome=LineOutcome.ORDERED),
            ],
            submitted_by=actor,
        )
        resolved = receipts.resolve(
            receipt.id,
            [
                LineReview(line_id=receipt.lines[0].id, action=LineAction.CONFIRM),
                LineReview(line_id=receipt.lines[1].id, action=LineAction.SKIP),
            ],
            actor=actor,
        )

        assert resolved.status is ReceiptStatus.CONFIRMED
        assert resolved.lines[0].observation_id is not None
        assert resolved.lines[1].needs_review is True
        assert InventoryLedger(_PG_URL).snapshot(item.id).state is StockState.ON_HAND
        with pytest.raises(ReceiptNotOpenError):
            receipts.reject(receipt.id, actor=actor)
