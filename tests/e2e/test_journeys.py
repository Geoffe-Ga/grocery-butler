"""E2E: whole journeys through API v2, one capability feeding the next.

``tests/test_api_v2.py`` checks each capability by itself. These tests
walk the loop the product exists for, over HTTP only, so a broken link
between two capabilities fails here even when each still works alone
(issue #226). Each step asserts on what the *next* capability reads.

Nothing here needs Claude, a clock or a network. The Instacart adapter
runs on its stub transport, which contacts nothing.
"""

from __future__ import annotations

import json
import logging
import re
from typing import TYPE_CHECKING, Any

import pytest

from grocery_butler.fulfillment.wording import BANNED_PHRASES
from grocery_butler.observability import AUDIT_LOGGER

if TYPE_CHECKING:
    from flask.testing import FlaskClient

    from tests.e2e.conftest import V2

pytestmark = pytest.mark.e2e


@pytest.fixture()
def instacart_stub(monkeypatch: pytest.MonkeyPatch) -> None:
    """Turn the Instacart adapter on, on the transport that contacts nothing."""
    monkeypatch.setenv("INSTACART_ENABLED", "true")
    monkeypatch.delenv("INSTACART_ENV", raising=False)
    monkeypatch.delenv("INSTACART_API_KEY", raising=False)


