"""Tests for the capability API at /api/v2 (ADR 0009, P0.8, issue #136).

Covers the contract (auth, named actors, idempotency keys, coded errors)
and the first capabilities: members, household items and their state.
"""

from __future__ import annotations

import itertools
import json
import os
import secrets
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from grocery_butler.api_v2 import (
    IDEMPOTENCY_HEADER,
    MEMBER_HEADER,
    REPLAY_HEADER,
)
from grocery_butler.app import create_app
from grocery_butler.auth_middleware import SECRET_ENV_VAR
from grocery_butler.household_items import HouseholdItemStore
from grocery_butler.inventory_ledger import InventoryLedger
from grocery_butler.members import MemberStore
from grocery_butler.models import (
    NeedReasonKind,
    NewObservation,
    ObservationSource,
    StockState,
)
from grocery_butler.replenishment import INVENTORY_SOURCE, ReplenishmentNeeds
from tests.conftest import bearer_header

if TYPE_CHECKING:
    from pathlib import Path

    from flask import Flask
    from werkzeug.test import TestResponse

# A fresh signing secret per test run; nothing depends on its value.
SECRET_ENV = {SECRET_ENV_VAR: secrets.token_hex(16)}
_keys = itertools.count(1)


def _new_key() -> str:
    """Return an idempotency key no other test call has used."""
    return f"test-key-{next(_keys):06d}"


class Api:
    """A small client that signs each request the way OpenClaw does."""

    def __init__(self, app: Flask) -> None:
        """Wrap the app's test client."""
        self._client = app.test_client()

    def call(
        self,
        method: str,
        path: str,
        body: dict[str, Any] | None = None,
        *,
        member: str | None = "geoff",
        key: str | None = "auto",
        query: str = "",
        signed: bool = True,
    ) -> TestResponse:
        """Send one request.

        Args:
            method: HTTP method.
            path: Path under ``/api/v2``.
            body: JSON body, if any.
            member: Value of the member header; None omits it.
            key: Idempotency key; ``"auto"`` makes a fresh one for a
                mutation, None omits the header.
            query: Query string without the ``?``.
            signed: Whether to send a bearer token.

        Returns:
            The response.
        """
        full_path = f"/api/v2{path}"
        raw = json.dumps(body).encode() if body is not None else b""
        headers: dict[str, str] = {}
        if signed:
            with patch.dict(os.environ, SECRET_ENV):
                headers.update(bearer_header("rubotpaul", method, full_path, raw))
        if method != "GET":
            if member is not None:
                headers[MEMBER_HEADER] = member
            if key == "auto":
                headers[IDEMPOTENCY_HEADER] = _new_key()
            elif key is not None:
                headers[IDEMPOTENCY_HEADER] = key
        url = f"{full_path}?{query}" if query else full_path
        with patch.dict(os.environ, SECRET_ENV):
            return self._client.open(
                url,
                method=method,
                data=raw,
                headers=headers,
                content_type="application/json" if body is not None else None,
            )


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a temporary database path; create_app migrates it."""
    return str(tmp_path / "api_v2.db")


@pytest.fixture()
def app(db_path: str) -> Flask:
    """Create the app with one household member, geoff."""
    with patch.dict(os.environ, SECRET_ENV):
        application = create_app(db_path=db_path)
    application.config["TESTING"] = True
    MemberStore(db_path).add("geoff", "Geoff", created_by="test")
    return application


@pytest.fixture()
def api(app: Flask) -> Api:
    """Return a signing client."""
    return Api(app)


def _error(response: TestResponse) -> dict[str, Any]:
    """Return a response's error object, checking the envelope's shape."""
    payload = response.get_json()
    assert set(payload) == {"error"}
    assert set(payload["error"]) == {"code", "message", "details"}
    error: dict[str, Any] = payload["error"]
    return error


class TestAuthentication:
    """v2 uses the same request-bound bearer token as v1."""

    @pytest.mark.parametrize(
        ("method", "path"),
        [("GET", "/items"), ("GET", "/members"), ("POST", "/items")],
    )
    def test_unsigned_request_is_unauthorized(
        self, api: Api, method: str, path: str
    ) -> None:
        """No token, no access, and the error is coded."""
        response = api.call(
            method, path, {} if method == "POST" else None, signed=False
        )

        assert response.status_code == 401
        assert _error(response)["code"] == "unauthorized"

    def test_token_for_another_path_is_rejected(self, app: Flask) -> None:
        """A token is bound to one path."""
        with patch.dict(os.environ, SECRET_ENV):
            headers = bearer_header("rubotpaul", "GET", "/api/v2/members")
            response = app.test_client().get("/api/v2/items", headers=headers)

        assert response.status_code == 401


class TestErrorEnvelope:
    """Every error has a stable code inside the same envelope."""

    def test_unknown_route_is_not_found(self, api: Api) -> None:
        """Framework errors use the envelope too."""
        response = api.call("GET", "/no-such-capability")

        assert response.status_code == 404
        assert _error(response)["code"] == "not_found"

    def test_wrong_method_is_method_not_allowed(self, api: Api) -> None:
        """A capability called with the wrong verb says so."""
        response = api.call("DELETE", "/items")

        assert response.status_code == 405
        assert _error(response)["code"] == "method_not_allowed"

    def test_body_that_is_not_an_object_is_a_bad_request(self, api: Api) -> None:
        """A JSON array is not a request body."""
        response = api.call("POST", "/items", body=None)

        assert response.status_code == 400
        assert _error(response)["code"] == "bad_request"

    def test_invalid_body_lists_fields_without_echoing_values(self, api: Api) -> None:
        """Validation failures name the field and the problem only."""
        response = api.call(
            "POST", "/items", {"name": "milk", "category": "sk-secret-looking-value"}
        )

        error = _error(response)
        assert response.status_code == 400
        assert error["code"] == "validation_failed"
        assert [f["field"] for f in error["details"]["fields"]] == ["category"]
        assert "sk-secret-looking-value" not in json.dumps(error)

    def test_unknown_body_field_is_rejected(self, api: Api) -> None:
        """A typo in a field name is an error, not silently ignored."""
        response = api.call("POST", "/items", {"name": "milk", "categry": "dairy"})

        assert response.status_code == 400
        assert _error(response)["details"]["fields"][0]["field"] == "categry"

    def test_domain_error_has_its_own_code(self, api: Api) -> None:
        """A missing item is item_not_found, not a bare 404."""
        response = api.call("GET", "/items/99999")

        assert response.status_code == 404
        assert _error(response)["code"] == "item_not_found"


class TestActor:
    """Every mutation names an active household member."""

    def test_mutation_without_a_member_is_refused(self, api: Api) -> None:
        """Attribution is required, and nothing is written."""
        response = api.call("POST", "/items", {"name": "milk"}, member=None)

        assert response.status_code == 400
        assert _error(response)["code"] == "member_required"
        assert api.call("GET", "/items", query="q=milk").get_json() == {"items": []}

    def test_unknown_member_is_refused(self, api: Api) -> None:
        """A name the household doesn't have can't act."""
        response = api.call("POST", "/items", {"name": "milk"}, member="mallory")

        assert response.status_code == 403
        assert _error(response)["code"] == "member_unknown"

    def test_deactivated_member_is_refused(self, api: Api, db_path: str) -> None:
        """A deactivated member keeps their history but can't act."""
        MemberStore(db_path).add("sam", "Sam", created_by="test")
        MemberStore(db_path).set_active("sam", active=False)

        response = api.call("POST", "/items", {"name": "milk"}, member="sam")

        assert response.status_code == 403
        assert _error(response)["code"] == "member_unknown"

    def test_actor_is_recorded_as_member_at_channel(
        self, api: Api, db_path: str
    ) -> None:
        """The audit trail shows who, and through which channel."""
        created = api.call("POST", "/items", {"name": "Oat Milk"}, member="Geoff")

        history = HouseholdItemStore(db_path).history(created.get_json()["id"])
        assert created.status_code == 201
        assert history[0].actor == "geoff@rubotpaul"

    def test_reads_need_no_member(self, api: Api) -> None:
        """Only changes are attributed."""
        assert api.call("GET", "/items", member=None).status_code == 200


