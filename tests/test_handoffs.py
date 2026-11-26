"""Tests for approved, idempotent plan handoffs (ADR 0016, #148).

The provider is always a fake transport. Nothing here reaches a network.
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

from grocery_butler.approvals import (
    EnvelopeNotActionableError,
    EnvelopeNotFoundError,
    EnvelopeStatus,
    PayloadMismatchError,
    StaleEnvelopeError,
)
from grocery_butler.constraints import HouseholdRules
from grocery_butler.db import get_connection, init_db
from grocery_butler.fulfillment.export import ExportAdapter
from grocery_butler.fulfillment.handoffs import (
    ADAPTER_ERROR,
    PROVIDER_UNAVAILABLE,
    AttemptNotFoundError,
    AttemptStatus,
    HandoffNotAvailableError,
    Handoffs,
    KeyBelongsToAnotherHandoffError,
    PlanNotCurrentError,
)
from grocery_butler.fulfillment.instacart.adapter import InstacartAdapter
from grocery_butler.fulfillment.instacart.transport import (
    FakeTransport,
    TransportError,
)
from grocery_butler.household_items import HouseholdItemStore
from grocery_butler.models import (
    HardConstraintKind,
    Ingredient,
    IngredientCategory,
    NeedReasonKind,
    NeedStatus,
    ParsedMeal,
    PlanMeal,
    Unit,
)
from grocery_butler.observability import AUDIT_LOGGER
from grocery_butler.recipe_store import RecipeStore
from grocery_butler.replenishment import ReplenishmentNeeds
from grocery_butler.shopping_plans import PlanNotFoundError, ShoppingPlans

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from grocery_butler.fulfillment.base import FulfillmentAdapter

NOW = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.UTC)
ACTOR = "geoff@test"
LINK = "https://example.invalid/l/abc"


class FakeClock:
    """A clock tests move by hand."""

    def __init__(self) -> None:
        """Start at NOW."""
        self.now = NOW

    def __call__(self) -> dt.datetime:
        """Return the current fake time."""
        return self.now


class World:
    """A household with one plan, one fake provider and the handoff service."""

    def __init__(self, db_path: str, *outcomes: str | TransportError) -> None:
        """Build the plan from the issue's primary scenario.

        Args:
            db_path: A migrated database.
            *outcomes: What the fake provider returns or raises, in order.
        """
        self.db_path = db_path
        self.clock = FakeClock()
        self.transport = FakeTransport(*(outcomes or (LINK,)))
        self.slept: list[float] = []
        self.adapters: dict[str, FulfillmentAdapter] = {
            "instacart": InstacartAdapter(self.transport, clock=self.clock),
            "export": ExportAdapter(),
        }
        self.handoffs = Handoffs(
            db_path,
            self.adapters.__getitem__,
            clock=self.clock,
            sleep=self.slept.append,
        )
        HouseholdRules(db_path).add_hard(
            HardConstraintKind.ALLERGEN_EXCLUSION, "peanut", actor=ACTOR
        )
        recipe = RecipeStore(db_path).save_recipe(
            ParsedMeal(
                name="Roast Vegetables",
                servings=2,
                known_recipe=True,
                needs_confirmation=False,
                purchase_items=[
                    Ingredient(
                        ingredient="carrots",
                        quantity=2.0,
                        unit=Unit.LB,
                        category=IngredientCategory.PRODUCE,
                    ),
                    Ingredient(
                        ingredient="sweet potatoes",
                        quantity=3.0,
                        unit=Unit.EACH,
                        category=IngredientCategory.PRODUCE,
                    ),
                ],
                pantry_items=[],
            )
        )
        self.plans = ShoppingPlans(db_path, clock=self.clock)
        self.plan = self.plans.build([PlanMeal(recipe_id=recipe)], actor=ACTOR)

    def approved(self) -> int:
        """Draft and approve a handoff; return the envelope id."""
        draft = self.handoffs.draft(self.plan.version, "instacart", actor=ACTOR)
        self.handoffs.approve(draft.id, draft.payload_hash, actor=ACTOR)
        return draft.id

    def change_the_plan(self) -> None:
        """Add a need, so the plan's inputs no longer match it."""
        item = HouseholdItemStore(self.db_path).resolve_or_create(
            "oat milk", actor=ACTOR
        )
        ReplenishmentNeeds(self.db_path, clock=self.clock).add_reason(
            item.id, NeedReasonKind.MANUAL_REQUEST, source_key="test", actor=ACTOR
        )

    def attempt_rows(self) -> int:
        """Count stored attempts."""
        with closing(get_connection(self.db_path)) as conn:
            rows = conn.execute("SELECT id FROM fulfillment_attempts").fetchall()
        return len(rows)


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a migrated temporary database path."""
    path = str(tmp_path / "handoffs.db")
    init_db(path)
    return path


@pytest.fixture()
def world(db_path: str) -> World:
    """Return a household whose provider always returns a link."""
    return World(db_path)


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


class TestPrimaryScenario:
    """The scenario in the issue, end to end against the fake transport."""

    def test_one_request_one_attempt_one_link(self, world: World) -> None:
        """An approved plan becomes exactly one provider request and one link."""
        envelope_id = world.approved()

        attempt = world.handoffs.execute(
            envelope_id, actor=ACTOR, idempotency_key="confirm-0001"
        )

        assert attempt.status is AttemptStatus.CREATED
        assert attempt.external_url == LINK
        assert attempt.adapter == "instacart"
        assert attempt.plan_version == world.plan.version
        assert attempt.plan_content_hash == world.plan.content_hash
        assert attempt.link_expires_at == NOW + dt.timedelta(days=14)
        assert attempt.error_code is None
        assert "check out there yourself" in attempt.human_action
        assert len(world.transport.requests) == 1

    def test_the_request_carries_the_translated_lines(self, world: World) -> None:
        """2 lb carrots and 3 sweet potatoes arrive in Instacart's units."""
        world.handoffs.execute(
            world.approved(), actor=ACTOR, idempotency_key="confirm-0001"
        )

        sent = {
            item["name"]: item["line_item_measurements"]
            for item in json.loads(json.dumps(world.transport.requests[0].body()))[
                "line_items"
            ]
        }
        assert sent == {
            "carrots": [{"quantity": 2.0, "unit": "lb"}],
            "sweet potatoes": [{"quantity": 3.0, "unit": "each"}],
        }
        assert "peanut" not in json.dumps(world.transport.requests[0].body())

    def test_repeating_the_request_returns_the_same_attempt_and_sends_nothing(
        self, world: World
    ) -> None:
        """The idempotency key makes a retry free."""
        envelope_id = world.approved()
        first = world.handoffs.execute(
            envelope_id, actor=ACTOR, idempotency_key="confirm-0001"
        )

        again = world.handoffs.execute(
            envelope_id, actor=ACTOR, idempotency_key="confirm-0001"
        )

        assert again == first
        assert len(world.transport.requests) == 1
        assert world.attempt_rows() == 1

    def test_no_order_or_received_state_is_recorded(self, world: World) -> None:
        """A link is a handoff: no need becomes ordered or received."""
        needs = ReplenishmentNeeds(world.db_path)
        before = [(need.id, need.status) for need in needs.list_needs()]

        world.handoffs.execute(
            world.approved(), actor=ACTOR, idempotency_key="confirm-0001"
        )

        after = [(need.id, need.status) for need in needs.list_needs()]
        assert after == before
        assert all(
            status not in {NeedStatus.ORDERED, NeedStatus.RECEIVED}
            for _, status in after
        )

    def test_the_approval_is_spent(self, world: World) -> None:
        """An executed approval can't create a second link under a new key."""
        envelope_id = world.approved()
        world.handoffs.execute(envelope_id, actor=ACTOR, idempotency_key="confirm-0001")

        with pytest.raises(EnvelopeNotActionableError, match="executed"):
            world.handoffs.execute(
                envelope_id, actor=ACTOR, idempotency_key="confirm-0002"
            )

        assert len(world.transport.requests) == 1
        assert world.attempt_rows() == 1