@pytest.fixture()
def audit(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Capture the audit lines the journey writes."""
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    return caplog


def _events(audit: pytest.LogCaptureFixture) -> list[dict[str, Any]]:
    """Return the captured audit events, oldest first, without request lines."""
    events = [
        json.loads(record.getMessage())
        for record in audit.records
        if record.name == AUDIT_LOGGER
    ]
    return [event for event in events if event["event"] != "http.request"]


def _item(v2: V2, name: str) -> int:
    """Create a household item and return its id."""
    item_id: int = v2.ok("POST", "/items", {"name": name})["id"]
    return item_id


def _state(v2: V2, item_id: int) -> str:
    """Return what the household is believed to have of an item."""
    state: str = v2.ok("GET", f"/items/{item_id}")["state"]["state"]
    return state


def _need_status(v2: V2, item_id: int) -> str | None:
    """Return the status of an item's open need, or None if it has none."""
    need = v2.ok("GET", f"/items/{item_id}")["open_need"]
    return None if need is None else str(need["status"])


def _planned_items(plan: dict[str, Any]) -> list[int]:
    """Return the item ids on a plan's lines."""
    return [line["item_id"] for line in plan["lines"]]


def _confirm_received(v2: V2, name: str, **receipt: Any) -> dict[str, Any]:
    """Submit a one-line receipt for an item and confirm it.

    Args:
        v2: The API client.
        name: The item's name as written on the receipt.
        **receipt: Extra receipt fields, such as ``attempt_id``.

    Returns:
        The resolved receipt.
    """
    submitted = v2.ok(
        "POST",
        "/receipts",
        {"lines": [{"name": name, "outcome": "received"}], **receipt},
    )
    resolved: dict[str, Any] = v2.ok(
        "POST",
        f"/receipts/{submitted['id']}/resolve",
        {"reviews": [{"line_id": submitted["lines"][0]["id"], "action": "confirm"}]},
    )
    return resolved


def test_low_stock_becomes_a_plan_a_list_and_a_restocked_item(v2: V2) -> None:
    """Observation, need, approval, plan, list, receipt: each feeds the next.

    Before every approval the test also checks that nothing has moved yet:
    a suggested need is not on a plan, and a submitted receipt changes no
    inventory until a member confirms it.
    """
    item_id = _item(v2, "oat milk")

    v2.ok("POST", f"/items/{item_id}/observations", {"state": "low"})
    need = v2.ok("GET", f"/items/{item_id}")["open_need"]
    assert need["status"] == "suggested"
    assert [reason["kind"] for reason in need["reasons"]] == ["low_stock"]

    unapproved_plan = v2.ok("POST", "/plans", {})
    assert _planned_items(unapproved_plan) == []

    v2.ok("POST", f"/needs/{need['id']}/approve")
    plan = v2.ok("POST", "/plans", {})
    assert _planned_items(plan) == [item_id]
    assert plan["lines"][0]["need_id"] == need["id"]
    assert _need_status(v2, item_id) == "planned"

    export = v2.ok("GET", f"/plans/{plan['version']}/export")
    assert export["plan_content_hash"] == plan["content_hash"]
    assert "[ ] Oat Milk" in export["content"]
    assert "Nothing has been ordered" in export["human_action"]

    submitted = v2.ok(
        "POST", "/receipts", {"lines": [{"name": "Oat Milk", "outcome": "received"}]}
    )
    assert submitted["lines"][0]["item_id"] == item_id
    assert _state(v2, item_id) == "low"
    assert _need_status(v2, item_id) == "planned"

    v2.ok(
        "POST",
        f"/receipts/{submitted['id']}/resolve",
        {"reviews": [{"line_id": submitted["lines"][0]["id"], "action": "confirm"}]},
    )
    item = v2.ok("GET", f"/items/{item_id}")
    assert item["state"]["state"] == "on_hand"
    assert item["state"]["observation"]["source"] == "receipt"
    assert item["open_need"] is None
    assert v2.ok("GET", "/needs") == {"needs": []}
    assert _planned_items(v2.ok("POST", "/plans", {})) == []


@pytest.mark.usefixtures("instacart_stub")
def test_an_instacart_handoff_from_draft_to_reconciled(
    v2: V2, client: FlaskClient, audit: pytest.LogCaptureFixture
) -> None:
    """Draft, approve, execute, open, receive: one attempt, counted once.

    The attempt's id links the handoff to the receipt in the stored
    records, in the metrics and in the audit lines.
    """
    item_id = _item(v2, "oat milk")
    v2.ok("POST", "/needs", {"item_id": item_id})
    version = v2.ok("POST", "/plans", {})["version"]

    draft = v2.ok("POST", f"/plans/{version}/handoffs", {"adapter": "instacart"})
    assert [line["item_id"] for line in draft["payload"]["lines"]] == [item_id]

    too_early = v2.post(f"/handoffs/{draft['id']}/execute")
    assert too_early.status_code == 409
    assert too_early.get_json()["error"]["code"] == "approval_not_actionable"
    assert v2.ok("GET", f"/plans/{version}/handoffs") == {"attempts": []}

    v2.ok(
        "POST",
        f"/handoffs/{draft['id']}/approve",
        {"payload_hash": draft["payload_hash"]},
    )
    attempt = v2.ok("POST", f"/handoffs/{draft['id']}/execute")
    assert attempt["status"] == "created"
    assert attempt["plan_content_hash"] == draft["payload"]["plan_content_hash"]
    listed = v2.ok("GET", f"/plans/{version}/handoffs")["attempts"]
    assert [each["id"] for each in listed] == [attempt["id"]]

    page = client.get("/handoff").get_data(as_text=True)
    button = re.search(r'<a[^>]*href="([^"]+)"[^>]*>\s*Shop on Instacart', page)
    assert button is not None
    assert button.group(1) == attempt["open_path"]

    first_open = client.get(attempt["open_path"])
    assert first_open.status_code == 302
    assert first_open.headers["Location"] == attempt["external_url"]
    opened = v2.ok("GET", f"/handoffs/attempts/{attempt['id']}")
    assert opened["status"] == "opened"
    client.get(attempt["open_path"])
    again = v2.ok("GET", f"/handoffs/attempts/{attempt['id']}")
    assert again["opened_at"] == opened["opened_at"]

    receipt = _confirm_received(v2, "oat milk", attempt_id=attempt["id"])
    assert receipt["attempt_id"] == attempt["id"]
    assert _state(v2, item_id) == "on_hand"
    assert _need_status(v2, item_id) is None

    handoffs = v2.ok("GET", "/operations/metrics")["handoffs"]
    assert (handoffs["created"], handoffs["opened"], handoffs["reconciled"]) == (
        1,
        1,
        1,
    )
    assert handoffs["failed"] == 0

    trail = [
        (event["event"], event.get("envelope_id"), event.get("attempt_id"))
        for event in _events(audit)
        if event["event"].startswith(("handoff.", "receipt."))
    ]
    assert trail == [
        ("handoff.drafted", draft["id"], None),
        ("handoff.approved", draft["id"], None),
        ("handoff.attempt", draft["id"], attempt["id"]),
        ("handoff.opened", None, attempt["id"]),
        ("receipt.submitted", None, attempt["id"]),
        ("receipt.resolved", None, attempt["id"]),
    ]


@pytest.mark.usefixtures("instacart_stub")
def test_no_handoff_text_claims_a_purchase(v2: V2, client: FlaskClient) -> None:
    """Every body and page along a handoff is free of the banned phrases."""
    v2.ok("POST", "/needs", {"item_id": _item(v2, "oat milk")})
    version = v2.ok("POST", "/plans", {})["version"]
    draft = v2.ok("POST", f"/plans/{version}/handoffs", {"adapter": "instacart"})
    approved = v2.ok(
        "POST",
        f"/handoffs/{draft['id']}/approve",
        {"payload_hash": draft["payload_hash"]},
    )
    attempt = v2.ok("POST", f"/handoffs/{draft['id']}/execute")
    client.get(attempt["open_path"])
    opened = v2.ok("GET", f"/handoffs/attempts/{attempt['id']}")
    page = client.get("/handoff").get_data(as_text=True)

    spoken = {
        "draft": json.dumps(draft),
        "approved": json.dumps(approved),
        "attempt": json.dumps(attempt),
        "opened": json.dumps(opened),
        "page": page,
    }
    for where, text in spoken.items():
        for phrase in BANNED_PHRASES:
            assert not re.search(rf"\b{re.escape(phrase)}\b", text, re.IGNORECASE), (
                f"{where} says {phrase!r}"
            )


def test_a_photo_batch_changes_only_what_a_member_decided(v2: V2) -> None:
    """Confirmed and corrected proposals move inventory and open needs.

    A rejected proposal, and every proposal before the review, moves
    nothing.
    """
    eggs, rice, butter = (_item(v2, name) for name in ("eggs", "rice", "butter"))

    batch = v2.ok(
        "POST",
        "/observation-batches",
        {
            "source": "photo",
            "evidence_ref": "photo:1",
            "proposals": [
                {"name": "eggs", "state": "out", "confidence": 0.95},
                {"name": "rise", "state": "low", "confidence": 0.9},
                {"name": "butter", "state": "out", "confidence": 0.9},
            ],
        },
    )
    assert [_state(v2, item) for item in (eggs, rice, butter)] == ["unknown"] * 3
    assert v2.ok("GET", "/needs") == {"needs": []}

    first, second, third = (proposal["id"] for proposal in batch["proposals"])
    v2.ok(
        "POST",
        f"/observation-batches/{batch['id']}/resolve",
        {
            "reviews": [
                {"proposal_id": first, "action": "confirm"},
                {"proposal_id": second, "action": "correct", "item_id": rice},
                {"proposal_id": third, "action": "reject"},
            ]
        },
    )

    assert [_state(v2, item) for item in (eggs, rice, butter)] == [
        "out",
        "low",
        "unknown",
    ]
    needs = v2.ok("GET", "/needs")["needs"]
    assert sorted(need["item_id"] for need in needs) == sorted([eggs, rice])
    assert {need["status"] for need in needs} == {"suggested"}
    assert _planned_items(v2.ok("POST", "/plans", {})) == []


@pytest.mark.usefixtures("instacart_stub")
def test_a_new_allergen_voids_an_approval_made_before_it(v2: V2) -> None:
    """A hard constraint added after approval stops that handoff.

    The approval was for a plan that did not know about the allergen, so
    it can't be executed. The next draft says what Instacart can't
    enforce, and the attempt carries that into what the member is told
    to check.
    """
    item_id = _item(v2, "oat milk")
    v2.ok("POST", "/needs", {"item_id": item_id})
    version = v2.ok("POST", "/plans", {})["version"]
    draft = v2.ok("POST", f"/plans/{version}/handoffs", {"adapter": "instacart"})
    v2.ok(
        "POST",
        f"/handoffs/{draft['id']}/approve",
        {"payload_hash": draft["payload_hash"]},
    )

    v2.ok("POST", "/constraints", {"kind": "allergen_exclusion", "value": "peanut"})

    refused = v2.post(f"/handoffs/{draft['id']}/execute")
    assert refused.status_code == 409
    assert refused.get_json()["error"]["code"] == "approval_stale"
    assert v2.ok("GET", f"/plans/{version}/handoffs") == {"attempts": []}
    assert v2.ok("GET", f"/plans/{version}")["is_current"] is False

    rebuilt = v2.ok("POST", "/plans", {})
    assert rebuilt["lines"][0]["hard_constraints"] == [
        {"kind": "allergen_exclusion", "value": "peanut", "scope": "household"}
    ]
    redraft = v2.ok(
        "POST", f"/plans/{rebuilt['version']}/handoffs", {"adapter": "instacart"}
    )
    assert redraft["payload"]["approving_acknowledges_unenforced_constraints"] is True
    assert [issue["kind"] for issue in redraft["payload"]["issues"]] == [
        "hard_constraint_unrepresentable"
    ]

    v2.ok(
        "POST",
        f"/handoffs/{redraft['id']}/approve",
        {"payload_hash": redraft["payload_hash"]},
    )
    attempt = v2.ok("POST", f"/handoffs/{redraft['id']}/execute")
    assert attempt["status"] == "created"
    assert attempt["needs_your_check"] == ["Oat Milk: check for peanut"]


@pytest.mark.usefixtures("instacart_stub")
def test_a_product_code_is_learned_from_a_receipt_and_used_on_the_next_plan(
    v2: V2,
) -> None:
    """Receipt, confirmation, item, next plan, handoff (ADR 0023).

    The code comes in on a receipt line and is saved only because a
    member confirmed that line and asked for it. The next plan's line
    carries it, the list shows it, and the handoff sends the item by code
    and says the organic filter was dropped for it.
    """
    code = "012345678905"
    item_id = _item(v2, "oat milk")
    v2.ok("POST", "/soft-preferences", {"kind": "organic"})
    v2.ok("POST", "/needs", {"item_id": item_id})
    first = v2.ok("POST", "/plans", {})
    assert "verified_upc" not in first["lines"][0]["constraints"]

    receipt = v2.ok(
        "POST",
        "/receipts",
        {"lines": [{"name": "oat milk", "outcome": "received", "upc": code}]},
    )
    assert v2.ok("GET", f"/items/{item_id}")["verified_upc"] is None

    v2.ok(
        "POST",
        f"/receipts/{receipt['id']}/resolve",
        {
            "reviews": [
                {
                    "line_id": receipt["lines"][0]["id"],
                    "action": "confirm",
                    "remember_upc": True,
                }
            ]
        },
    )
    item = v2.ok("GET", f"/items/{item_id}")
    assert item["verified_upc"] == code
    assert item["upc_verified_by"].startswith("geoff@")
    assert item["upc_verified_at"] is not None

    v2.ok("POST", "/needs", {"item_id": item_id})
    plan = v2.ok("POST", "/plans", {})
    assert plan["lines"][0]["constraints"] == {"organic": True, "verified_upc": code}
    assert f"UPC {code}" in v2.ok("GET", f"/plans/{plan['version']}/export")["content"]

    draft = v2.ok(
        "POST", f"/plans/{plan['version']}/handoffs", {"adapter": "instacart"}
    )
    assert [issue["kind"] for issue in draft["payload"]["issues"]] == [
        "filter_dropped_by_upc"
    ]

    cleared = v2.patch(f"/items/{item_id}", {"verified_upc": None})
    assert cleared.status_code == 200
    assert cleared.get_json()["verified_upc"] is None
    typed = v2.patch(f"/items/{item_id}", {"verified_upc": code})
    assert typed.status_code == 400
    assert v2.ok("GET", f"/items/{item_id}")["verified_upc"] is None