class TestIdempotency:
    """A retried mutation returns the first response and changes nothing."""

    def test_mutation_without_a_key_is_refused(self, api: Api) -> None:
        """Every change needs a key."""
        response = api.call("POST", "/items", {"name": "milk"}, key=None)

        assert response.status_code == 400
        assert _error(response)["code"] == "idempotency_key_required"

    @pytest.mark.parametrize("key", ["short", "has a space in it", "x" * 129, "é" * 10])
    def test_malformed_key_is_refused(self, api: Api, key: str) -> None:
        """Keys are 8 to 128 printable ASCII characters without spaces."""
        response = api.call("POST", "/items", {"name": "milk"}, key=key)

        assert response.status_code == 400
        assert _error(response)["code"] == "idempotency_key_invalid"

    def test_retry_replays_the_response_and_changes_nothing(
        self, api: Api, db_path: str
    ) -> None:
        """Same key, same request: same answer, one item, one event."""
        first = api.call("POST", "/items", {"name": "milk"}, key="retry-key-0001")
        second = api.call("POST", "/items", {"name": "milk"}, key="retry-key-0001")

        assert first.status_code == 201
        assert second.status_code == 201
        assert second.get_json() == first.get_json()
        assert REPLAY_HEADER not in first.headers
        assert second.headers[REPLAY_HEADER] == "true"
        events = HouseholdItemStore(db_path).history(first.get_json()["id"])
        assert len(events) == 1

    def test_without_the_key_a_repeat_would_answer_differently(self, api: Api) -> None:
        """The replay is the stored response, not a second execution."""
        first = api.call("POST", "/items", {"name": "milk"})
        again = api.call("POST", "/items", {"name": "milk"})

        assert (first.status_code, again.status_code) == (201, 200)

    def test_key_reused_for_a_different_request_is_a_conflict(self, api: Api) -> None:
        """One key, one change."""
        api.call("POST", "/items", {"name": "milk"}, key="reused-key-0001")

        response = api.call("POST", "/items", {"name": "eggs"}, key="reused-key-0001")

        assert response.status_code == 409
        assert _error(response)["code"] == "idempotency_key_reused"
        assert api.call("GET", "/items", query="q=eggs").get_json() == {"items": []}

    def test_key_reused_by_another_member_is_a_conflict(
        self, api: Api, db_path: str
    ) -> None:
        """The actor is part of the request."""
        MemberStore(db_path).add("jess", "Jess", created_by="test")
        api.call("POST", "/items", {"name": "milk"}, key="shared-key-0001")

        response = api.call(
            "POST", "/items", {"name": "milk"}, key="shared-key-0001", member="jess"
        )

        assert response.status_code == 409
        assert _error(response)["code"] == "idempotency_key_reused"

    def test_failed_request_releases_its_key(self, api: Api) -> None:
        """A failure changed nothing, so the same key can be retried."""
        failed = api.call(
            "POST", "/items/99999/aliases", {"alias": "x"}, key="failed-key-0001"
        )
        retried = api.call(
            "POST", "/items/99999/aliases", {"alias": "x"}, key="failed-key-0001"
        )

        assert failed.status_code == 404
        assert retried.status_code == 404
        assert _error(retried)["code"] == "item_not_found"

    def test_request_still_running_is_reported(self, api: Api, db_path: str) -> None:
        """A key that is claimed but not finished is in progress."""
        from grocery_butler.idempotency import IdempotencyStore, request_hash

        body = json.dumps({"name": "milk"}).encode()
        IdempotencyStore(db_path).claim(
            "running-key-0001",
            request_hash("POST", "/api/v2/items", body + b"\ngeoff@rubotpaul"),
        )

        response = api.call("POST", "/items", {"name": "milk"}, key="running-key-0001")

        assert response.status_code == 409
        assert _error(response)["code"] == "request_in_progress"