class TestDraft:
    """A draft shows the member exactly what they would be approving."""

    def test_the_payload_lists_the_plan_and_what_wont_be_enforced(
        self, world: World
    ) -> None:
        """Lines, unenforced constraints and translation issues are all there."""
        draft = world.handoffs.draft(world.plan.version, "instacart", actor=ACTOR)
        payload = draft.payload

        assert draft.status is EnvelopeStatus.DRAFT
        assert payload["adapter"] == "instacart"
        assert payload["plan_version"] == world.plan.version
        assert payload["plan_content_hash"] == world.plan.content_hash
        assert {line["display_name"]: line["amounts"] for line in payload["lines"]} == {
            "Carrots": ["2 lb"],
            "Sweet Potatoes": ["3 each"],
        }
        assert [(c["kind"], c["value"]) for c in payload["unenforced_constraints"]] == [
            ("allergen_exclusion", "peanut"),
            ("allergen_exclusion", "peanut"),
        ]
        assert {i["kind"] for i in payload["issues"]} == {
            "hard_constraint_unrepresentable"
        }
        assert payload["approving_acknowledges_unenforced_constraints"] is True

    def test_drafting_sends_nothing(self, world: World) -> None:
        """A draft never reaches the provider."""
        world.handoffs.draft(world.plan.version, "instacart", actor=ACTOR)

        assert world.transport.requests == []
        assert world.attempt_rows() == 0

    def test_an_unknown_plan(self, world: World) -> None:
        """There is no draft for a plan that doesn't exist."""
        with pytest.raises(PlanNotFoundError):
            world.handoffs.draft(999, "instacart", actor=ACTOR)

    def test_an_unknown_adapter(self, world: World) -> None:
        """An adapter that isn't registered can't be drafted for."""
        with pytest.raises(LookupError):
            world.handoffs.draft(world.plan.version, "nonesuch", actor=ACTOR)

    def test_the_export_needs_no_approval(self, world: World) -> None:
        """An adapter that sends nothing outside is pointed at the export."""
        with pytest.raises(HandoffNotAvailableError, match="needs no approval"):
            world.handoffs.draft(world.plan.version, "export", actor=ACTOR)

    def test_an_empty_plan_can_not_be_handed_off(self, db_path: str) -> None:
        """Nothing to send, nothing to approve."""
        handoffs = Handoffs(
            db_path, {"instacart": InstacartAdapter(FakeTransport.ok(LINK))}.__getitem__
        )
        empty = ShoppingPlans(db_path).build([], actor=ACTOR)

        with pytest.raises(HandoffNotAvailableError, match="no lines"):
            handoffs.draft(empty.version, "instacart", actor=ACTOR)

    def test_a_stale_plan_can_not_be_drafted(self, world: World) -> None:
        """Once the needs change, the old plan is not offered for handoff."""
        world.change_the_plan()

        with pytest.raises(PlanNotCurrentError, match="Build a new plan"):
            world.handoffs.draft(world.plan.version, "instacart", actor=ACTOR)


