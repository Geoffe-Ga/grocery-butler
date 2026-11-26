"""Tests for the operational metrics and the code that feeds them (ADR 0015)."""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
from typing import TYPE_CHECKING, Any
from unittest.mock import MagicMock

import pytest

from grocery_butler.claude_utils import create_message, extraction_failed
from grocery_butler.db import init_db
from grocery_butler.fulfillment.base import (
    AdapterDescription,
    Authorization,
    Capability,
    FulfillmentAdapter,
    HandoffResult,
    HandoffStatus,
)
from grocery_butler.fulfillment.registry import default_registry
from grocery_butler.fulfillment.runner import create_handoff
from grocery_butler.household_items import HouseholdItemStore
from grocery_butler.inventory_ledger import InventoryLedger
from grocery_butler.meal_parser import MealParser
from grocery_butler.metrics import (
    COUNTED_EVENTS,
    RECONCILIATION_GRACE,
    Counter,
    Metrics,
)
from grocery_butler.models import (
    DecisionAction,
    NeedReasonKind,
    NeedStatus,
    NewObservation,
    NewProposal,
    ObservationSource,
    ProposalReview,
    StockState,
)
from grocery_butler.observability import AUDIT_LOGGER, bound
from grocery_butler.observation_batches import ObservationBatches, capture_session
from grocery_butler.pantry_manager import PantryManager
from grocery_butler.replenishment import ReplenishmentNeeds
from grocery_butler.retries import RetriesExhaustedError
from grocery_butler.shopping_plans import ShoppingPlans

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from grocery_butler.models import ShoppingPlan

NOW = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.UTC)
ACTOR = "metrics-test"