class TestMembers:
    """Members are named people without logins (ADR 0002)."""

    def test_first_member_can_be_created_without_a_member_header(
        self, db_path: str
    ) -> None:
        """Bootstrap: there is nobody to name yet."""
        with patch.dict(os.environ, SECRET_ENV):
            empty_app = create_app(db_path=db_path)
        api = Api(empty_app)

        response = api.call(
            "POST", "/members", {"key": "Geoff", "display_name": "Geoff"}, member=None
        )

        assert response.status_code == 201
        assert response.get_json()["key"] == "geoff"
        assert response.get_json()["created_by"] == "rubotpaul"

    def test_bootstrap_reopens_when_every_member_is_deactivated(
        self, api: Api, db_path: str
    ) -> None:
        """With nobody active there is nobody to name, so the door reopens."""
        MemberStore(db_path).set_active("geoff", active=False)

        response = api.call(
            "POST", "/members", {"key": "jess", "display_name": "Jess"}, member=None
        )

        assert response.status_code == 201
        assert response.get_json()["created_by"] == "rubotpaul"

    def test_once_a_member_exists_the_header_is_required(self, api: Api) -> None:
        """Bootstrap ends with the first member."""
        response = api.call(
            "POST", "/members", {"key": "jess", "display_name": "Jess"}, member=None
        )

        assert response.status_code == 400
        assert _error(response)["code"] == "member_required"

    def test_add_list_deactivate_reactivate(self, api: Api) -> None:
        """The member lifecycle."""
        added = api.call("POST", "/members", {"key": "jess", "display_name": "Jess"})
        api.call("POST", "/members/jess/deactivate")
        active = api.call("GET", "/members").get_json()["members"]
        everyone = api.call("GET", "/members", query="include_inactive=true")
        api.call("POST", "/members/jess/reactivate")
        after = api.call("GET", "/members").get_json()["members"]

        assert added.status_code == 201
        assert added.get_json()["created_by"] == "geoff@rubotpaul"
        assert [m["key"] for m in active] == ["geoff"]
        assert [m["key"] for m in everyone.get_json()["members"]] == ["geoff", "jess"]
        assert [m["key"] for m in after] == ["geoff", "jess"]

    @pytest.mark.parametrize(
        ("body", "status", "code"),
        [
            ({"key": "geoff", "display_name": "Other"}, 409, "member_exists"),
            ({"key": "Not Valid!", "display_name": "X"}, 400, "invalid_member_key"),
            ({"key": "jess", "display_name": ""}, 400, "validation_failed"),
        ],
    )
    def test_bad_member_requests(
        self, api: Api, body: dict[str, str], status: int, code: str
    ) -> None:
        """Each failure has its own code."""
        response = api.call("POST", "/members", body)

        assert response.status_code == status
        assert _error(response)["code"] == code

    def test_deactivating_an_unknown_member_is_not_found(self, api: Api) -> None:
        """You can only deactivate someone who exists."""
        response = api.call("POST", "/members/nobody/deactivate")

        assert response.status_code == 404
        assert _error(response)["code"] == "member_not_found"


class TestItems:
    """Items, their names, their merges and their current state."""

    def _create(self, api: Api, name: str, **extra: Any) -> dict[str, Any]:
        """Create an item and return its JSON."""
        response = api.call("POST", "/items", {"name": name, **extra})
        assert response.status_code == 201
        item: dict[str, Any] = response.get_json()
        return item

    def test_create_returns_the_item_with_unknown_state(self, api: Api) -> None:
        """A new item has no evidence yet."""
        item = self._create(api, "Oat Milk", category="dairy")

        assert item["name"] == "oat milk"
        assert item["display_name"] == "Oat Milk"
        assert item["category"] == "dairy"
        assert item["aliases"] == ["oat milk"]
        assert item["open_need"] is None
        assert item["state"]["state"] == "unknown"
        assert item["state"]["observation"] is None

    def test_creating_an_existing_name_returns_it_with_200(self, api: Api) -> None:
        """The capability is find-or-create."""
        first = self._create(api, "oat milk")

        again = api.call("POST", "/items", {"name": "  OAT   milk "})

        assert again.status_code == 200
        assert again.get_json()["id"] == first["id"]

    def test_item_shows_its_state_and_why(self, api: Api, db_path: str) -> None:
        """The snapshot carries the observation behind it."""
        item = self._create(api, "eggs")
        InventoryLedger(db_path).record(
            NewObservation(
                item_id=item["id"],
                state=StockState.LOW,
                source=ObservationSource.HUMAN,
                actor="jess@rubotpaul",
                notes="two left",
            )
        )

        state = api.call("GET", f"/items/{item['id']}").get_json()["state"]

        assert state["state"] == "low"
        assert state["stale"] is False
        assert state["observation"]["actor"] == "jess@rubotpaul"
        assert state["observation"]["source"] == "human"
        assert state["observation"]["notes"] == "two left"

    def test_item_shows_its_open_need(self, api: Api, db_path: str) -> None:
        """What we plan to do about it is part of the picture."""
        item = self._create(api, "eggs")
        ReplenishmentNeeds(db_path).add_reason(
            item["id"], NeedReasonKind.OUT, source_key=INVENTORY_SOURCE, actor="test"
        )

        need = api.call("GET", f"/items/{item['id']}").get_json()["open_need"]

        assert need["status"] == "suggested"
        assert [r["kind"] for r in need["reasons"]] == ["out"]

    def test_list_is_sorted_and_searchable_by_alias(self, api: Api) -> None:
        """q matches names, display names and aliases."""
        plum = self._create(api, "plum jam")
        self._create(api, "apricot jam")
        api.call("POST", f"/items/{plum['id']}/aliases", {"alias": "PJ-Special"})

        names = [
            i["name"]
            for i in api.call("GET", "/items", query="q=JAM").get_json()["items"]
        ]
        by_alias = api.call("GET", "/items", query="q=pj-spec").get_json()["items"]

        assert names == ["apricot jam", "plum jam"]
        assert [i["id"] for i in by_alias] == [plum["id"]]
        assert "aliases" not in by_alias[0]

    def test_update_sets_policy_and_preferences(self, api: Api) -> None:
        """PATCH changes only the fields sent."""
        item = self._create(api, "peanut butter")

        response = api.call(
            "PATCH",
            f"/items/{item['id']}",
            {"staple_policy": "always_keep", "preferred_brand": "Adams"},
        )

        updated = response.get_json()
        assert response.status_code == 200
        assert updated["staple_policy"] == "always_keep"
        assert updated["preferred_brand"] == "Adams"
        assert updated["display_name"] == "Peanut Butter"

    def test_alias_conflict_and_removal_rules_are_coded(self, api: Api) -> None:
        """An alias can't be stolen, and an item's own name can't be removed."""
        jam = self._create(api, "jam")
        plum = self._create(api, "plum jam")

        stolen = api.call("POST", f"/items/{plum['id']}/aliases", {"alias": "jam"})
        own = api.call("POST", f"/items/{jam['id']}/aliases/remove", {"alias": "jam"})
        missing = api.call("POST", "/items/99999/aliases/remove", {"alias": "x"})

        assert (stolen.status_code, _error(stolen)["code"]) == (409, "alias_conflict")
        assert (own.status_code, _error(own)["code"]) == (409, "alias_not_removable")
        assert (missing.status_code, _error(missing)["code"]) == (404, "item_not_found")

    def test_alias_can_be_removed(self, api: Api) -> None:
        """Removing an alias takes it off the item."""
        item = self._create(api, "peanut butter")
        api.call("POST", f"/items/{item['id']}/aliases", {"alias": "pb"})

        response = api.call(
            "POST", f"/items/{item['id']}/aliases/remove", {"alias": "PB"}
        )

        assert response.get_json()["aliases"] == ["peanut butter"]

    def test_merge_folds_needs_and_unmerge_restores(
        self, api: Api, db_path: str
    ) -> None:
        """A merge joins identity and needs; an unmerge restores identity."""
        tomato = self._create(api, "tomato")
        tomatoes = self._create(api, "tomatoes")
        needs = ReplenishmentNeeds(db_path)
        needs.add_reason(
            tomato["id"], NeedReasonKind.OUT, source_key=INVENTORY_SOURCE, actor="t"
        )
        needs.add_reason(
            tomatoes["id"],
            NeedReasonKind.RECIPE_DEMAND,
            source_key="recipe:1",
            actor="t",
        )

        merged = api.call(
            "POST", f"/items/{tomatoes['id']}/merge", {"into_item_id": tomato["id"]}
        )
        hidden = api.call("GET", "/items", query="q=tomato").get_json()["items"]
        restored = api.call("POST", f"/items/{tomatoes['id']}/unmerge")

        survivor = merged.get_json()
        assert merged.status_code == 200
        assert survivor["id"] == tomato["id"]
        assert sorted(r["kind"] for r in survivor["open_need"]["reasons"]) == [
            "out",
            "recipe_demand",
        ]
        assert len(needs.list_needs()) == 1
        assert [i["id"] for i in hidden] == [tomato["id"]]
        assert restored.get_json()["active"] is True
        assert restored.get_json()["merged_into_id"] is None

    def test_merge_rule_violations_are_coded(self, api: Api) -> None:
        """An impossible merge is a conflict with its reason."""
        tomato = self._create(api, "tomato")

        itself = api.call(
            "POST", f"/items/{tomato['id']}/merge", {"into_item_id": tomato["id"]}
        )
        not_merged = api.call("POST", f"/items/{tomato['id']}/unmerge")

        assert (itself.status_code, _error(itself)["code"]) == (
            409,
            "merge_not_allowed",
        )
        assert _error(not_merged)["code"] == "merge_not_allowed"

    def test_history_explains_identity_and_observations(
        self, api: Api, db_path: str
    ) -> None:
        """The audit capability returns both trails."""
        item = self._create(api, "eggs")
        ledger = InventoryLedger(db_path)
        wrong = ledger.record(
            NewObservation(
                item_id=item["id"],
                state=StockState.OUT,
                source=ObservationSource.HUMAN,
                actor="geoff@rubotpaul",
            )
        )
        ledger.retract(wrong.id, actor="geoff@rubotpaul", notes="wrong shelf")

        history = api.call("GET", f"/items/{item['id']}/history").get_json()

        assert history["item_id"] == item["id"]
        assert [e["event_type"] for e in history["identity_events"]] == ["created"]
        assert [o["kind"] for o in history["observations"]] == [
            "observation",
            "retraction",
        ]
        assert history["observations"][0]["superseded_by_id"] is not None

    def test_history_of_a_missing_item_is_not_found(self, api: Api) -> None:
        """You can only explain an item that exists."""
        response = api.call("GET", "/items/99999/history")

        assert _error(response)["code"] == "item_not_found"