class TestApprove:
    """Approval is bound to exactly what was shown."""

    def test_the_wrong_hash_is_refused(self, world: World) -> None:
        """A member can't approve content they weren't shown."""
        draft = world.handoffs.draft(world.plan.version, "instacart", actor=ACTOR)

        with pytest.raises(PayloadMismatchError):
            world.handoffs.approve(draft.id, "0" * 64, actor=ACTOR)

    def test_a_changed_plan_can_not_be_approved(self, world: World) -> None:
        """Modifying the plan's inputs invalidates the draft."""
        draft = world.handoffs.draft(world.plan.version, "instacart", actor=ACTOR)
        world.change_the_plan()

        with pytest.raises(StaleEnvelopeError):
            world.handoffs.approve(draft.id, draft.payload_hash, actor=ACTOR)

    def test_an_unknown_envelope(self, world: World) -> None:
        """There is nothing to approve under an id that doesn't exist."""
        with pytest.raises(EnvelopeNotFoundError):
            world.handoffs.approve(999, "0" * 64, actor=ACTOR)

    def test_an_envelope_for_something_else_is_not_a_handoff(
        self, world: World
    ) -> None:
        """An approval of another kind can't be used to hand a plan off."""
        from grocery_butler.approvals import ApprovalEnvelopes

        other = ApprovalEnvelopes(world.db_path, clock=world.clock).draft(
            kind="something_else",
            payload={"x": 1},
            subject_type="shopping_plan",
            subject_id=world.plan.id,
            subject_version=world.plan.version,
            subject_fingerprint=world.plan.content_hash,
            created_by=ACTOR,
        )

        with pytest.raises(HandoffNotAvailableError, match="not a shopping plan"):
            world.handoffs.approve(other.id, other.payload_hash, actor=ACTOR)
        with pytest.raises(HandoffNotAvailableError):
            world.handoffs.execute(other.id, actor=ACTOR, idempotency_key="k-000001")

    def test_reject_closes_the_draft(self, world: World) -> None:
        """A rejected handoff can't be executed and sends nothing."""
        draft = world.handoffs.draft(world.plan.version, "instacart", actor=ACTOR)

        rejected = world.handoffs.reject(draft.id, actor=ACTOR, note="not today")

        assert rejected.status is EnvelopeStatus.REJECTED
        with pytest.raises(EnvelopeNotActionableError):
            world.handoffs.execute(draft.id, actor=ACTOR, idempotency_key="k-000001")
        assert world.transport.requests == []