class FakeClock:
    """A clock tests move by hand."""

    def __init__(self) -> None:
        """Start at NOW."""
        self.now = NOW

    def __call__(self) -> dt.datetime:
        """Return the current fake time."""
        return self.now


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


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a migrated temporary database path."""
    path = str(tmp_path / "metrics.db")
    init_db(path)
    return path


@pytest.fixture()
def clock() -> FakeClock:
    """Return a clock fixed at NOW."""
    return FakeClock()


@pytest.fixture()
def metrics(db_path: str, clock: FakeClock) -> Metrics:
    """Return the metrics on the fake clock."""
    return Metrics(db_path, clock=clock)


def _named(events: list[dict[str, Any]], name: str) -> list[dict[str, Any]]:
    """Return the events with one name."""
    return [event for event in events if event["event"] == name]


def _response(text: str) -> MagicMock:
    """Build a fake Messages API response carrying ``text``."""
    response = MagicMock()
    response.content = [MagicMock(text=text)]
    return response


class _Transport:
    """A fake model transport: plays back a script of replies and errors."""

    def __init__(self, *script: Any) -> None:
        """Queue the replies.

        Args:
            *script: Each entry is reply text, or an exception to raise.
        """
        self._script = list(script)
        self.calls = 0
        self.messages = self

    def create(self, **_request: Any) -> Any:
        """Return or raise the next scripted outcome."""
        self.calls += 1
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return _response(outcome)


class TestCounters:
    """Counters go up, by day, and never down."""

    def test_a_new_counter_is_zero(self, metrics: Metrics) -> None:
        """Nothing counted reads as zero, not as missing."""
        assert metrics.total(Counter.EXTRACTION_FAILURES) == 0
        assert metrics.total(Counter.ADAPTER_FAILURES, days=7) == 0

    def test_increment_returns_todays_count(self, metrics: Metrics) -> None:
        """Each increment reports the running count for the day."""
        assert metrics.increment(Counter.EXTRACTION_FAILURES) == 1
        assert metrics.increment(Counter.EXTRACTION_FAILURES) == 2
        assert metrics.increment(Counter.EXTRACTION_FAILURES, by=3) == 5
        assert metrics.total(Counter.EXTRACTION_FAILURES) == 5

    def test_counters_are_separate(self, metrics: Metrics) -> None:
        """One counter going up leaves the other alone."""
        metrics.increment(Counter.ADAPTER_FAILURES)

        assert metrics.total(Counter.ADAPTER_FAILURES) == 1
        assert metrics.total(Counter.EXTRACTION_FAILURES) == 0

    def test_days_are_counted_apart(self, metrics: Metrics, clock: FakeClock) -> None:
        """A window counts only its days; the total counts all."""
        clock.now = NOW - dt.timedelta(days=7)
        metrics.increment(Counter.EXTRACTION_FAILURES, by=4)
        clock.now = NOW - dt.timedelta(days=6)
        metrics.increment(Counter.EXTRACTION_FAILURES, by=2)
        clock.now = NOW
        metrics.increment(Counter.EXTRACTION_FAILURES)

        assert metrics.total(Counter.EXTRACTION_FAILURES) == 7
        assert metrics.total(Counter.EXTRACTION_FAILURES, days=7) == 3
        assert metrics.total(Counter.EXTRACTION_FAILURES, days=1) == 1

    @pytest.mark.parametrize("by", [0, -1])
    def test_a_counter_never_goes_down(self, metrics: Metrics, by: int) -> None:
        """Adding nothing or less is refused."""
        with pytest.raises(ValueError, match="only goes up"):
            metrics.increment(Counter.EXTRACTION_FAILURES, by=by)

        assert metrics.total(Counter.EXTRACTION_FAILURES) == 0

    def test_count_event_maps_events_to_counters(self, metrics: Metrics) -> None:
        """The two counted events advance their counters; others do nothing."""
        metrics.count_event("extraction.failed")
        metrics.count_event("adapter.failed")
        metrics.count_event("adapter.failed")
        metrics.count_event("http.request")

        assert metrics.total(Counter.EXTRACTION_FAILURES) == 1
        assert metrics.total(Counter.ADAPTER_FAILURES) == 2
        assert set(COUNTED_EVENTS.values()) == set(Counter)


class TestExtractionFailures:
    """A model reply that can't be used is audited and counted."""

    def test_the_event_names_the_stage_and_nothing_else(
        self, events: list[dict[str, Any]]
    ) -> None:
        """Only a stage and a reason are recorded, never content."""
        extraction_failed("meal_parse", "invalid_json")

        assert set(events[0]) == {"event", "at", "correlation_id", "stage", "reason"}
        assert events[0]["event"] == "extraction.failed"

    def test_unparseable_inventory_reply_is_counted_once_with_the_session_id(
        self, db_path: str, metrics: Metrics, events: list[dict[str, Any]]
    ) -> None:
        """Two bad replies are one failed extraction, tied to the session."""
        transport = _Transport("not json", "still not json")
        manager = PantryManager(db_path, anthropic_client=transport)

        with bound("capture-session-01", count=metrics.count_event):
            updates = manager.parse_inventory_intent("we're out of milk")

        failures = _named(events, "extraction.failed")
        assert updates == []
        assert transport.calls == 2
        assert len(failures) == 1
        assert failures[0]["correlation_id"] == "capture-session-01"
        assert failures[0]["stage"] == "inventory_update"
        assert failures[0]["reason"] == "invalid_json"
        assert metrics.total(Counter.EXTRACTION_FAILURES) == 1
        assert "milk" not in json.dumps(events)

    def test_a_failed_model_call_is_counted(
        self, db_path: str, metrics: Metrics, events: list[dict[str, Any]]
    ) -> None:
        """A call that errors for good is an extraction failure too."""
        transport = _Transport(ValueError("bad request"))
        manager = PantryManager(db_path, anthropic_client=transport)

        with bound(None, count=metrics.count_event):
            updates = manager.parse_inventory_intent("we're out of milk")

        assert updates == []
        assert _named(events, "extraction.failed")[0]["reason"] == "call_failed"
        assert metrics.total(Counter.EXTRACTION_FAILURES) == 1

    def test_a_good_reply_counts_nothing(
        self, db_path: str, metrics: Metrics, events: list[dict[str, Any]]
    ) -> None:
        """Success leaves the counter alone."""
        reply = json.dumps(
            [{"ingredient": "milk", "new_status": "out", "confidence": 0.95}]
        )
        manager = PantryManager(db_path, anthropic_client=_Transport(reply))

        with bound(None, count=metrics.count_event):
            updates = manager.parse_inventory_intent("we're out of milk")

        assert len(updates) == 1
        assert _named(events, "extraction.failed") == []
        assert metrics.total(Counter.EXTRACTION_FAILURES) == 0

    def test_meal_parse_failures_are_counted(
        self, db_path: str, metrics: Metrics, events: list[dict[str, Any]]
    ) -> None:
        """A meal that can't be decomposed falls back to a stub and is counted."""
        from grocery_butler.recipe_store import RecipeStore

        transport = _Transport("not json", "nor this")
        parser = MealParser(RecipeStore(db_path), anthropic_client=transport)

        with bound(None, count=metrics.count_event):
            meals = parser.parse_meals(["mystery stew"])

        failures = _named(events, "extraction.failed")
        assert len(meals) == 1
        assert [f["stage"] for f in failures] == ["meal_parse"]
        assert failures[0]["reason"] == "invalid_json"
        assert metrics.total(Counter.EXTRACTION_FAILURES) == 1

    def test_meal_parse_call_failure_is_counted(
        self, db_path: str, metrics: Metrics, events: list[dict[str, Any]]
    ) -> None:
        """A model call that errors for good is counted for meals too."""
        from grocery_butler.recipe_store import RecipeStore

        parser = MealParser(
            RecipeStore(db_path), anthropic_client=_Transport(ValueError("no"))
        )

        with bound(None, count=metrics.count_event):
            parser.parse_meals(["mystery stew"])

        assert _named(events, "extraction.failed")[0]["reason"] == "call_failed"
        assert metrics.total(Counter.EXTRACTION_FAILURES) == 1


