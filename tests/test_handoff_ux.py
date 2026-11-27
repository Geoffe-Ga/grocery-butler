"""Tests for how a handoff is presented: wording, states, page, metrics (#149).

The provider is always a fake transport. Nothing here reaches a network.
"""

from __future__ import annotations

import datetime as dt
import itertools
import os
import re
from pathlib import Path
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from grocery_butler.app import create_app
from grocery_butler.constraints import HouseholdRules
from grocery_butler.db import init_db
from grocery_butler.fulfillment.handoffs import (
    AttemptNotFoundError,
    AttemptStatus,
    HandoffAttempt,
    HandoffNotAvailableError,
    Handoffs,
)
from grocery_butler.fulfillment.instacart.adapter import InstacartAdapter
from grocery_butler.fulfillment.instacart.transport import (
    FakeTransport,
    TransportError,
)
from grocery_butler.fulfillment.wording import (
    BANNED_PHRASES,
    EXPORT_LINK,
    NOT_AN_ORDER,
    OPEN_BUTTON,
    checks_for,
    summarize,
)
from grocery_butler.household_items import HouseholdItemStore
from grocery_butler.members import MemberStore
from grocery_butler.metrics import Metrics
from grocery_butler.models import HardConstraintKind, NeedReasonKind
from grocery_butler.reconciliation import (
    LineAction,
    LineOutcome,
    LineReview,
    NewReceiptLine,
    Reconciliations,
)
from grocery_butler.replenishment import ReplenishmentNeeds
from grocery_butler.shopping_plans import ShoppingPlans
from tests.test_api_v2 import SECRET_ENV, Api

if TYPE_CHECKING:
    from flask import Flask

    from grocery_butler.fulfillment.base import FulfillmentAdapter

NOW = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.UTC)
ACTOR = "geoff@test"
LINK = "https://example.invalid/l/abc"
TEMPLATES = Path(__file__).resolve().parent.parent / "grocery_butler" / "templates"
_keys = itertools.count(1)


def _banned_in(text: str) -> list[str]:
    """Return the banned phrases that appear in a text, as whole words."""
    lowered = text.lower()
    return [
        phrase
        for phrase in BANNED_PHRASES
        if re.search(rf"\b{re.escape(phrase)}\b", lowered)
    ]


class FakeClock:
    """A clock tests move by hand."""

    def __init__(self) -> None:
        """Start at NOW."""
        self.now = NOW

    def __call__(self) -> dt.datetime:
        """Return the current fake time."""
        return self.now