class TestV1IsUntouched:
    """v1 keeps its own error shape while v2 lands beside it."""

    def test_v1_errors_keep_their_string_shape(self, app: Flask) -> None:
        """ADR 0009: nothing in v1 changes."""
        response = app.test_client().get("/api/v1/inventory")

        assert response.status_code == 401
        assert isinstance(response.get_json()["error"], str)


def _item(api: Api, name: str) -> int:
    """Create an item and return its id."""
    response = api.call("POST", "/items", {"name": name})
    assert response.status_code == 201
    item_id: int = response.get_json()["id"]
    return item_id


class TestDirectObservations:
    """What a member says directly is recorded directly, as theirs."""

    def test_statement_is_recorded_and_raises_a_need(self, api: Api) -> None:
        """A member's "we're out" is human evidence and suggests a need."""
        item_id = _item(api, "oat milk")

        response = api.call(
            "POST",
            f"/items/{item_id}/observations",
            {"state": "out", "notes": "carton is empty"},
        )
        item = api.call("GET", f"/items/{item_id}").get_json()

        stored = response.get_json()
        assert response.status_code == 201
        assert stored["source"] == "human"
        assert stored["actor"] == "geoff@rubotpaul"
        assert stored["confidence"] == 1.0
        assert item["state"]["state"] == "out"
        assert item["open_need"]["status"] == "suggested"
        assert [r["kind"] for r in item["open_need"]["reasons"]] == ["out"]

    def test_stated_quantity_is_kept(self, api: Api) -> None:
        """A count a person states is stored with its unit."""
        item_id = _item(api, "oat milk")

        response = api.call(
            "POST",
            f"/items/{item_id}/observations",
            {"state": "on_hand", "quantity": 2, "unit": "carton"},
        )

        assert (response.get_json()["quantity"], response.get_json()["unit"]) == (
            2,
            "carton",
        )

    @pytest.mark.parametrize(
        "body",
        [{"state": "unknown"}, {"state": "low", "quantity": 2}, {"state": "empty"}],
        ids=["unknown-state", "quantity-without-unit", "not-a-state"],
    )
    def test_invalid_statements_are_validation_errors(
        self, api: Api, body: dict[str, Any]
    ) -> None:
        """The ledger's rules surface as coded validation errors."""
        item_id = _item(api, "oat milk")

        response = api.call("POST", f"/items/{item_id}/observations", body)

        assert response.status_code == 400
        assert _error(response)["code"] == "validation_failed"

    def test_observation_for_a_missing_item_is_not_found(self, api: Api) -> None:
        """You can only observe an item that exists."""
        response = api.call("POST", "/items/99999/observations", {"state": "low"})

        assert _error(response)["code"] == "item_not_found"

    def test_correct_and_retract_keep_history_and_resync_the_need(
        self, api: Api
    ) -> None:
        """A correction replaces the state; a retraction withdraws it."""
        item_id = _item(api, "oat milk")
        wrong = api.call(
            "POST", f"/items/{item_id}/observations", {"state": "out"}
        ).get_json()

        fixed = api.call(
            "POST", f"/observations/{wrong['id']}/correct", {"state": "on_hand"}
        )
        after_fix = api.call("GET", f"/items/{item_id}").get_json()
        retracted = api.call(
            "POST", f"/observations/{fixed.get_json()['id']}/retract", {"notes": "oops"}
        )
        after_retract = api.call("GET", f"/items/{item_id}").get_json()
        history = api.call("GET", f"/items/{item_id}/history").get_json()

        assert fixed.status_code == 201
        assert fixed.get_json()["supersedes_id"] == wrong["id"]
        assert after_fix["state"]["state"] == "on_hand"
        assert after_fix["open_need"] is None
        assert retracted.status_code == 201
        assert retracted.get_json()["kind"] == "retraction"
        assert after_retract["state"]["state"] == "unknown"
        assert len(history["observations"]) == 3

    def test_correcting_twice_or_a_missing_observation_is_coded(self, api: Api) -> None:
        """An already replaced or unknown observation has its own code."""
        item_id = _item(api, "oat milk")
        wrong = api.call(
            "POST", f"/items/{item_id}/observations", {"state": "out"}
        ).get_json()
        api.call("POST", f"/observations/{wrong['id']}/correct", {"state": "low"})

        again = api.call(
            "POST", f"/observations/{wrong['id']}/correct", {"state": "on_hand"}
        )
        missing = api.call("POST", "/observations/99999/retract", {})
        missing_fix = api.call("POST", "/observations/99999/correct", {"state": "low"})

        assert (again.status_code, _error(again)["code"]) == (
            409,
            "observation_superseded",
        )
        assert (missing.status_code, _error(missing)["code"]) == (
            404,
            "observation_not_found",
        )
        assert _error(missing_fix)["code"] == "observation_not_found"