class TestNegativeCases:
    """Each negative case in the issue: the stated outcome and no stray request."""

    def test_unapproved_is_rejected_without_a_request(self, world: World) -> None:
        """A draft nobody approved can't be executed."""
        draft = world.handoffs.draft(world.plan.version, "instacart", actor=ACTOR)

        with pytest.raises(EnvelopeNotActionableError, match="draft"):
            world.handoffs.execute(draft.id, actor=ACTOR, idempotency_key="k-000001")

        assert world.transport.requests == []
        assert world.attempt_rows() == 0

    def test_expired_approval_is_rejected_without_a_request(self, world: World) -> None:
        """Thirty minutes after drafting, the approval is gone."""
        envelope_id = world.approved()
        world.clock.now = NOW + dt.timedelta(minutes=31)

        with pytest.raises(EnvelopeNotActionableError, match="expired"):
            world.handoffs.execute(envelope_id, actor=ACTOR, idempotency_key="k-000001")

        assert world.transport.requests == []
        assert world.attempt_rows() == 0

    def test_changed_plan_is_rejected_without_a_request(self, world: World) -> None:
        """An approval doesn't survive a change to what it approved."""
        envelope_id = world.approved()
        world.change_the_plan()

        with pytest.raises(StaleEnvelopeError):
            world.handoffs.execute(envelope_id, actor=ACTOR, idempotency_key="k-000001")

        assert world.transport.requests == []
        assert world.attempt_rows() == 0

    def test_not_configured_fails_without_claiming_success(self, db_path: str) -> None:
        """A provider that can't be called is a failed attempt, not retryable."""
        world = World(db_path, TransportError("not_configured"))

        attempt = world.handoffs.execute(
            world.approved(), actor=ACTOR, idempotency_key="k-000001"
        )

        assert attempt.status is AttemptStatus.FAILED
        assert attempt.error_code == "not_configured"
        assert attempt.retryable is False
        assert attempt.external_url is None
        assert "list export" in attempt.human_action

    @pytest.mark.parametrize(
        ("code", "status"), [("rate_limited", 429), ("unavailable", 503)]
    )
    def test_a_passing_provider_fault_fails_retryably(
        self, db_path: str, code: str, status: int
    ) -> None:
        """A 429 or outage is tried three times, then recorded as retryable."""
        world = World(db_path, TransportError(code, status))

        attempt = world.handoffs.execute(
            world.approved(), actor=ACTOR, idempotency_key="k-000001"
        )

        assert attempt.status is AttemptStatus.FAILED
        assert attempt.error_code == PROVIDER_UNAVAILABLE
        assert attempt.retryable is True
        assert len(world.transport.requests) == 3
        assert world.slept == [0.5, 1.0]

    def test_a_retry_with_the_same_key_tries_again_and_can_succeed(
        self, db_path: str
    ) -> None:
        """After a passing fault, the same key sends again under the same approval."""
        down = TransportError("unavailable", 503)
        world = World(db_path, down, down, down, LINK)
        envelope_id = world.approved()
        failed = world.handoffs.execute(
            envelope_id, actor=ACTOR, idempotency_key="k-000001"
        )

        retried = world.handoffs.execute(
            envelope_id, actor=ACTOR, idempotency_key="k-000001"
        )

        assert failed.status is AttemptStatus.FAILED
        assert retried.status is AttemptStatus.CREATED
        assert retried.id == failed.id
        assert retried.external_url == LINK
        assert retried.error_code is None
        assert retried.retryable is False
        assert len(world.transport.requests) == 4
        assert world.attempt_rows() == 1

    def test_a_retry_is_refused_once_the_plan_changed(self, db_path: str) -> None:
        """A retry still needs the approval to hold."""
        world = World(db_path, TransportError("unavailable", 503))
        envelope_id = world.approved()
        world.handoffs.execute(envelope_id, actor=ACTOR, idempotency_key="k-000001")
        world.change_the_plan()

        with pytest.raises(StaleEnvelopeError):
            world.handoffs.execute(envelope_id, actor=ACTOR, idempotency_key="k-000001")

        assert len(world.transport.requests) == 3

    def test_a_retry_is_refused_once_the_approval_ran_out(self, db_path: str) -> None:
        """A retry after the approval's time is up needs a new approval."""
        world = World(db_path, TransportError("unavailable", 503))
        envelope_id = world.approved()
        world.handoffs.execute(envelope_id, actor=ACTOR, idempotency_key="k-000001")
        world.clock.now = NOW + dt.timedelta(minutes=31)

        with pytest.raises(EnvelopeNotActionableError, match="retried"):
            world.handoffs.execute(envelope_id, actor=ACTOR, idempotency_key="k-000001")

        assert len(world.transport.requests) == 3

    def test_a_permanent_failure_is_not_retried_by_the_same_key(
        self, db_path: str
    ) -> None:
        """A refusal stays a refusal; repeating the request sends nothing."""
        world = World(db_path, TransportError("bad_request", 400))
        envelope_id = world.approved()
        first = world.handoffs.execute(
            envelope_id, actor=ACTOR, idempotency_key="k-000001"
        )

        again = world.handoffs.execute(
            envelope_id, actor=ACTOR, idempotency_key="k-000001"
        )

        assert first.error_code == "bad_request"
        assert again == first
        assert len(world.transport.requests) == 1

    @pytest.mark.parametrize("link", ["", "http://insecure.example/l"])
    def test_a_response_without_a_link_is_failed_never_created(
        self, db_path: str, link: str
    ) -> None:
        """A 200 with no usable link is not a handoff."""
        world = World(db_path, link)

        attempt = world.handoffs.execute(
            world.approved(), actor=ACTOR, idempotency_key="k-000001"
        )

        assert attempt.status is AttemptStatus.FAILED
        assert attempt.error_code == "invalid_response"
        assert attempt.external_url is None

    def test_an_expired_link_shows_as_expired(self, world: World) -> None:
        """Past the link's expiry the attempt reads expired, and stays stored."""
        attempt = world.handoffs.execute(
            world.approved(), actor=ACTOR, idempotency_key="k-000001"
        )
        world.clock.now = NOW + dt.timedelta(days=14, seconds=1)

        later = world.handoffs.get_attempt(attempt.id)

        assert later is not None
        assert later.status is AttemptStatus.EXPIRED
        assert later.external_url == LINK

    def test_regenerating_needs_a_new_approval(self, world: World) -> None:
        """A second link for the same plan takes a second draft and approval."""
        first = world.handoffs.execute(
            world.approved(), actor=ACTOR, idempotency_key="k-000001"
        )

        second = world.handoffs.execute(
            world.approved(), actor=ACTOR, idempotency_key="k-000002"
        )

        assert second.id != first.id
        assert second.envelope_id != first.envelope_id
        assert len(world.transport.requests) == 2

    def test_a_key_used_for_another_approval_is_refused(self, world: World) -> None:
        """One key, one handoff."""
        world.handoffs.execute(
            world.approved(), actor=ACTOR, idempotency_key="k-000001"
        )
        other = world.approved()

        with pytest.raises(KeyBelongsToAnotherHandoffError, match="new key"):
            world.handoffs.execute(other, actor=ACTOR, idempotency_key="k-000001")

        assert len(world.transport.requests) == 1

    def test_an_adapter_that_crashes_is_a_failed_attempt(self, db_path: str) -> None:
        """A bug in an adapter is recorded, not raised at the member."""
        world = World(db_path)
        envelope_id = world.approved()
        del world.adapters["instacart"]

        attempt = world.handoffs.execute(
            envelope_id, actor=ACTOR, idempotency_key="k-000001"
        )

        assert attempt.status is AttemptStatus.FAILED
        assert attempt.error_code == ADAPTER_ERROR
        assert attempt.retryable is False