class World:
    """A household with a plan that carries an allergen and an odd unit."""

    def __init__(self, db_path: str, *outcomes: str | TransportError) -> None:
        """Build the plan and the handoff service.

        Args:
            db_path: A migrated database.
            *outcomes: What the fake provider returns or raises.
        """
        self.db_path = db_path
        self.clock = FakeClock()
        self.adapters: dict[str, FulfillmentAdapter] = {
            "instacart": InstacartAdapter(
                FakeTransport(*(outcomes or (LINK,))), clock=self.clock
            )
        }
        self.handoffs = Handoffs(
            db_path,
            self.adapters.__getitem__,
            clock=self.clock,
            sleep=lambda _s: None,
        )
        HouseholdRules(db_path).add_hard(
            HardConstraintKind.ALLERGEN_EXCLUSION, "peanut", actor=ACTOR
        )
        items = HouseholdItemStore(db_path)
        needs = ReplenishmentNeeds(db_path, clock=self.clock)
        self.item_id = items.resolve_or_create("granola bars", actor=ACTOR).id
        needs.add_reason(
            self.item_id, NeedReasonKind.MANUAL_REQUEST, source_key="t", actor=ACTOR
        )
        self.plan = ShoppingPlans(db_path, clock=self.clock).build([], actor=ACTOR)

    def attempt(self) -> HandoffAttempt:
        """Draft, approve and execute a handoff."""
        draft = self.handoffs.draft(self.plan.version, "instacart", actor=ACTOR)
        self.handoffs.approve(draft.id, draft.payload_hash, actor=ACTOR)
        return self.handoffs.execute(
            draft.id, actor=ACTOR, idempotency_key=f"ux-{next(_keys):06d}"
        )

    def say(self, attempt: HandoffAttempt) -> str:
        """Return the sentence for an attempt."""
        return summarize(attempt, self.handoffs.approved_payload(attempt))


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a migrated temporary database path."""
    path = str(tmp_path / "ux.db")
    init_db(path)
    return path


@pytest.fixture()
def world(db_path: str) -> World:
    """Return a household whose provider returns a link."""
    return World(db_path)


def _every_state(db_path: str) -> dict[AttemptStatus, tuple[World, HandoffAttempt]]:
    """Build one attempt in each state a person can be shown."""
    made: dict[AttemptStatus, tuple[World, HandoffAttempt]] = {}
    created = World(db_path)
    made[AttemptStatus.CREATED] = (created, created.attempt())
    opened_attempt = created.handoffs.mark_opened(created.attempt().id)
    made[AttemptStatus.OPENED] = (created, opened_attempt)
    failed = World(db_path, TransportError("bad_request", 400))
    made[AttemptStatus.FAILED] = (failed, failed.attempt())
    expiring = created.attempt()
    created.clock.now = NOW + dt.timedelta(days=15)
    expired = created.handoffs.get_attempt(expiring.id)
    assert expired is not None
    made[AttemptStatus.EXPIRED] = (created, expired)
    created.clock.now = NOW
    pending = expiring.model_copy(update={"status": AttemptStatus.PENDING})
    made[AttemptStatus.PENDING] = (created, pending)
    return made


class TestWording:
    """What we say about a handoff never claims a purchase."""

    def test_a_live_link_says_what_needs_checking(self, world: World) -> None:
        """The example in the issue: ready to review, and what to check."""
        sentence = world.say(world.attempt())

        assert sentence.startswith(
            "Your plan is ready to review in Instacart shopping list."
        )
        assert "1 item needs your check: Granola Bars: check for peanut." in sentence
        assert sentence.endswith(NOT_AN_ORDER)

    def test_every_state_has_wording_and_none_is_banned(self, db_path: str) -> None:
        """Created, opened, expired, failed and pending each read honestly."""
        sentences = {
            status: world.say(attempt)
            for status, (world, attempt) in _every_state(db_path).items()
        }

        assert set(sentences) == set(AttemptStatus)
        assert "ready to review" in sentences[AttemptStatus.CREATED]
        assert "You opened this plan" in sentences[AttemptStatus.OPENED]
        assert "has run out" in sentences[AttemptStatus.EXPIRED]
        assert "list export" in sentences[AttemptStatus.FAILED]
        assert "still being made" in sentences[AttemptStatus.PENDING]
        for sentence in sentences.values():
            assert _banned_in(sentence) == [], sentence

    def test_the_fixed_strings_are_clean(self) -> None:
        """The button, the export link and the note use no banned phrase."""
        assert OPEN_BUTTON == "Shop on Instacart"
        assert EXPORT_LINK == "Export list instead"
        for text in (OPEN_BUTTON, EXPORT_LINK, NOT_AN_ORDER):
            assert _banned_in(text) == [], text

    def test_the_banned_list_covers_what_the_issue_names(self) -> None:
        """Ordered, purchased and on the way are all on the list."""
        assert {"ordered", "purchased", "on the way"} <= set(BANNED_PHRASES)
        assert _banned_in("Your groceries are on the way") == ["on the way"]
        assert _banned_in("Ordered!") == ["ordered"]
        assert _banned_in("a reordered list") == []

    def test_many_checks_are_counted_and_only_a_few_are_named(self) -> None:
        """Five things to check: all counted, three named, the rest summed."""
        payload = {
            "adapter_display_name": "Instacart",
            "unenforced_constraints": [
                {
                    "display_name": f"Item {n}",
                    "kind": "allergen_exclusion",
                    "value": "x",
                }
                for n in range(5)
            ],
            "issues": [],
        }
        attempt = HandoffAttempt(
            id=1,
            envelope_id=1,
            adapter="instacart",
            plan_version=1,
            plan_content_hash="h",
            idempotency_key="k",
            status=AttemptStatus.CREATED,
            external_url=LINK,
            link_expires_at=None,
            error_code=None,
            retryable=False,
            human_action="",
            created_by=ACTOR,
            created_at=NOW,
            updated_at=NOW,
        )

        sentence = summarize(attempt, payload)

        assert "5 items need your check:" in sentence
        assert "Item 2: check for x; and 2 more." in sentence
        assert "Item 3" not in sentence

    def test_checks_name_constraints_and_other_issues_once(self) -> None:
        """A constraint isn't listed twice; other issues are listed too."""
        payload = {
            "unenforced_constraints": [
                {
                    "display_name": "Bars",
                    "kind": "allergen_exclusion",
                    "value": "peanut",
                },
                {"display_name": "Tofu", "kind": "do_not_substitute", "value": ""},
            ],
            "issues": [
                {
                    "kind": "hard_constraint_unrepresentable",
                    "display_name": "Bars",
                    "detail": "ignored here",
                },
                {
                    "kind": "unsupported_unit",
                    "display_name": "Garlic",
                    "detail": "No unit for 3 clove.",
                },
            ],
        }

        assert checks_for(payload) == [
            "Bars: check for peanut",
            "Tofu: check for do not substitute",
            "Garlic: No unit for 3 clove",
        ]
        assert checks_for({}) == []

    def test_unfiltered_preferences_are_one_note_however_many_lines(self) -> None:
        """A household-wide preference is said once, not on every line (#215)."""
        payload = {
            "issues": [
                {
                    "kind": "preference_not_applied",
                    "display_name": "Oat Milk",
                    "detail": "prefer the store brand; avoid Silk",
                },
                {
                    "kind": "unsupported_unit",
                    "display_name": "Garlic",
                    "detail": "No unit for 3 clove.",
                },
                {
                    "kind": "preference_not_applied",
                    "display_name": "Butter",
                    "detail": "prefer the store brand",
                },
            ]
        }

        assert checks_for(payload) == [
            "Garlic: No unit for 3 clove",
            "These preferences can't be applied here, so check as you shop:"
            " Oat Milk (prefer the store brand; avoid Silk),"
            " Butter (prefer the store brand)",
        ]

    def test_no_checks_means_a_short_sentence(self) -> None:
        """With nothing to check, the sentence says so by saying less."""
        payload = {"adapter_display_name": "Instacart"}
        attempt = HandoffAttempt(
            id=1,
            envelope_id=1,
            adapter="instacart",
            plan_version=1,
            plan_content_hash="h",
            idempotency_key="k",
            status=AttemptStatus.CREATED,
            external_url=LINK,
            link_expires_at=None,
            error_code=None,
            retryable=False,
            human_action="",
            created_by=ACTOR,
            created_at=NOW,
            updated_at=NOW,
        )

        assert summarize(attempt, payload) == (
            f"Your plan is ready to review in Instacart. {NOT_AN_ORDER}"
        )

    def test_handoff_templates_use_no_banned_phrase(self) -> None:
        """The page's own words are held to the same list."""
        text = (TEMPLATES / "handoff.html").read_text()
        visible = re.sub(r"\{[%{#].*?[%}#]\}", " ", text, flags=re.DOTALL)

        assert _banned_in(visible) == []