class TestObservationBatches:
    """A model's proposals wait for a member before anything is recorded."""

    def _submit(self, api: Api, proposals: list[dict[str, Any]]) -> dict[str, Any]:
        """Submit a photo batch and return its JSON."""
        response = api.call(
            "POST",
            "/observation-batches",
            {"source": "photo", "proposals": proposals, "evidence_ref": "photo:1"},
        )
        assert response.status_code == 201
        batch: dict[str, Any] = response.get_json()
        return batch

    def test_submitting_changes_no_inventory_state(self, api: Api) -> None:
        """Evidence before mutation: a proposal is not an observation."""
        item_id = _item(api, "eggs")

        batch = self._submit(
            api, [{"name": "Eggs", "state": "out", "confidence": 0.95}]
        )
        item = api.call("GET", f"/items/{item_id}").get_json()

        assert batch["status"] == "open"
        assert batch["submitted_by"] == "geoff@rubotpaul"
        assert batch["proposals"][0]["item_id"] == item_id
        assert batch["proposals"][0]["needs_review"] is False
        assert item["state"]["state"] == "unknown"
        assert item["open_need"] is None

    def test_review_flags_and_listing(self, api: Api) -> None:
        """Unmatched and low-confidence proposals are flagged for review."""
        _item(api, "eggs")

        batch = self._submit(
            api,
            [
                {"name": "eggs", "state": "low", "confidence": 0.5},
                {"name": "dragonfruit", "state": "low", "confidence": 0.99},
            ],
        )
        listed = api.call("GET", "/observation-batches", query="status=open")
        fetched = api.call("GET", f"/observation-batches/{batch['id']}")

        assert [p["needs_review"] for p in batch["proposals"]] == [True, True]
        assert batch["proposals"][1]["item_id"] is None
        assert [b["id"] for b in listed.get_json()["batches"]] == [batch["id"]]
        assert fetched.get_json()["id"] == batch["id"]

    def test_resolving_records_what_the_member_decided(self, api: Api) -> None:
        """Confirm one, correct one, reject one: all applied together."""
        eggs = _item(api, "eggs")
        rice = _item(api, "rice")
        batch = self._submit(
            api,
            [
                {"name": "eggs", "state": "out", "confidence": 0.95},
                {"name": "rise", "state": "low", "confidence": 0.9},
                {"name": "ghost", "state": "low", "confidence": 0.9},
            ],
        )
        first, second, third = (p["id"] for p in batch["proposals"])

        response = api.call(
            "POST",
            f"/observation-batches/{batch['id']}/resolve",
            {
                "reviews": [
                    {"proposal_id": first, "action": "confirm"},
                    {"proposal_id": second, "action": "correct", "item_id": rice},
                    {"proposal_id": third, "action": "reject"},
                ]
            },
            member="geoff",
        )

        resolved = response.get_json()
        eggs_state = api.call("GET", f"/items/{eggs}").get_json()["state"]
        rice_state = api.call("GET", f"/items/{rice}").get_json()["state"]
        assert response.status_code == 200
        assert resolved["status"] == "confirmed"
        assert resolved["resolved_by"] == "geoff@rubotpaul"
        assert [p["decision"] for p in resolved["proposals"]] == [
            "confirmed",
            "corrected",
            "rejected",
        ]
        assert eggs_state["state"] == "out"
        assert eggs_state["observation"]["source"] == "photo"
        assert eggs_state["observation"]["confidence"] == 0.95
        assert eggs_state["observation"]["evidence_ref"] == "photo:1"
        assert rice_state["state"] == "low"
        assert rice_state["observation"]["source"] == "human"

    def test_undecided_proposals_are_listed_and_nothing_is_written(
        self, api: Api
    ) -> None:
        """Bulk accept never covers a proposal that needs review."""
        eggs = _item(api, "eggs")
        batch = self._submit(
            api,
            [
                {"name": "eggs", "state": "out", "confidence": 0.95},
                {"name": "ghost", "state": "low", "confidence": 0.99},
            ],
        )

        response = api.call(
            "POST",
            f"/observation-batches/{batch['id']}/resolve",
            {"accept_high_confidence": True},
        )

        error = _error(response)
        assert response.status_code == 409
        assert error["code"] == "reviews_missing"
        assert error["details"]["proposal_ids"] == [batch["proposals"][1]["id"]]
        assert (
            api.call("GET", f"/items/{eggs}").get_json()["state"]["state"] == "unknown"
        )

    def test_retrying_a_resolve_does_not_record_twice(self, api: Api) -> None:
        """Idempotency covers the one call where a double-apply would hurt most."""
        eggs = _item(api, "eggs")
        batch = self._submit(
            api, [{"name": "eggs", "state": "out", "confidence": 0.95}]
        )
        body = {"accept_high_confidence": True}
        path = f"/observation-batches/{batch['id']}/resolve"

        first = api.call("POST", path, body, key="resolve-key-0001")
        retry = api.call("POST", path, body, key="resolve-key-0001")
        fresh_key = api.call("POST", path, body)
        history = api.call("GET", f"/items/{eggs}/history").get_json()

        assert first.status_code == 200
        assert retry.get_json() == first.get_json()
        assert retry.headers[REPLAY_HEADER] == "true"
        assert (fresh_key.status_code, _error(fresh_key)["code"]) == (
            409,
            "batch_not_open",
        )
        assert len(history["observations"]) == 1

    def test_reject_closes_the_batch(self, api: Api) -> None:
        """A rejected batch records nothing."""
        eggs = _item(api, "eggs")
        batch = self._submit(
            api, [{"name": "eggs", "state": "out", "confidence": 0.95}]
        )

        response = api.call("POST", f"/observation-batches/{batch['id']}/reject")

        assert response.get_json()["status"] == "rejected"
        assert (
            api.call("GET", f"/items/{eggs}").get_json()["state"]["state"] == "unknown"
        )

    @pytest.mark.parametrize(
        ("body", "status", "code"),
        [
            ({"source": "photo", "proposals": []}, 400, "batch_invalid"),
            (
                {
                    "source": "system",
                    "proposals": [{"name": "x", "state": "low", "confidence": 1}],
                },
                400,
                "batch_invalid",
            ),
            (
                {
                    "source": "photo",
                    "proposals": [{"name": "x", "state": "low", "confidence": 2}],
                },
                400,
                "validation_failed",
            ),
            (
                {
                    "source": "photo",
                    "proposals": [{"name": "x", "state": "unknown", "confidence": 1}],
                },
                400,
                "validation_failed",
            ),
        ],
        ids=["empty", "system-source", "bad-confidence", "unknown-state"],
    )
    def test_bad_batches_are_refused(
        self, api: Api, body: dict[str, Any], status: int, code: str
    ) -> None:
        """Each kind of bad batch has its code."""
        response = api.call("POST", "/observation-batches", body)

        assert (response.status_code, _error(response)["code"]) == (status, code)

    def test_bad_reviews_and_missing_batches_are_coded(self, api: Api) -> None:
        """A review that can't apply, an unknown batch, a bad status filter."""
        _item(api, "eggs")
        batch = self._submit(
            api, [{"name": "ghost", "state": "low", "confidence": 0.9}]
        )
        proposal_id = batch["proposals"][0]["id"]

        unmatched = api.call(
            "POST",
            f"/observation-batches/{batch['id']}/resolve",
            {"reviews": [{"proposal_id": proposal_id, "action": "confirm"}]},
        )
        missing = api.call("GET", "/observation-batches/99999")
        bad_filter = api.call("GET", "/observation-batches", query="status=nope")

        assert (unmatched.status_code, _error(unmatched)["code"]) == (
            409,
            "review_invalid",
        )
        assert (missing.status_code, _error(missing)["code"]) == (
            404,
            "batch_not_found",
        )
        assert (bad_filter.status_code, _error(bad_filter)["code"]) == (
            400,
            "validation_failed",
        )