class TestModelRetries:
    """A model call retries a passing fault, with a fake transport."""

    def test_second_try_succeeds_and_both_attempts_are_logged(
        self, events: list[dict[str, Any]]
    ) -> None:
        """A timeout, then a reply: the caller gets the reply."""
        transport = _Transport(TimeoutError("slow"), "hello")
        slept: list[float] = []

        response = create_message(
            transport, what="model.test", sleep=slept.append, model="m", messages=[]
        )

        attempts = _named(events, "call.attempt")
        assert response.content[0].text == "hello"
        assert transport.calls == 2
        assert slept == [0.5]
        assert [(a["attempt"], a["outcome"]) for a in attempts] == [
            (1, "failed"),
            (2, "ok"),
        ]
        assert {a["what"] for a in attempts} == {"model.test"}

    def test_three_timeouts_give_up(self, events: list[dict[str, Any]]) -> None:
        """The budget is three tries; then the failure is reported."""
        transport = _Transport(*[TimeoutError("slow")] * 3)

        with pytest.raises(RetriesExhaustedError):
            create_message(
                transport, what="model.test", sleep=lambda _s: None, messages=[]
            )

        assert transport.calls == 3
        assert len(_named(events, "call.attempt")) == 3

    def test_a_bad_request_is_not_retried(self) -> None:
        """A permanent error is raised at once."""
        transport = _Transport(ValueError("bad"))

        with pytest.raises(ValueError, match="bad"):
            create_message(transport, what="model.test", messages=[])

        assert transport.calls == 1

    def test_the_sdk_client_leaves_retrying_to_us(self) -> None:
        """The real client is built with its own retries off."""
        from grocery_butler.claude_utils import make_anthropic_client

        client: Any = make_anthropic_client("test-" + os.urandom(4).hex())

        assert client is not None
        assert client.max_retries == 0