class TestOpened:
    """Following the link through our redirect is recorded once."""

    def test_opening_marks_the_attempt(self, world: World) -> None:
        """A created attempt becomes opened, with the time."""
        attempt = world.attempt()
        world.clock.now = NOW + dt.timedelta(hours=1)

        opened = world.handoffs.mark_opened(attempt.id)

        assert opened.status is AttemptStatus.OPENED
        assert opened.opened_at == NOW + dt.timedelta(hours=1)
        assert opened.external_url == LINK

    def test_opening_again_keeps_the_first_time(self, world: World) -> None:
        """The first open is the one recorded."""
        attempt = world.attempt()
        first = world.handoffs.mark_opened(attempt.id)
        world.clock.now = NOW + dt.timedelta(hours=5)

        again = world.handoffs.mark_opened(attempt.id)

        assert again.opened_at == first.opened_at == NOW

    def test_an_opened_link_still_expires(self, world: World) -> None:
        """Expiry wins over opened once the link has run out."""
        attempt = world.attempt()
        world.handoffs.mark_opened(attempt.id)
        world.clock.now = NOW + dt.timedelta(days=15)

        later = world.handoffs.get_attempt(attempt.id)

        assert later is not None
        assert later.status is AttemptStatus.EXPIRED
        assert later.opened_at == NOW

    def test_a_failed_or_expired_attempt_has_nothing_to_open(
        self, db_path: str
    ) -> None:
        """There is no link to follow, and the error says what to do."""
        failed_world = World(db_path, TransportError("bad_request", 400))
        failed = failed_world.attempt()
        live_world = World(db_path)
        expiring = live_world.attempt()
        live_world.clock.now = NOW + dt.timedelta(days=15)

        with pytest.raises(HandoffNotAvailableError, match="is failed"):
            failed_world.handoffs.mark_opened(failed.id)
        with pytest.raises(HandoffNotAvailableError, match="is expired"):
            live_world.handoffs.mark_opened(expiring.id)
        with pytest.raises(AttemptNotFoundError):
            live_world.handoffs.mark_opened(99999)

    def test_latest_for_plan(self, world: World) -> None:
        """The newest attempt is the one shown; none is None."""
        assert world.handoffs.latest_for_plan(world.plan.version) is None
        world.attempt()
        second = world.attempt()

        latest = world.handoffs.latest_for_plan(world.plan.version)

        assert latest is not None
        assert latest.id == second.id