class TestNeeds:
    """A member's decisions about what to buy."""

    def _suggested(self, api: Api, name: str = "eggs") -> dict[str, Any]:
        """Make an item low and return its suggested need."""
        item_id = _item(api, name)
        api.call("POST", f"/items/{item_id}/observations", {"state": "low"})
        need: dict[str, Any] = api.call("GET", f"/items/{item_id}").get_json()[
            "open_need"
        ]
        return need

    def test_manual_request_starts_approved_with_its_reason(self, api: Api) -> None:
        """A member asking for something has already decided."""
        item_id = _item(api, "birthday candles")

        response = api.call(
            "POST", "/needs", {"item_id": item_id, "note": "for Saturday"}
        )

        need = response.get_json()
        assert response.status_code == 201
        assert need["status"] == "approved"
        assert need["reasons"][0]["kind"] == "manual_request"
        assert need["reasons"][0]["detail"] == {"note": "for Saturday"}
        assert need["history"][0]["actor"] == "geoff@rubotpaul"

    def test_approve_records_who_and_why(self, api: Api) -> None:
        """Approval is a member's decision, with a note."""
        need = self._suggested(api)

        response = api.call(
            "POST", f"/needs/{need['id']}/approve", {"note": "yes please"}
        )

        approved = response.get_json()
        assert approved["status"] == "approved"
        assert approved["history"][-1]["actor"] == "geoff@rubotpaul"
        assert approved["history"][-1]["note"] == "yes please"
        assert approved["history"][-1]["from_status"] == "suggested"

    def test_decisions_work_without_a_body(self, api: Api) -> None:
        """A note is optional, so is the body."""
        need = self._suggested(api)

        response = api.call("POST", f"/needs/{need['id']}/dismiss")

        assert response.status_code == 200
        assert response.get_json()["status"] == "dismissed"

    def test_defer_until_a_date_then_reopen(self, api: Api) -> None:
        """A snooze has an optional end, and can be ended early."""
        need = self._suggested(api)

        deferred = api.call(
            "POST",
            f"/needs/{need['id']}/defer",
            {"until": "2026-12-05T09:00:00+00:00", "note": "after the trip"},
        )
        reopened = api.call("POST", f"/needs/{need['id']}/reopen")

        assert deferred.get_json()["status"] == "deferred"
        assert deferred.get_json()["deferred_until"].startswith("2026-12-05T09:00:00")
        assert reopened.get_json()["status"] == "suggested"
        assert reopened.get_json()["deferred_until"] is None

    def test_cancel_and_listing_by_status(self, api: Api) -> None:
        """The default list is open needs; statuses can be chosen."""
        kept = self._suggested(api, "eggs")
        gone = self._suggested(api, "rice")
        api.call("POST", f"/needs/{gone['id']}/cancel")

        open_needs = api.call("GET", "/needs").get_json()["needs"]
        chosen = api.call("GET", "/needs", query="status=cancelled&status=dismissed")
        bad = api.call("GET", "/needs", query="status=nope")

        assert [n["id"] for n in open_needs] == [kept["id"]]
        assert [n["id"] for n in chosen.get_json()["needs"]] == [gone["id"]]
        assert _error(bad)["code"] == "validation_failed"

    def test_a_move_the_state_machine_forbids_is_coded(self, api: Api) -> None:
        """A closed need can't be approved; the message lists what can happen."""
        need = self._suggested(api)
        api.call("POST", f"/needs/{need['id']}/dismiss")

        response = api.call("POST", f"/needs/{need['id']}/approve")

        assert response.status_code == 409
        assert _error(response)["code"] == "need_move_not_allowed"

    def test_missing_need_and_missing_item_are_coded(self, api: Api) -> None:
        """Unknown ids have their own codes."""
        need = api.call("GET", "/needs/99999")
        move = api.call("POST", "/needs/99999/approve")
        request_for = api.call("POST", "/needs", {"item_id": 99999})

        assert _error(need)["code"] == "need_not_found"
        assert _error(move)["code"] == "need_not_found"
        assert _error(request_for)["code"] == "item_not_found"