class TestConcurrency:
    """Two confirms at once make one request."""

    def test_concurrent_confirms_with_one_key_send_once(self, world: World) -> None:
        """The key's unique row decides which caller sends."""
        envelope_id = world.approved()
        barrier = threading.Barrier(4)
        results: list[Any] = []

        def confirm() -> None:
            barrier.wait()
            try:
                results.append(
                    world.handoffs.execute(
                        envelope_id, actor=ACTOR, idempotency_key="k-000001"
                    )
                )
            except Exception as err:
                results.append(err)

        threads = [threading.Thread(target=confirm) for _ in range(4)]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        assert len(world.transport.requests) == 1
        assert world.attempt_rows() == 1
        assert not [r for r in results if isinstance(r, Exception)]
        assert {r.id for r in results} == {results[0].id}

    def test_concurrent_confirms_with_different_keys_send_once(
        self, world: World
    ) -> None:
        """The approval can be claimed once, whatever the keys."""
        envelope_id = world.approved()
        barrier = threading.Barrier(4)
        results: list[Any] = []

        def confirm(key: str) -> None:
            barrier.wait()
            try:
                results.append(
                    world.handoffs.execute(
                        envelope_id, actor=ACTOR, idempotency_key=key
                    )
                )
            except EnvelopeNotActionableError as err:
                results.append(err)

        threads = [
            threading.Thread(target=confirm, args=(f"k-00000{n}",)) for n in range(4)
        ]
        for thread in threads:
            thread.start()
        for thread in threads:
            thread.join()

        created = [r for r in results if not isinstance(r, Exception)]
        assert len(world.transport.requests) == 1
        assert len(created) == 1
        assert len(results) == 4
        assert world.attempt_rows() == 1