class TestHandoffMetrics:
    """Counts of what became of handoffs, and nothing but counts."""

    def test_counts_follow_each_outcome(self, db_path: str) -> None:
        """Created, opened, abandoned, reconciled, failed and corrected."""
        world = World(db_path)
        metrics = Metrics(db_path, clock=world.clock)
        opened = world.attempt()
        world.handoffs.mark_opened(opened.id)
        reconciled = world.attempt()
        world.attempt()
        rejected = world.handoffs.draft(world.plan.version, "instacart", actor=ACTOR)
        world.handoffs.reject(rejected.id, actor=ACTOR)
        down = World(db_path, TransportError("bad_request", 400))
        down.attempt()
        receipts = Reconciliations(db_path, clock=world.clock)
        receipt = receipts.submit(
            [NewReceiptLine(name="granola bars", outcome=LineOutcome.RECEIVED)],
            submitted_by=ACTOR,
            attempt_id=reconciled.id,
        )
        receipts.resolve(
            receipt.id,
            [LineReview(line_id=receipt.lines[0].id, action=LineAction.CONFIRM)],
            actor=ACTOR,
        )
        before_expiry = metrics.handoffs()

        world.clock.now = NOW + dt.timedelta(days=15)

        assert before_expiry == {
            "created": 3,
            "opened": 1,
            "abandoned": 0,
            "reconciled": 1,
            "failed": 1,
            "corrected_before_handoff": 1,
        }
        assert metrics.handoffs()["abandoned"] == 2
        assert metrics.snapshot()["handoffs"] == metrics.handoffs()

    def test_no_handoffs_is_all_zeros(self, db_path: str) -> None:
        """A household that never handed anything off reads as zeros."""
        assert set(Metrics(db_path).handoffs().values()) == {0}

    def test_metrics_carry_no_names_or_links(self, world: World) -> None:
        """Only whole numbers come out."""
        world.attempt()

        counts = Metrics(world.db_path, clock=world.clock).handoffs()

        assert all(type(value) is int for value in counts.values())