class _ScriptedAdapter(FulfillmentAdapter):
    """A fake adapter: plays back a script of results and errors."""

    def __init__(self, *script: Any) -> None:
        """Queue the outcomes.

        Args:
            *script: Each entry is a status to return, or an exception.
        """
        self._script = list(script)
        self.calls = 0

    def describe(self) -> AdapterDescription:
        """Describe the fake."""
        return AdapterDescription(
            name="fake",
            display_name="Fake",
            capabilities=frozenset({Capability.SHOPPING_LIST_LINK}),
            authorization=Authorization.NONE,
            data_freshness="none",
            human_required=True,
        )

    def create_handoff(
        self, plan: ShoppingPlan, *, constraints_acknowledged: bool = False
    ) -> HandoffResult:
        """Return or raise the next scripted outcome."""
        self.calls += 1
        outcome = self._script.pop(0)
        if isinstance(outcome, Exception):
            raise outcome
        return HandoffResult(
            adapter="fake",
            status=outcome,
            plan_version=plan.version,
            plan_content_hash=plan.content_hash,
            human_action="Open the link.",
        )


class TestAdapterFailures:
    """A handoff that fails is audited and counted; a passing fault retries."""

    @pytest.fixture()
    def plan(self, db_path: str) -> ShoppingPlan:
        """Build an empty plan version."""
        return ShoppingPlans(db_path).build([], actor=ACTOR)

    def test_a_retried_call_succeeds_on_the_second_try(
        self, plan: ShoppingPlan, metrics: Metrics, events: list[dict[str, Any]]
    ) -> None:
        """One dropped connection, then success: both attempts are logged."""
        adapter = _ScriptedAdapter(ConnectionError("reset"), HandoffStatus.CREATED)
        slept: list[float] = []

        with bound("handoff-0001", count=metrics.count_event):
            result = create_handoff(adapter, plan, sleep=slept.append)

        attempts = _named(events, "call.attempt")
        assert result.status is HandoffStatus.CREATED
        assert adapter.calls == 2
        assert slept == [0.5]
        assert [(a["what"], a["outcome"]) for a in attempts] == [
            ("adapter.fake", "failed"),
            ("adapter.fake", "ok"),
        ]
        assert _named(events, "adapter.handoff_created")[0]["plan_version"] == 1
        assert _named(events, "adapter.failed") == []
        assert metrics.total(Counter.ADAPTER_FAILURES) == 0

    def test_an_adapter_that_stays_down_is_counted(
        self, plan: ShoppingPlan, metrics: Metrics, events: list[dict[str, Any]]
    ) -> None:
        """Three failures: the error is raised and the failure counted once."""
        adapter = _ScriptedAdapter(*[ConnectionError("down")] * 3)

        with (
            bound("handoff-0002", count=metrics.count_event),
            pytest.raises(RetriesExhaustedError),
        ):
            create_handoff(adapter, plan, sleep=lambda _s: None)

        failed = _named(events, "adapter.failed")
        assert adapter.calls == 3
        assert len(failed) == 1
        assert failed[0]["adapter"] == "fake"
        assert failed[0]["error"] == "RetriesExhaustedError"
        assert failed[0]["correlation_id"] == "handoff-0002"
        assert metrics.total(Counter.ADAPTER_FAILURES) == 1

    def test_a_permanent_adapter_error_is_counted_without_retrying(
        self, plan: ShoppingPlan, metrics: Metrics, events: list[dict[str, Any]]
    ) -> None:
        """A bug in the adapter fails once, as itself."""
        adapter = _ScriptedAdapter(ValueError("bad line"))

        with (
            bound(None, count=metrics.count_event),
            pytest.raises(ValueError, match="bad line"),
        ):
            create_handoff(adapter, plan)

        assert adapter.calls == 1
        assert _named(events, "adapter.failed")[0]["error"] == "ValueError"
        assert metrics.total(Counter.ADAPTER_FAILURES) == 1

    def test_a_failed_status_is_counted(
        self, plan: ShoppingPlan, metrics: Metrics, events: list[dict[str, Any]]
    ) -> None:
        """An adapter that reports failure without raising is counted too."""
        adapter = _ScriptedAdapter(HandoffStatus.FAILED)

        with bound(None, count=metrics.count_event):
            result = create_handoff(adapter, plan)

        assert result.status is HandoffStatus.FAILED
        assert _named(events, "adapter.failed")[0]["error"] == "failed_status"
        assert _named(events, "adapter.handoff_created") == []
        assert metrics.total(Counter.ADAPTER_FAILURES) == 1

    def test_the_real_export_adapter_goes_through_the_runner(
        self, plan: ShoppingPlan, events: list[dict[str, Any]]
    ) -> None:
        """The list export is audited like any other adapter."""
        result = create_handoff(default_registry().get("export"), plan)

        assert result.status is HandoffStatus.CREATED
        assert _named(events, "adapter.handoff_created")[0]["adapter"] == "export"


