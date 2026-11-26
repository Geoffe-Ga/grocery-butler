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