class TestPlans:
    """The shopping plan: a numbered version OpenClaw can read and rebuild."""

    def _approved(self, api: Api, name: str) -> int:
        """Create an item with an approved manual request; return the item id."""
        item_id = _item(api, name)
        api.call("POST", "/needs", {"item_id": item_id})
        return item_id

    def _recipe(self, db_path: str, name: str, ingredient: str) -> int:
        """Save a one-ingredient recipe and return its id."""
        from grocery_butler.models import (
            Ingredient,
            IngredientCategory,
            ParsedMeal,
            Unit,
        )
        from grocery_butler.recipe_store import RecipeStore

        return RecipeStore(db_path).save_recipe(
            ParsedMeal(
                name=name,
                servings=2,
                known_recipe=True,
                needs_confirmation=False,
                purchase_items=[
                    Ingredient(
                        ingredient=ingredient,
                        quantity=1.0,
                        unit=Unit.CUP,
                        category=IngredientCategory.DAIRY,
                    )
                ],
                pantry_items=[],
            )
        )

    def test_no_plan_yet_is_not_found(self, api: Api) -> None:
        """Before the first build there is no current plan."""
        response = api.call("GET", "/plans/latest")

        assert (response.status_code, _error(response)["code"]) == (
            404,
            "plan_not_found",
        )
        assert api.call("GET", "/plans").get_json() == {"plans": []}

    def test_build_from_needs_alone(self, api: Api) -> None:
        """A plan needs no meals (ADR 0004), and marks its needs planned."""
        item_id = self._approved(api, "oat milk")

        response = api.call("POST", "/plans")

        plan = response.get_json()
        need = api.call("GET", f"/items/{item_id}").get_json()["open_need"]
        assert response.status_code == 201
        assert plan["version"] == 1
        assert plan["is_current"] is True
        assert plan["created_by"] == "geoff@rubotpaul"
        assert [line["display_name"] for line in plan["lines"]] == ["Oat Milk"]
        assert plan["lines"][0]["amounts"] == []
        assert plan["lines"][0]["reasons"][0]["kind"] == "manual_request"
        assert need["status"] == "planned"

    def test_rebuilding_unchanged_inputs_returns_the_same_version_with_200(
        self, api: Api
    ) -> None:
        """Nothing changed, nothing new is stored."""
        self._approved(api, "oat milk")
        first = api.call("POST", "/plans")

        again = api.call("POST", "/plans", {"meals": []})

        assert (first.status_code, again.status_code) == (201, 200)
        assert again.get_json()["version"] == 1
        assert len(api.call("GET", "/plans").get_json()["plans"]) == 1

    def test_a_change_makes_a_new_version_and_the_old_one_reads_as_stale(
        self, api: Api, db_path: str
    ) -> None:
        """Version 1 stays readable, and says it no longer reflects the household."""
        self._approved(api, "oat milk")
        api.call("POST", "/plans")
        self._approved(api, "eggs")
        recipe_id = self._recipe(db_path, "Porridge", "oats")

        stale = api.call("GET", "/plans/1").get_json()
        second = api.call(
            "POST", "/plans", {"meals": [{"recipe_id": recipe_id, "servings": 4}]}
        )
        listed = api.call("GET", "/plans").get_json()["plans"]

        plan = second.get_json()
        assert stale["is_current"] is False
        assert second.status_code == 201
        assert plan["version"] == 2
        assert plan["meals"] == [{"recipe_id": recipe_id, "servings": 4}]
        assert sorted(line["display_name"] for line in plan["lines"]) == [
            "Eggs",
            "Oat Milk",
            "Oats",
        ]
        oats = next(line for line in plan["lines"] if line["display_name"] == "Oats")
        assert oats["amounts"] == [{"quantity": 2.0, "unit": "cup"}]
        assert [(p["version"], p["line_count"]) for p in listed] == [(1, 1), (2, 3)]
        assert api.call("GET", "/plans/latest").get_json()["version"] == 2

    @pytest.mark.parametrize(
        ("body", "status", "code"),
        [
            ({"meals": [{"recipe_id": 99999}]}, 404, "recipe_not_found"),
            ({"meals": [{"recipe_id": 1, "servings": 0}]}, 400, "validation_failed"),
            ({"meal": []}, 400, "validation_failed"),
        ],
        ids=["unknown-recipe", "zero-servings", "unknown-field"],
    )
    def test_bad_plan_requests_are_coded(
        self, api: Api, body: dict[str, Any], status: int, code: str
    ) -> None:
        """Each failure has its code, and no version is stored."""
        response = api.call("POST", "/plans", body)

        assert (response.status_code, _error(response)["code"]) == (status, code)
        assert api.call("GET", "/plans").get_json() == {"plans": []}

    def test_listing_a_recipe_twice_is_plan_invalid(
        self, api: Api, db_path: str
    ) -> None:
        """Say how many servings instead."""
        recipe_id = self._recipe(db_path, "Porridge", "oats")

        response = api.call(
            "POST",
            "/plans",
            {"meals": [{"recipe_id": recipe_id}, {"recipe_id": recipe_id}]},
        )

        assert (response.status_code, _error(response)["code"]) == (400, "plan_invalid")

    def test_missing_version_is_not_found(self, api: Api) -> None:
        """An unknown version has its own code."""
        response = api.call("GET", "/plans/7")

        assert _error(response)["code"] == "plan_not_found"


class TestFulfillment:
    """Adapters are declared, and any plan can be exported for any store."""

    def test_adapters_declare_what_they_do(self, api: Api) -> None:
        """OpenClaw can see what each adapter can and can't do."""
        adapters = api.call("GET", "/fulfillment/adapters").get_json()["adapters"]

        assert adapters == [
            {
                "name": "export",
                "display_name": "Shopping list export",
                "capabilities": ["export_list"],
                "authorization": "none",
                "data_freshness": adapters[0]["data_freshness"],
                "human_required": True,
                "enforces": [],
            }
        ]

    @pytest.mark.parametrize(
        ("fmt", "marker"),
        [
            ("text", "[ ] Oat Milk: the usual (requested)"),
            ("csv", "Oat Milk"),
            ("json", '"item": "Oat Milk"'),
        ],
    )
    def test_export_in_each_format(self, api: Api, fmt: str, marker: str) -> None:
        """The same plan in text, CSV and JSON, tied to its version and hash."""
        item_id = _item(api, "oat milk")
        api.call("POST", "/needs", {"item_id": item_id})
        plan = api.call("POST", "/plans").get_json()

        response = api.call("GET", "/plans/1/export", query=f"format={fmt}")

        export = response.get_json()
        assert response.status_code == 200
        assert export["adapter"] == "export"
        assert export["status"] == "created"
        assert export["format"] == fmt
        assert export["plan_version"] == 1
        assert export["plan_content_hash"] == plan["content_hash"]
        assert marker in export["content"]
        assert "Nothing has been ordered" in export["human_action"]

    def test_text_is_the_default_format(self, api: Api) -> None:
        """No format means the checklist."""
        api.call("POST", "/plans")

        export = api.call("GET", "/plans/1/export").get_json()

        assert export["format"] == "text"
        assert export["content"] == "Shopping plan v1\n\nNothing to buy.\n"

    def test_exporting_changes_nothing(self, api: Api) -> None:
        """An export is a read: no new version, and the need stays planned."""
        item_id = _item(api, "oat milk")
        api.call("POST", "/needs", {"item_id": item_id})
        api.call("POST", "/plans")

        api.call("GET", "/plans/1/export")
        api.call("GET", "/plans/1/export", query="format=csv")

        need = api.call("GET", f"/items/{item_id}").get_json()["open_need"]
        assert len(api.call("GET", "/plans").get_json()["plans"]) == 1
        assert need["status"] == "planned"

    def test_unknown_format_and_missing_plan_are_coded(self, api: Api) -> None:
        """Each failure has its code."""
        api.call("POST", "/plans")

        bad_format = api.call("GET", "/plans/1/export", query="format=pdf")
        missing = api.call("GET", "/plans/9/export")

        assert (bad_format.status_code, _error(bad_format)["code"]) == (
            400,
            "validation_failed",
        )
        assert "csv, json, text" in _error(bad_format)["message"]
        assert (missing.status_code, _error(missing)["code"]) == (404, "plan_not_found")