class TestReads:
    """Attempts can be read back."""

    def test_attempts_for_a_plan(self, world: World) -> None:
        """Every attempt for a plan version, oldest first."""
        first = world.handoffs.execute(
            world.approved(), actor=ACTOR, idempotency_key="k-000001"
        )
        second = world.handoffs.execute(
            world.approved(), actor=ACTOR, idempotency_key="k-000002"
        )

        listed = world.handoffs.attempts_for_plan(world.plan.version)

        assert [attempt.id for attempt in listed] == [first.id, second.id]
        assert world.handoffs.attempts_for_plan(999) == []

    def test_an_unknown_attempt(self, world: World) -> None:
        """A missing id reads as None; the error names it."""
        assert world.handoffs.get_attempt(999) is None
        assert str(AttemptNotFoundError(999)) == "No handoff attempt has id 999."


class TestAudit:
    """Each step leaves an audit event, and none carries a secret."""

    def test_draft_approve_and_attempt_are_audited(
        self, world: World, events: list[dict[str, Any]]
    ) -> None:
        """The trail shows who drafted, who approved and what the attempt came to."""
        attempt = world.handoffs.execute(
            world.approved(), actor=ACTOR, idempotency_key="k-000001"
        )

        names = [event["event"] for event in events]
        assert names.index("handoff.drafted") < names.index("handoff.approved")
        assert names.index("handoff.approved") < names.index("handoff.attempt")
        recorded = next(e for e in events if e["event"] == "handoff.attempt")
        assert recorded["attempt_id"] == attempt.id
        assert recorded["status"] == "created"
        assert recorded["adapter"] == "instacart"
        assert LINK not in json.dumps(events)

    def test_a_failed_attempt_is_audited_with_its_code(
        self, db_path: str, events: list[dict[str, Any]]
    ) -> None:
        """A failure names its stable code and whether a retry could help."""
        world = World(db_path, TransportError("unavailable", 503))

        world.handoffs.execute(
            world.approved(), actor=ACTOR, idempotency_key="k-000001"
        )

        recorded = next(e for e in events if e["event"] == "handoff.attempt")
        assert recorded["status"] == "failed"
        assert recorded["error"] == PROVIDER_UNAVAILABLE
        assert recorded["retryable"] is True
        assert [e["event"] for e in events].count("adapter.failed") == 1