class TestCorrectionRate:
    """The share of decided proposals a member had to correct."""

    def _batch(self, db_path: str, names: list[str]) -> ObservationBatches:
        """Submit one batch proposing each named item is low."""
        items = HouseholdItemStore(db_path)
        for name in names:
            items.resolve_or_create(name, actor=ACTOR)
        batches = ObservationBatches(db_path)
        batches.submit(
            ObservationSource.PHOTO,
            [NewProposal(name=n, state=StockState.LOW, confidence=0.9) for n in names],
            submitted_by=ACTOR,
        )
        return batches

    def test_nothing_decided_has_no_rate(self, db_path: str, metrics: Metrics) -> None:
        """Pending proposals don't count, and zero decided is not zero percent."""
        self._batch(db_path, ["oat milk"])

        assert metrics.correction_rate() == {"decided": 0, "corrected": 0, "rate": None}

    def test_a_correction_raises_the_rate(self, db_path: str, metrics: Metrics) -> None:
        """One corrected out of four decided is a quarter."""
        batches = self._batch(db_path, ["oat milk", "rye bread", "tofu", "miso"])
        proposals = batches.list_batches()[0].proposals
        reviews = [
            ProposalReview(
                proposal_id=proposals[0].id,
                action=DecisionAction.CORRECT,
                state=StockState.OUT,
            ),
            ProposalReview(proposal_id=proposals[1].id, action=DecisionAction.CONFIRM),
            ProposalReview(proposal_id=proposals[2].id, action=DecisionAction.CONFIRM),
            ProposalReview(proposal_id=proposals[3].id, action=DecisionAction.REJECT),
        ]

        batches.resolve(proposals[0].batch_id, reviews, actor=ACTOR)

        assert metrics.correction_rate() == {"decided": 4, "corrected": 1, "rate": 0.25}


class TestStaleInventory:
    """Items whose last observation is past its freshness window."""

    def test_an_observation_goes_stale_with_time(
        self, db_path: str, metrics: Metrics, clock: FakeClock
    ) -> None:
        """Fresh today, stale after the window has passed."""
        baseline = metrics.stale_inventory()
        item = HouseholdItemStore(db_path).resolve_or_create("oat milk", actor=ACTOR)
        InventoryLedger(db_path, clock=clock).record(
            NewObservation(
                item_id=item.id,
                state=StockState.ON_HAND,
                source=ObservationSource.HUMAN,
                actor=ACTOR,
            )
        )
        fresh = metrics.stale_inventory()

        clock.now = NOW + dt.timedelta(days=400)

        assert fresh == baseline
        assert metrics.stale_inventory() == baseline + 1