class TestConstraints:
    """What must hold, and what the household would merely like."""

    def test_allergen_exclusion_reaches_every_plan_line_and_the_export(
        self, api: Api
    ) -> None:
        """The whole path: constraint, plan line, export, unenforced report."""
        item_id = _item(api, "oat milk")
        api.call("POST", "/needs", {"item_id": item_id})

        added = api.call(
            "POST",
            "/constraints",
            {"kind": "allergen_exclusion", "value": " Peanut ", "note": "anaphylaxis"},
        )
        plan = api.call("POST", "/plans").get_json()
        export = api.call("GET", "/plans/1/export").get_json()

        constraint = added.get_json()
        assert added.status_code == 201
        assert (constraint["value"], constraint["scope"]) == ("peanut", "household")
        assert constraint["created_by"] == "geoff@rubotpaul"
        assert plan["lines"][0]["hard_constraints"] == [
            {"kind": "allergen_exclusion", "value": "peanut", "scope": "household"}
        ]
        assert plan["lines"][0]["constraints"] == {}
        assert "{MUST: no peanut}" in export["content"]
        assert export["unenforced_constraints"] == [
            {
                "item_id": item_id,
                "display_name": "Oat Milk",
                "kind": "allergen_exclusion",
                "value": "peanut",
            }
        ]
        assert "marked MUST" in export["human_action"]

    def test_adding_a_constraint_makes_the_current_plan_stale(self, api: Api) -> None:
        """An approval built on the old plan can't survive a new allergen."""
        item_id = _item(api, "oat milk")
        api.call("POST", "/needs", {"item_id": item_id})
        api.call("POST", "/plans")

        api.call("POST", "/constraints", {"kind": "dietary_rule", "value": "kosher"})

        assert api.call("GET", "/plans/1").get_json()["is_current"] is False

    def test_item_constraint_listing_and_removal(self, api: Api) -> None:
        """Do-not-substitute is about one item; removal keeps the record."""
        item_id = _item(api, "oat milk")
        api.call("POST", "/constraints", {"kind": "allergen_exclusion", "value": "soy"})
        added = api.call(
            "POST", "/constraints", {"kind": "do_not_substitute", "item_id": item_id}
        ).get_json()

        for_item = api.call("GET", "/constraints", query=f"item_id={item_id}")
        removed = api.call("POST", f"/constraints/{added['id']}/deactivate")
        active = api.call("GET", "/constraints").get_json()
        everything = api.call("GET", "/constraints", query="include_inactive=true")

        assert [c["kind"] for c in for_item.get_json()["constraints"]] == [
            "allergen_exclusion",
            "do_not_substitute",
        ]
        assert removed.get_json()["active"] is False
        assert removed.get_json()["deactivated_by"] == "geoff@rubotpaul"
        assert [c["kind"] for c in active["constraints"]] == ["allergen_exclusion"]
        assert len(everything.get_json()["constraints"]) == 2
        assert "peanut" in active["common_allergens"]

    @pytest.mark.parametrize(
        ("body", "status", "code"),
        [
            ({"kind": "allergen_exclusion", "value": " "}, 400, "constraint_invalid"),
            ({"kind": "do_not_substitute"}, 400, "constraint_invalid"),
            ({"kind": "do_not_substitute", "item_id": 99999}, 404, "item_not_found"),
            ({"kind": "brand", "value": "Clover"}, 400, "validation_failed"),
            (
                {"kind": "allergen_exclusion", "value": "x", "strength": "soft"},
                400,
                "validation_failed",
            ),
        ],
        ids=["no-value", "no-item", "unknown-item", "soft-kind", "unknown-field"],
    )
    def test_bad_constraints_are_refused(
        self, api: Api, body: dict[str, Any], status: int, code: str
    ) -> None:
        """Each failure has its code, and nothing is stored."""
        response = api.call("POST", "/constraints", body)

        assert (response.status_code, _error(response)["code"]) == (status, code)
        assert api.call("GET", "/constraints").get_json()["constraints"] == []

    def test_a_hard_kind_cannot_be_stored_as_a_soft_preference(self, api: Api) -> None:
        """ADR 0012: there is no route from an allergen to a preference."""
        response = api.call(
            "POST",
            "/soft-preferences",
            {"kind": "allergen_exclusion", "value": "peanut"},
        )

        assert (response.status_code, _error(response)["code"]) == (
            400,
            "validation_failed",
        )
        assert api.call("GET", "/soft-preferences").get_json() == {"preferences": []}
        assert api.call("GET", "/constraints").get_json()["constraints"] == []

    def test_soft_preferences_for_an_item_combine_every_source(self, api: Api) -> None:
        """The item's own brand, plus stored household and item preferences."""
        item_id = _item(api, "oat milk")
        api.call("PATCH", f"/items/{item_id}", {"preferred_brand": "Oatly"})
        api.call("POST", "/soft-preferences", {"kind": "organic"})

        added = api.call(
            "POST", "/soft-preferences", {"kind": "generic", "item_id": item_id}
        )
        household = api.call("GET", "/soft-preferences").get_json()["preferences"]

        assert added.status_code == 201
        assert [(p["kind"], p["source"]) for p in added.get_json()["preferences"]] == [
            ("brand", "item"),
            ("generic", "preference"),
            ("organic", "preference"),
        ]
        assert [p["kind"] for p in household] == ["organic"]

    def test_removing_a_missing_constraint_is_not_found(self, api: Api) -> None:
        """You can only remove what exists."""
        response = api.call("POST", "/constraints/99999/deactivate")

        assert (response.status_code, _error(response)["code"]) == (
            404,
            "constraint_not_found",
        )