_PG_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.mark.skipif(not _PG_URL, reason="TEST_DATABASE_URL not set")
class TestPostgres:
    """The attempt table and its claims behave the same on Postgres."""

    @pytest.fixture()
    def clean(self) -> Iterator[None]:
        """Remove this suite's attempts before and after."""
        init_db(_PG_URL)
        self._clean()
        yield
        self._clean()

    def _clean(self) -> None:
        """Delete this suite's attempts."""
        with closing(get_connection(_PG_URL)) as conn:
            conn.execute(
                "DELETE FROM fulfillment_attempts WHERE created_by = ?",
                ("pg-handoff-test",),
            )
            conn.commit()

    @pytest.mark.usefixtures("clean")
    def test_execute_retry_and_repeat(self) -> None:
        """Create, fail retryably, retry and repeat, all through Postgres."""
        actor = "pg-handoff-test"
        down = TransportError("unavailable", 503)
        transport = FakeTransport(down, down, down, LINK)
        adapters: dict[str, FulfillmentAdapter] = {
            "instacart": InstacartAdapter(transport)
        }
        handoffs = Handoffs(_PG_URL, adapters.__getitem__, sleep=lambda _s: None)
        item = HouseholdItemStore(_PG_URL).resolve_or_create(
            f"pg handoff item {os.urandom(4).hex()}", actor=actor
        )
        ReplenishmentNeeds(_PG_URL).add_reason(
            item.id, NeedReasonKind.MANUAL_REQUEST, source_key="pg", actor=actor
        )
        plan = ShoppingPlans(_PG_URL).build([], actor=actor)
        draft = handoffs.draft(plan.version, "instacart", actor=actor)
        handoffs.approve(draft.id, draft.payload_hash, actor=actor)
        key = f"pg-{os.urandom(6).hex()}"

        failed = handoffs.execute(draft.id, actor=actor, idempotency_key=key)
        retried = handoffs.execute(draft.id, actor=actor, idempotency_key=key)
        again = handoffs.execute(draft.id, actor=actor, idempotency_key=key)

        assert failed.status is AttemptStatus.FAILED
        assert failed.retryable is True
        assert retried.status is AttemptStatus.CREATED
        assert retried.id == failed.id
        assert again == retried
        assert len(transport.requests) == 4