class TestReconciliationGaps:
    """Ordered needs nobody confirmed received within the grace period."""

    def _ordered(self, db_path: str, clock: FakeClock, name: str) -> int:
        """Create a requested need and move it to ordered; return its id.

        A manual request starts out approved, so it only has to be planned
        and ordered.
        """
        item = HouseholdItemStore(db_path).resolve_or_create(name, actor=ACTOR)
        needs = ReplenishmentNeeds(db_path, clock=clock)
        need = needs.add_reason(
            item.id, NeedReasonKind.MANUAL_REQUEST, source_key="test", actor=ACTOR
        )
        assert need.status is NeedStatus.APPROVED
        for status in (NeedStatus.PLANNED, NeedStatus.ORDERED):
            needs.transition(need.id, status, actor=ACTOR)
        return need.id

    def test_a_fresh_order_is_not_a_gap(
        self, db_path: str, metrics: Metrics, clock: FakeClock
    ) -> None:
        """Inside the grace period an ordered need is just waiting."""
        self._ordered(db_path, clock, "oat milk")
        clock.now = NOW + RECONCILIATION_GRACE - dt.timedelta(minutes=1)

        assert metrics.reconciliation_gaps() == 0

    def test_an_old_order_becomes_a_gap_until_received(
        self, db_path: str, metrics: Metrics, clock: FakeClock
    ) -> None:
        """Past the grace period it is a gap; marking it received closes it."""
        need_id = self._ordered(db_path, clock, "oat milk")
        self._ordered(db_path, clock, "rye bread")
        clock.now = NOW + RECONCILIATION_GRACE + dt.timedelta(minutes=1)

        before = metrics.reconciliation_gaps()
        ReplenishmentNeeds(db_path, clock=clock).transition(
            need_id, NeedStatus.RECEIVED, actor=ACTOR
        )

        assert before == 2
        assert metrics.reconciliation_gaps() == 1


class TestHandoffGaps:
    """A handoff nobody reconciled is a gap too."""

    def _handoff(self, db_path: str, clock: FakeClock) -> int:
        """Create a handoff attempt for a one-item plan; return its id."""
        from grocery_butler.fulfillment.handoffs import Handoffs
        from grocery_butler.fulfillment.instacart.adapter import InstacartAdapter
        from grocery_butler.fulfillment.instacart.transport import FakeTransport

        item = HouseholdItemStore(db_path).resolve_or_create("oat milk", actor=ACTOR)
        ReplenishmentNeeds(db_path, clock=clock).add_reason(
            item.id, NeedReasonKind.MANUAL_REQUEST, source_key="test", actor=ACTOR
        )
        plan = ShoppingPlans(db_path, clock=clock).build([], actor=ACTOR)
        adapters: dict[str, FulfillmentAdapter] = {
            "instacart": InstacartAdapter(
                FakeTransport.ok("https://example.invalid/l/gap"), clock=clock
            )
        }
        handoffs = Handoffs(db_path, adapters.__getitem__, clock=clock)
        draft = handoffs.draft(plan.version, "instacart", actor=ACTOR)
        handoffs.approve(draft.id, draft.payload_hash, actor=ACTOR)
        return handoffs.execute(draft.id, actor=ACTOR, idempotency_key="gap-0001").id

    def test_an_unreconciled_handoff_becomes_a_gap_after_the_grace_period(
        self, db_path: str, metrics: Metrics, clock: FakeClock
    ) -> None:
        """Fresh, it is waiting; past the grace period, it is a gap."""
        self._handoff(db_path, clock)
        fresh = metrics.reconciliation_gaps()

        clock.now = NOW + RECONCILIATION_GRACE + dt.timedelta(minutes=1)

        assert fresh == 0
        assert metrics.reconciliation_gaps() == 1

    def test_a_confirmed_receipt_closes_the_gap(
        self, db_path: str, metrics: Metrics, clock: FakeClock
    ) -> None:
        """An open receipt doesn't close it; a confirmed one does."""
        from grocery_butler.reconciliation import (
            LineAction,
            LineOutcome,
            LineReview,
            NewReceiptLine,
            Reconciliations,
        )

        attempt_id = self._handoff(db_path, clock)
        clock.now = NOW + RECONCILIATION_GRACE + dt.timedelta(minutes=1)
        receipts = Reconciliations(db_path, clock=clock)
        receipt = receipts.submit(
            [NewReceiptLine(name="oat milk", outcome=LineOutcome.RECEIVED)],
            submitted_by=ACTOR,
            attempt_id=attempt_id,
        )
        while_open = metrics.reconciliation_gaps()

        receipts.resolve(
            receipt.id,
            [LineReview(line_id=receipt.lines[0].id, action=LineAction.CONFIRM)],
            actor=ACTOR,
        )

        assert while_open == 1
        assert metrics.reconciliation_gaps() == 0