class TestPage:
    """The dashboard page renders every state, with the export one tap away."""

    @pytest.fixture()
    def app(self, db_path: str) -> Flask:
        """Create the app on the test database."""
        with patch.dict(os.environ, SECRET_ENV):
            application = create_app(db_path=db_path)
        application.config["TESTING"] = True
        return application

    def _page(self, app: Flask) -> str:
        """Fetch the handoff page."""
        response = app.test_client().get("/handoff")
        assert response.status_code == 200
        return response.get_data(as_text=True)

    def _stub(self, monkeypatch: pytest.MonkeyPatch) -> None:
        """Turn the Instacart adapter on, on its stub transport."""
        monkeypatch.setenv("INSTACART_ENABLED", "true")
        monkeypatch.delenv("INSTACART_ENV", raising=False)

    def test_no_plan_yet(self, app: Flask) -> None:
        """Before any plan the page says so."""
        page = self._page(app)

        assert "There is no shopping plan yet" in page
        assert OPEN_BUTTON not in page

    def test_a_plan_not_handed_off_offers_the_export(
        self, app: Flask, db_path: str
    ) -> None:
        """The list is one tap away even with no handoff at all."""
        World(db_path)

        page = self._page(app)

        assert 'data-state="none"' in page
        assert EXPORT_LINK in page
        assert OPEN_BUTTON not in page

    def test_a_created_handoff_shows_the_button_and_the_checks(
        self, app: Flask, db_path: str
    ) -> None:
        """A live link shows the button, what to check, and the note."""
        world = World(db_path)
        attempt = world.attempt()

        page = self._page(app)

        assert 'data-state="created"' in page
        assert f"/handoff/attempts/{attempt.id}/open" in page
        assert OPEN_BUTTON in page
        assert "Granola Bars: check for peanut" in page
        assert EXPORT_LINK in page
        assert "not a purchase" in page
        assert LINK not in page

    def test_failed_shows_the_fallback_and_no_button(
        self, app: Flask, db_path: str
    ) -> None:
        """A failed handoff points at the export and offers no link."""
        World(db_path, TransportError("bad_request", 400)).attempt()

        page = self._page(app)

        assert 'data-state="failed"' in page
        assert "list export" in page
        assert EXPORT_LINK in page
        assert OPEN_BUTTON not in page

    def test_every_rendered_state_is_free_of_banned_phrases(
        self, app: Flask, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """None, created, opened and failed pages all pass the copy test."""
        pages = [self._page(app)]
        world = World(db_path)
        pages.append(self._page(app))
        attempt = world.attempt()
        pages.append(self._page(app))
        world.handoffs.mark_opened(attempt.id)
        pages.append(self._page(app))
        World(db_path, TransportError("bad_request", 400)).attempt()
        pages.append(self._page(app))

        assert 'data-state="opened"' in pages[3]
        for page in pages:
            body = re.sub(r"<[^>]+>", " ", page.split("<main", 1)[-1])
            assert _banned_in(body) == []

    def test_following_the_button_records_the_open_and_redirects(
        self, app: Flask, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The redirect goes to the stored link and marks the attempt opened."""
        self._stub(monkeypatch)
        MemberStore(db_path).add("geoff", "Geoff", created_by="test")
        api = Api(app)
        item = api.call("POST", "/items", {"name": "oat milk"}).get_json()["id"]
        api.call("POST", "/needs", {"item_id": item})
        version = api.call("POST", "/plans", {}).get_json()["version"]
        draft = api.call(
            "POST", f"/plans/{version}/handoffs", {"adapter": "instacart"}
        ).get_json()
        api.call(
            "POST",
            f"/handoffs/{draft['id']}/approve",
            {"payload_hash": draft["payload_hash"]},
        )
        attempt = api.call("POST", f"/handoffs/{draft['id']}/execute").get_json()

        response = app.test_client().get(attempt["open_path"])
        after = api.call("GET", f"/handoffs/attempts/{attempt['id']}").get_json()

        assert response.status_code == 302
        assert response.headers["Location"] == attempt["external_url"]
        assert after["status"] == "opened"
        assert after["opened_at"] is not None

    def test_the_redirect_never_follows_a_dead_or_unknown_attempt(
        self, app: Flask, db_path: str
    ) -> None:
        """No live link: back to the page with a message, never elsewhere."""
        failed = World(db_path, TransportError("bad_request", 400)).attempt()
        client = app.test_client()

        dead = client.get(f"/handoff/attempts/{failed.id}/open")
        unknown = client.get("/handoff/attempts/99999/open")
        hostile = client.get(
            f"/handoff/attempts/{failed.id}/open?next=https://evil.example"
        )

        assert [r.status_code for r in (dead, unknown, hostile)] == [302, 302, 302]
        assert {r.headers["Location"] for r in (dead, unknown, hostile)} == {"/handoff"}

    def test_the_export_works_with_instacart_off_or_failing(
        self, app: Flask, db_path: str, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """The list never depends on another adapter."""
        monkeypatch.delenv("INSTACART_ENABLED", raising=False)
        World(db_path, TransportError("unavailable", 503)).attempt()

        response = app.test_client().get("/handoff/export.txt")

        assert response.status_code == 200
        assert response.mimetype == "text/plain"
        assert "Granola Bars" in response.get_data(as_text=True)
        assert "{MUST:" in response.get_data(as_text=True)

    def test_exporting_with_no_plan_goes_back_with_a_message(self, app: Flask) -> None:
        """Nothing to export is said, not served as an empty file."""
        response = app.test_client().get("/handoff/export.txt")

        assert response.status_code == 302
        assert response.headers["Location"] == "/handoff"


class TestApiResponse:
    """The API tells OpenClaw what was sent, what to check and what to say."""

    @pytest.fixture()
    def api(self, db_path: str, monkeypatch: pytest.MonkeyPatch) -> Api:
        """Return a signing client with the Instacart stub turned on."""
        monkeypatch.setenv("INSTACART_ENABLED", "true")
        monkeypatch.delenv("INSTACART_ENV", raising=False)
        with patch.dict(os.environ, SECRET_ENV):
            application = create_app(db_path=db_path)
        application.config["TESTING"] = True
        MemberStore(db_path).add("geoff", "Geoff", created_by="test")
        return Api(application)

    def _attempt(self, api: Api) -> dict[str, Any]:
        """Request an item with an allergen rule, plan it and hand it off."""
        item = api.call("POST", "/items", {"name": "granola bars"}).get_json()["id"]
        api.call(
            "POST", "/constraints", {"kind": "allergen_exclusion", "value": "peanut"}
        )
        api.call("POST", "/needs", {"item_id": item})
        version = api.call("POST", "/plans", {}).get_json()["version"]
        draft = api.call(
            "POST", f"/plans/{version}/handoffs", {"adapter": "instacart"}
        ).get_json()
        api.call(
            "POST",
            f"/handoffs/{draft['id']}/approve",
            {"payload_hash": draft["payload_hash"]},
        )
        attempt: dict[str, Any] = api.call(
            "POST", f"/handoffs/{draft['id']}/execute"
        ).get_json()
        return attempt

    def test_the_attempt_carries_the_link_expiry_lines_and_issues(
        self, api: Api
    ) -> None:
        """Everything OpenClaw needs to speak honestly is in one response."""
        attempt = self._attempt(api)

        assert attempt["status"] == "created"
        assert attempt["external_url"].startswith("https://example.invalid/")
        assert attempt["link_expires_at"] is not None
        assert attempt["open_path"] == f"/handoff/attempts/{attempt['id']}/open"
        assert [line["display_name"] for line in attempt["sent"]["lines"]] == [
            "Granola Bars"
        ]
        assert [i["kind"] for i in attempt["sent"]["issues"]] == [
            "hard_constraint_unrepresentable"
        ]
        assert attempt["needs_your_check"] == ["Granola Bars: check for peanut"]
        assert "1 item needs your check" in attempt["summary"]

    def test_no_handoff_response_uses_a_banned_phrase(self, api: Api) -> None:
        """The whole response body, every field, passes the copy test."""
        attempt = self._attempt(api)
        listed = api.call("GET", f"/plans/{attempt['plan_version']}/handoffs")
        fetched = api.call("GET", f"/handoffs/attempts/{attempt['id']}")
        approval = api.call("GET", f"/handoffs/{attempt['approval_id']}")

        for response in (listed, fetched, approval):
            assert _banned_in(response.get_data(as_text=True)) == []

    def test_metrics_include_handoff_counts(self, api: Api) -> None:
        """OpenClaw can read how handoffs are going."""
        self._attempt(api)

        handoffs = api.call("GET", "/operations/metrics").get_json()["handoffs"]

        assert handoffs["created"] == 1
        assert set(handoffs) == {
            "created",
            "opened",
            "abandoned",
            "reconciled",
            "failed",
            "corrected_before_handoff",
        }