class TestSnapshot:
    """All five metrics in one read."""

    def test_snapshot_shape(self, metrics: Metrics) -> None:
        """Each metric is present under its name."""
        metrics.increment(Counter.EXTRACTION_FAILURES)

        snapshot = metrics.snapshot()

        assert set(snapshot) == {
            "as_of",
            "extraction_failures",
            "adapter_failures",
            "correction_rate",
            "stale_inventory",
            "reconciliation_gaps",
            "handoffs",
        }
        assert snapshot["as_of"] == NOW.isoformat()
        assert snapshot["extraction_failures"] == {"total": 1, "last_7_days": 1}
        assert snapshot["adapter_failures"] == {"total": 0, "last_7_days": 0}
        assert snapshot["reconciliation_gaps"] == 0
        assert json.loads(json.dumps(snapshot)) == snapshot


class TestCaptureSessions:
    """A batch's events share one capture session id across requests."""

    def test_submit_and_resolve_share_the_session(
        self, db_path: str, events: list[dict[str, Any]]
    ) -> None:
        """Two requests, two correlation ids, one capture session."""
        HouseholdItemStore(db_path).resolve_or_create("oat milk", actor=ACTOR)
        batches = ObservationBatches(db_path)
        with bound("request-submit-1"):
            batch = batches.submit(
                ObservationSource.PHOTO,
                [NewProposal(name="oat milk", state=StockState.LOW, confidence=0.9)],
                submitted_by=ACTOR,
                evidence_ref="photo-123",
            )
        with bound("request-resolve-1"):
            batches.resolve(
                batch.id,
                [
                    ProposalReview(
                        proposal_id=batch.proposals[0].id, action=DecisionAction.CONFIRM
                    )
                ],
                actor=ACTOR,
            )

        submitted = _named(events, "capture.batch_submitted")[0]
        resolved = _named(events, "capture.batch_resolved")[0]
        assert submitted["capture_session"] == capture_session(batch.id)
        assert resolved["capture_session"] == submitted["capture_session"]
        assert submitted["correlation_id"] == "request-submit-1"
        assert resolved["correlation_id"] == "request-resolve-1"
        assert submitted["proposals"] == 1
        assert (resolved["confirmed"], resolved["corrected"], resolved["rejected"]) == (
            1,
            0,
            0,
        )
        assert "photo-123" not in json.dumps(events)

    def test_reject_is_audited(
        self, db_path: str, events: list[dict[str, Any]]
    ) -> None:
        """Throwing a batch away is part of the session too."""
        batches = ObservationBatches(db_path)
        batch = batches.submit(
            ObservationSource.PHOTO,
            [NewProposal(name="something", state=StockState.LOW, confidence=0.4)],
            submitted_by=ACTOR,
        )

        batches.reject(batch.id, actor=ACTOR)

        rejected = _named(events, "capture.batch_rejected")[0]
        assert rejected["capture_session"] == f"batch-{batch.id}"
        assert rejected["proposals"] == 1
        assert rejected["actor"] == ACTOR
