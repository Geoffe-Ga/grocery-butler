"""Tests for the /api/v1 destructive endpoints with staged confirmation.

Covers the staged-confirmation flow: ``/brands/set`` and
``/preferences/set`` stage into ``pending_actions`` and execute only
through ``/actions/confirm`` (chat-based draft → confirm → execute per
PIVOT.md). The retired Safeway ``/order/*`` endpoints are gone (ADR 0001).

Issue #74: every bearer token minted in this module is now bound to the
exact (method, path, body) of the request it authorizes, via the
shared ``tests.conftest.bearer_header`` helper. A single static header
cannot cover the many different requests exercised here, so the
``auth_headers`` fixture is a per-request minting factory rather than a
static header dict.
"""

from __future__ import annotations

import datetime as dt
import json
import logging
import os
import uuid
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable
    from pathlib import Path

    from flask import Flask
    from flask.testing import FlaskClient

    AuthHeaderFactory = Callable[[str, str, bytes], dict[str, str]]


from grocery_butler.api import api_v1
from grocery_butler.app import create_app
from grocery_butler.auth_middleware import SECRET_ENV_VAR
from grocery_butler.db import get_connection
from grocery_butler.models import (
    BrandMatchType,
    BrandPreference,
    BrandPreferenceType,
    PendingActionStatus,
)
from grocery_butler.pending_actions import PendingActionsStore
from grocery_butler.recipe_store import RecipeStore
from tests.conftest import bearer_header

TEST_SECRET = "test-shared-secret"
SECRET_ENV = {SECRET_ENV_VAR: TEST_SECRET}

DESTRUCTIVE_ENDPOINTS = [
    "/api/v1/brands/set",
    "/api/v1/preferences/set",
    "/api/v1/actions/confirm",
    "/api/v1/actions/deny",
]

#: Issue #74 AC#2: a view that never calls require_bearer() itself, used
#: to prove the api_v1 blueprint's before_request hook enforces auth
#: even when a route forgets the per-route check. Registered once, at
#: import time -- Flask blueprints only accept new routes before
#: they've been applied to an app via register_blueprint, and api_v1 is
#: a process-wide singleton reused by every create_app() call in this
#: suite, so this must be added before any test in the suite calls
#: create_app().
UNAUTHENTICATED_THROWAWAY_PATH = "/api/v1/_test_unauthenticated_view_issue_74"


@api_v1.get("/_test_unauthenticated_view_issue_74")
def _unauthenticated_throwaway_view() -> dict[str, bool]:
    """Return a fixed payload without ever calling require_bearer().

    Exists solely so ``TestAuthByDefault`` can prove that unauthenticated
    requests are still rejected -- by the blueprint's before_request
    hook, not by this view.
    """
    return {"ok": True}


# ---------------------------------------------------------------------------
# Fixtures and builders
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a temporary database path for test isolation."""
    return str(tmp_path / "test_api_destructive.db")


@pytest.fixture()
def app(db_path: str) -> Flask:
    """Create a Flask test app with a temporary database."""
    application = create_app(db_path=db_path)
    application.config["TESTING"] = True
    return application


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    """Return a Flask test client."""
    return app.test_client()


@pytest.fixture()
def pending_store(db_path: str) -> PendingActionsStore:
    """Return a PendingActionsStore bound to the test database."""
    return PendingActionsStore(db_path)


@pytest.fixture()
def recipe_store(db_path: str) -> RecipeStore:
    """Return a RecipeStore bound to the test database."""
    return RecipeStore(db_path)


@pytest.fixture()
def auth_headers() -> AuthHeaderFactory:
    """Return a factory that mints a bearer header for one exact request.

    A single static header cannot authorize every request exercised in
    this module -- each destructive-endpoint call has its own method,
    path, and body, and the request-bound token contract (issue #74)
    signs all three together. Callers invoke the returned factory once
    per request, e.g.
    ``auth_headers("POST", "/api/v1/brands/set", serialized_body)``.

    Returns:
        A callable ``(method, path, body=b"") -> {"Authorization": ...}``
        that mints tokens under the test shared secret.
    """
    return _auth_factory_for("rubotpaul")


def _post(
    client: FlaskClient,
    auth_headers: AuthHeaderFactory,
    path: str,
    body: Any,
) -> Any:
    """POST a JSON-serializable body with a token bound to the exact bytes sent.

    Serializes ``body`` exactly once so the identical bytes are both
    sent as the request payload and hashed into the bearer token's
    binding -- the two must match byte-for-byte for the server's HMAC
    verification to succeed (issue #74).

    Args:
        client: Flask test client.
        auth_headers: Factory fixture minting a bearer header for one
            exact (method, path, body) triple.
        path: Request path to POST to.
        body: JSON-serializable request payload.

    Returns:
        The Flask test client's response.
    """
    serialized = json.dumps(body).encode()
    headers = auth_headers("POST", path, serialized)
    return client.post(
        path, data=serialized, content_type="application/json", headers=headers
    )


def _auth_factory_for(caller_id: str) -> AuthHeaderFactory:
    """Return an auth-header factory minting request-bound tokens for one caller.

    Used to distinguish the staging caller (requester) from the
    confirming/denying caller (resolver) in W1 audit-trail tests
    (issue #75), while still honoring the request-bound token contract
    (issue #74): every minted header is valid only for the exact
    (method, path, body) it was created for.

    Args:
        caller_id: The caller identity to embed in every minted token.

    Returns:
        A callable ``(method, path, body=b"") -> {"Authorization": ...}``
        that mints bound tokens for ``caller_id`` under the test secret.
    """

    def _make(method: str, path: str, body: bytes = b"") -> dict[str, str]:
        """Mint a bearer header for this caller bound to method/path/body."""
        with patch.dict(os.environ, SECRET_ENV):
            return bearer_header(caller_id, method, path, body)

    return _make


def _get(
    client: FlaskClient,
    auth_headers: AuthHeaderFactory,
    path: str,
    query: str = "",
) -> Any:
    """GET a path with a token bound to the exact request.

    The server binds tokens to ``request.path`` (query string excluded),
    so the token is minted for ``path`` alone while the request URL may
    carry a query string (issue #74).

    Args:
        client: Flask test client.
        auth_headers: Factory fixture minting a bearer header for one
            exact (method, path, body) triple.
        path: Request path (no query string) the token is bound to.
        query: Optional query string (including ``?``) appended to the
            request URL only.

    Returns:
        The Flask test client's response.
    """
    headers = auth_headers("GET", path, b"")
    return client.get(f"{path}{query}", headers=headers)


def _brand_body() -> dict[str, Any]:
    """Return a valid /brands/set request body."""
    return BrandPreference(
        match_target="milk",
        match_type=BrandMatchType.INGREDIENT,
        brand="Clover",
        preference_type=BrandPreferenceType.PREFERRED,
        notes="organic when available",
    ).model_dump(mode="json")


def _stage_via_api(
    client: FlaskClient,
    auth_headers: AuthHeaderFactory,
    path: str,
    body: dict[str, Any],
) -> str:
    """Stage an action through the API and return its action_id."""
    response = _post(client, auth_headers, path, body)
    assert response.status_code == 200
    action_id = response.get_json()["action_id"]
    assert isinstance(action_id, str)
    return action_id


# ---------------------------------------------------------------------------
# W1 (issue #75): requester/resolver recorded from the real caller_id
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestRequesterAndResolverAtRoutes:
    """Every staged/resolved action records the real caller (W1, issue #75).

    Two distinct callers are minted so requester (the staging caller) and
    resolver (the confirming/denying caller) can be told apart in the
    audit trail -- neither should ever fall back to the old
    "rubotpaul"-only default.
    """

    def test_brands_set_records_requester_as_caller_id(
        self, client: FlaskClient, pending_store: PendingActionsStore
    ) -> None:
        """Staging a brand rule records the token's caller_id as requester."""
        response = _post(
            client, _auth_factory_for("bob"), "/api/v1/brands/set", _brand_body()
        )
        assert response.status_code == 200
        action = pending_store.get_pending_action(response.get_json()["action_id"])
        assert action is not None
        assert action.requester == "bob"

    def test_preferences_set_records_requester_as_caller_id(
        self, client: FlaskClient, pending_store: PendingActionsStore
    ) -> None:
        """Staging preferences records the token's caller_id as requester."""
        body = {"preferences": {"fulfillment": "pickup"}}
        response = _post(
            client, _auth_factory_for("carol"), "/api/v1/preferences/set", body
        )
        assert response.status_code == 200
        action = pending_store.get_pending_action(response.get_json()["action_id"])
        assert action is not None
        assert action.requester == "carol"

    def test_confirm_records_resolver_as_confirming_caller_id(
        self,
        client: FlaskClient,
        pending_store: PendingActionsStore,
    ) -> None:
        """Confirming stamps resolver with the CONFIRMING caller, not requester."""
        action_id = _stage_via_api(
            client, _auth_factory_for("alice"), "/api/v1/brands/set", _brand_body()
        )
        response = _post(
            client,
            _auth_factory_for("bob"),
            "/api/v1/actions/confirm",
            {"action_id": action_id},
        )
        assert response.status_code == 200
        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.requester == "alice"
        assert action.resolver == "bob"

    def test_deny_records_resolver_as_denying_caller_id(
        self,
        client: FlaskClient,
        pending_store: PendingActionsStore,
    ) -> None:
        """Denying stamps the resolver with the DENYING caller."""
        action_id = _stage_via_api(
            client, _auth_factory_for("alice"), "/api/v1/brands/set", _brand_body()
        )
        response = _post(
            client,
            _auth_factory_for("dave"),
            "/api/v1/actions/deny",
            {"action_id": action_id},
        )
        assert response.status_code == 200
        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.resolver == "dave"

    def test_system_expiry_via_confirm_leaves_resolver_none(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
    ) -> None:
        """Confirming an expired action resolves it as expired, no resolver.

        Expiry is a system-initiated resolution (the human never actually
        confirmed anything -- the TTL did), so the resolver must stay
        NULL even though a caller made the doomed confirm request.
        """
        action_id = str(uuid.uuid4())
        pending_store.insert_pending_action(
            action_id=action_id,
            kind="preferences_set",
            payload={"preferences": {"fulfillment": "pickup"}},
            expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
        )
        response = _post(
            client, auth_headers, "/api/v1/actions/confirm", {"action_id": action_id}
        )
        assert response.status_code == 410
        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.EXPIRED
        assert action.resolver is None


# ---------------------------------------------------------------------------
# W2 (issue #75): every transition logs INFO with action_id/kind, never
# payload contents
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestActionAuditLogging:
    """Every staged-action transition is logged at INFO (W2, issue #75).

    Uses a distinctive ingredient name as a payload-content marker: none
    of the captured log messages may ever contain it, proving the audit
    trail logs metadata (action_id, kind) and never the payload itself.
    """

    _MARKER_INGREDIENT = "unobtainium-marker-item"

    def test_staging_brands_logs_info_with_action_id_and_kind(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Staging a brand rule logs an INFO line, never the brand name."""
        with caplog.at_level(logging.INFO, logger="api"):
            response = _post(client, auth_headers, "/api/v1/brands/set", _brand_body())
        assert response.status_code == 200
        action_id = response.get_json()["action_id"]
        messages = [r.getMessage() for r in caplog.records if r.name == "api"]
        assert any(action_id in m and "brands_set" in m for m in messages)
        assert not any("Clover" in m for m in messages)

    def test_confirm_logs_info_with_action_id_and_kind(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Confirming a staged action logs a transition INFO line."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="api"):
            response = _post(
                client,
                auth_headers,
                "/api/v1/actions/confirm",
                {"action_id": action_id},
            )
        assert response.status_code == 200
        messages = [r.getMessage() for r in caplog.records if r.name == "api"]
        assert any(action_id in m and "brands_set" in m for m in messages)
        assert not any("Clover" in m for m in messages)

    def test_deny_logs_info_with_action_id_and_kind(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Denying a staged action logs a 'denied' transition line."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        caplog.clear()
        with caplog.at_level(logging.INFO, logger="api"):
            response = _post(
                client, auth_headers, "/api/v1/actions/deny", {"action_id": action_id}
            )
        assert response.status_code == 200
        messages = [r.getMessage() for r in caplog.records if r.name == "api"]
        assert any(action_id in m and "denied" in m.lower() for m in messages)
        assert not any("Clover" in m for m in messages)

    def test_expire_logs_info_with_action_id_and_kind(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Confirming an expired action logs an 'expired' transition line."""
        action_id = str(uuid.uuid4())
        pending_store.insert_pending_action(
            action_id=action_id,
            kind="preferences_set",
            payload={"preferences": {"fulfillment": "pickup"}},
            expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
        )
        with caplog.at_level(logging.INFO, logger="api"):
            response = _post(
                client,
                auth_headers,
                "/api/v1/actions/confirm",
                {"action_id": action_id},
            )
        assert response.status_code == 410
        messages = [r.getMessage() for r in caplog.records if r.name == "api"]
        assert any(action_id in m and "expired" in m.lower() for m in messages)


# ---------------------------------------------------------------------------
# Auth: every destructive endpoint rejects unauthenticated requests
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestAuthRequired:
    """All destructive endpoints must reject requests without a valid bearer."""

    @pytest.mark.parametrize("path", DESTRUCTIVE_ENDPOINTS)
    def test_missing_bearer_returns_401(self, client: FlaskClient, path: str) -> None:
        """Requests without an Authorization header get a JSON 401."""
        response = client.post(path, json={})
        assert response.status_code == 401
        assert "error" in response.get_json()

    @pytest.mark.parametrize("path", DESTRUCTIVE_ENDPOINTS)
    def test_garbage_bearer_returns_401(self, client: FlaskClient, path: str) -> None:
        """Requests with an invalid token get 401."""
        response = client.post(
            path, json={}, headers={"Authorization": "Bearer not.a.token"}
        )
        assert response.status_code == 401


# ---------------------------------------------------------------------------
# POST /brands/set and /preferences/set — same staging pattern
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestBrandsSetStaging:
    """/brands/set stages a validated brand rule without applying it."""

    def test_stages_pending_action(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
        recipe_store: RecipeStore,
    ) -> None:
        """A valid brand rule is staged, not written to brand preferences."""
        response = _post(client, auth_headers, "/api/v1/brands/set", _brand_body())
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "pending_confirmation"

        action = pending_store.get_pending_action(data["action_id"])
        assert action is not None
        assert action.kind == "brands_set"
        assert action.status is PendingActionStatus.PENDING
        assert action.payload["brand"] == "Clover"
        assert recipe_store.get_brand_preferences() == []

    def test_invalid_brand_payload_returns_400(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """A payload missing required brand fields is rejected."""
        response = _post(
            client, auth_headers, "/api/v1/brands/set", {"brand": "Clover"}
        )
        assert response.status_code == 400
        assert "invalid brand" in response.get_json()["error"]


@patch.dict(os.environ, SECRET_ENV)
class TestPreferencesSetStaging:
    """/preferences/set stages key/value settings without applying them."""

    def test_stages_pending_action(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
        recipe_store: RecipeStore,
    ) -> None:
        """Valid preferences are staged, not written to the store."""
        body = {"preferences": {"fulfillment": "pickup", "store_id": "1234"}}
        response = _post(client, auth_headers, "/api/v1/preferences/set", body)
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "pending_confirmation"

        action = pending_store.get_pending_action(data["action_id"])
        assert action is not None
        assert action.kind == "preferences_set"
        assert action.payload == body
        # Staging must not write: neither staged key reaches the store.
        stored = recipe_store.get_all_preferences()
        assert "fulfillment" not in stored
        assert "store_id" not in stored

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"preferences": {}},
            {"preferences": "pickup"},
            {"preferences": {"fulfillment": 7}},
        ],
    )
    def test_invalid_preferences_return_400(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        body: dict[str, Any],
    ) -> None:
        """Missing, empty, or non-string preference payloads are rejected."""
        response = _post(client, auth_headers, "/api/v1/preferences/set", body)
        assert response.status_code == 400


# ---------------------------------------------------------------------------
# POST /actions/confirm — executes exactly once
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestActionsConfirm:
    """/actions/confirm resolves staged actions with strict semantics."""

    def test_missing_action_id_returns_400(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """A body without action_id is rejected."""
        response = _post(client, auth_headers, "/api/v1/actions/confirm", {})
        assert response.status_code == 400

    def test_unknown_action_id_returns_404(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """An unknown action_id gets a JSON 404."""
        response = _post(
            client,
            auth_headers,
            "/api/v1/actions/confirm",
            {"action_id": str(uuid.uuid4())},
        )
        assert response.status_code == 404
        assert "error" in response.get_json()

    def test_expired_action_returns_410_and_marks_expired(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
    ) -> None:
        """Confirming past the TTL returns 410 and resolves the row as expired."""
        action_id = str(uuid.uuid4())
        pending_store.insert_pending_action(
            action_id=action_id,
            kind="preferences_set",
            payload={"preferences": {"fulfillment": "pickup"}},
            expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
        )
        response = _post(
            client,
            auth_headers,
            "/api/v1/actions/confirm",
            {"action_id": action_id},
        )
        assert response.status_code == 410
        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.EXPIRED
        assert action.resolved_at is not None

    def test_legacy_order_action_is_never_executed(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
    ) -> None:
        """A staged row of the retired order kind is refused, not executed.

        ADR 0001 removed the Safeway order executor. A row staged before
        the upgrade must come back as an unknown kind and stay pending
        (it expires on its own TTL) rather than reach any retailer.
        """
        action_id = str(uuid.uuid4())
        pending_store.insert_pending_action(
            action_id=action_id,
            kind="safeway_order_submit",
            payload={"total": "7.75"},
            expires_at=dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5),
        )
        response = _post(
            client,
            auth_headers,
            "/api/v1/actions/confirm",
            {"action_id": action_id},
        )
        assert response.status_code == 400
        assert "unknown action kind" in response.get_json()["error"]
        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.PENDING

    def test_double_confirm_returns_409(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
    ) -> None:
        """Confirming an already-approved action returns 409."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        first = _post(
            client,
            auth_headers,
            "/api/v1/actions/confirm",
            {"action_id": action_id},
        )
        assert first.status_code == 200
        second = _post(
            client,
            auth_headers,
            "/api/v1/actions/confirm",
            {"action_id": action_id},
        )
        assert second.status_code == 409
        assert "error" in second.get_json()

    def test_confirm_after_deny_returns_409(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
    ) -> None:
        """Confirming a denied action returns 409."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        denied = _post(
            client,
            auth_headers,
            "/api/v1/actions/deny",
            {"action_id": action_id},
        )
        assert denied.status_code == 200
        response = _post(
            client,
            auth_headers,
            "/api/v1/actions/confirm",
            {"action_id": action_id},
        )
        assert response.status_code == 409

    def test_confirm_brands_applies_rule(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        recipe_store: RecipeStore,
        pending_store: PendingActionsStore,
    ) -> None:
        """Confirming a brands_set action writes the brand rule."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        response = _post(
            client,
            auth_headers,
            "/api/v1/actions/confirm",
            {"action_id": action_id},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "approved"
        assert data["result"]["brand"] == "Clover"

        prefs = recipe_store.get_brand_preferences()
        assert len(prefs) == 1
        assert prefs[0].brand == "Clover"
        assert prefs[0].match_target == "milk"
        # The confirmation payload must carry the real inserted row id, not
        # the BrandPreference model's None default (regression: issue #63).
        assert prefs[0].id is not None
        assert data["result"]["id"] == prefs[0].id

        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.APPROVED

    def test_confirm_preferences_applies_settings(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        recipe_store: RecipeStore,
    ) -> None:
        """Confirming a preferences_set action writes every key/value pair."""
        body = {"preferences": {"fulfillment": "pickup", "store_id": "1234"}}
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/preferences/set", body
        )
        response = _post(
            client,
            auth_headers,
            "/api/v1/actions/confirm",
            {"action_id": action_id},
        )
        assert response.status_code == 200
        assert response.get_json()["result"]["updated"] == 2
        stored = recipe_store.get_all_preferences()
        assert stored["fulfillment"] == "pickup"
        assert stored["store_id"] == "1234"


# ---------------------------------------------------------------------------
# POST /actions/deny — audit-trail denial
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestActionsDeny:
    """/actions/deny resolves staged actions as denied."""

    def test_missing_action_id_returns_400(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """A body without action_id is rejected."""
        response = _post(client, auth_headers, "/api/v1/actions/deny", {})
        assert response.status_code == 400

    def test_unknown_action_id_returns_404(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """An unknown action_id gets 404."""
        response = _post(
            client,
            auth_headers,
            "/api/v1/actions/deny",
            {"action_id": str(uuid.uuid4())},
        )
        assert response.status_code == 404

    def test_denies_pending_action(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
        recipe_store: RecipeStore,
    ) -> None:
        """Denying marks the row denied and never applies the change."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        response = _post(
            client,
            auth_headers,
            "/api/v1/actions/deny",
            {"action_id": action_id},
        )
        assert response.status_code == 200
        data = response.get_json()
        assert data["status"] == "denied"
        assert data["action_id"] == action_id

        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.DENIED
        assert action.resolved_at is not None
        assert recipe_store.get_brand_preferences() == []

    def test_double_deny_returns_409(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
    ) -> None:
        """Denying an already-denied action returns 409."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        first = _post(
            client,
            auth_headers,
            "/api/v1/actions/deny",
            {"action_id": action_id},
        )
        assert first.status_code == 200
        second = _post(
            client,
            auth_headers,
            "/api/v1/actions/deny",
            {"action_id": action_id},
        )
        assert second.status_code == 409


# ---------------------------------------------------------------------------
# W5 (issue #75): preferences confirmation must be all-or-nothing
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestPreferencesAtomicity:
    """Confirming preferences_set must be all-or-nothing (W5, issue #75).

    ``_confirm_preferences_set`` must use a single connection / one
    commit for every staged key (``RecipeStore.set_preferences``)
    instead of the old per-key loop, so a mid-way failure leaves the
    store completely untouched rather than half-applied.
    """

    def test_mid_write_failure_leaves_no_keys_applied(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        recipe_store: RecipeStore,
    ) -> None:
        """A failure applying the second key rolls back the first too."""
        body = {"preferences": {"fulfillment": "pickup", "store_id": "1234"}}
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/preferences/set", body
        )

        class _FailSecondWrite:
            """Connection proxy that fails starting on its second execute()."""

            def __init__(self, real_conn: Any) -> None:
                self._real = real_conn
                self._calls = 0

            def execute(self, sql: str, params: Any = ()) -> Any:
                self._calls += 1
                if self._calls > 1:
                    msg = "simulated mid-transaction failure"
                    raise RuntimeError(msg)
                return self._real.execute(sql, params)

            def executescript(self, sql: str) -> None:
                self._real.executescript(sql)

            def commit(self) -> None:
                self._real.commit()

            def close(self) -> None:
                self._real.close()

        def _fake_get_connection(path: str) -> _FailSecondWrite:
            return _FailSecondWrite(get_connection(path))

        with (
            patch(
                "grocery_butler.recipe_store.get_connection",
                side_effect=_fake_get_connection,
            ),
            pytest.raises(RuntimeError, match="simulated mid-transaction failure"),
        ):
            _post(
                client,
                auth_headers,
                "/api/v1/actions/confirm",
                {"action_id": action_id},
            )

        stored = recipe_store.get_all_preferences()
        assert "fulfillment" not in stored
        assert "store_id" not in stored


# ---------------------------------------------------------------------------
# W4 (issue #75): GET /api/v1/actions and GET /api/v1/actions/<action_id>
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestListActionsEndpoint:
    """GET /api/v1/actions -- paginated, filterable audit-trail read (W4).

    Issue #75: read access to the pending_actions audit log so an
    operator (or RubotPaul) can see what's staged, resolved, or expired
    without querying the database directly.
    """

    def test_missing_bearer_returns_401(self, client: FlaskClient) -> None:
        """Unauthenticated requests are rejected."""
        response = client.get("/api/v1/actions")
        assert response.status_code == 401

    def test_returns_staged_actions_with_count(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """Staged actions show up in the list with a matching count."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        response = _get(client, auth_headers, "/api/v1/actions")
        assert response.status_code == 200
        data = response.get_json()
        assert "actions" in data
        assert "count" in data
        assert data["count"] == len(data["actions"])
        ids = [a["action_id"] for a in data["actions"]]
        assert action_id in ids

    def test_sweeps_expired_actions_before_listing(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
    ) -> None:
        """A past-due pending row shows up as expired, not pending."""
        action_id = str(uuid.uuid4())
        pending_store.insert_pending_action(
            action_id=action_id,
            kind="brands_set",
            payload={},
            expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
        )
        response = _get(client, auth_headers, "/api/v1/actions")
        assert response.status_code == 200
        matching = [
            a for a in response.get_json()["actions"] if a["action_id"] == action_id
        ]
        assert matching
        assert matching[0]["status"] == "expired"

    def test_default_limit_is_50(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """Omitting ?limit uses a default of 50."""
        with patch.object(
            PendingActionsStore, "list_pending_actions", return_value=[]
        ) as mock_list:
            response = _get(client, auth_headers, "/api/v1/actions")
        assert response.status_code == 200
        assert mock_list.call_args.kwargs.get("limit") == 50

    def test_limit_over_max_is_clamped_not_rejected(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """A ?limit above the hard cap is clamped to 200, not a 400."""
        with patch.object(
            PendingActionsStore, "list_pending_actions", return_value=[]
        ) as mock_list:
            response = _get(client, auth_headers, "/api/v1/actions", "?limit=99999")
        assert response.status_code == 200
        assert mock_list.call_args.kwargs.get("limit") == 200

    def test_status_filter_returns_only_matching_actions(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
    ) -> None:
        """?status= filters the returned actions by that status."""
        denied_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        _post(client, auth_headers, "/api/v1/actions/deny", {"action_id": denied_id})
        _stage_via_api(client, auth_headers, "/api/v1/brands/set", _brand_body())

        response = _get(client, auth_headers, "/api/v1/actions", "?status=denied")
        assert response.status_code == 200
        data = response.get_json()
        assert all(a["status"] == "denied" for a in data["actions"])
        assert denied_id in [a["action_id"] for a in data["actions"]]

    def test_unknown_status_value_returns_400(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """An unrecognized ?status value is rejected, not silently ignored."""
        response = _get(
            client, auth_headers, "/api/v1/actions", "?status=not_a_real_status"
        )
        assert response.status_code == 400


@patch.dict(os.environ, SECRET_ENV)
class TestGetActionEndpoint:
    """GET /api/v1/actions/<action_id> -- single-row audit-trail read (W4)."""

    def test_missing_bearer_returns_401(self, client: FlaskClient) -> None:
        """Unauthenticated requests are rejected."""
        response = client.get(f"/api/v1/actions/{uuid.uuid4()}")
        assert response.status_code == 401

    def test_returns_serialized_action(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """A known action_id returns its full serialized row."""
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        response = _get(client, auth_headers, f"/api/v1/actions/{action_id}")
        assert response.status_code == 200
        data = response.get_json()
        assert data["action_id"] == action_id
        assert data["kind"] == "brands_set"
        assert data["status"] == "pending"

    def test_unknown_action_id_returns_404(
        self, client: FlaskClient, auth_headers: AuthHeaderFactory
    ) -> None:
        """An unknown action_id gets a JSON 404."""
        response = _get(client, auth_headers, f"/api/v1/actions/{uuid.uuid4()}")
        assert response.status_code == 404
        assert "error" in response.get_json()


# ---------------------------------------------------------------------------
# W4 (issue #75): staging endpoints also sweep past-due pending rows first
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestStagingSweepsExpiredActions:
    """Staging a new action sweeps expired rows too, not just GET /actions.

    Issue #75 (W4) requires the lazy sweep to run wherever a caller
    touches the pending_actions table, not only on read: otherwise a
    past-due row can sit ``pending`` forever if the audit log is never
    listed. Each staging route must trigger the sweep as
    a side effect of inserting its own new row.
    """

    @pytest.mark.parametrize(
        ("path", "body"),
        [
            ("/api/v1/brands/set", _brand_body()),
            ("/api/v1/preferences/set", {"preferences": {"fulfillment": "pickup"}}),
        ],
        ids=["brands_set", "preferences_set"],
    )
    def test_staging_sweeps_unrelated_past_due_row(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
        path: str,
        body: dict[str, Any],
    ) -> None:
        """An unrelated past-due pending row flips to expired on staging."""
        stale_id = str(uuid.uuid4())
        pending_store.insert_pending_action(
            action_id=stale_id,
            kind="brands_set",
            payload={},
            expires_at=dt.datetime.now(dt.UTC) - dt.timedelta(seconds=1),
        )
        response = _post(client, auth_headers, path, body)
        assert response.status_code == 200

        stale = pending_store.get_pending_action(stale_id)
        assert stale is not None
        assert stale.status is PendingActionStatus.EXPIRED
        assert stale.resolved_at is not None
        assert stale.resolver is None


# ---------------------------------------------------------------------------
# Issue #74 AC#1: a token bound to one endpoint cannot be replayed on another
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestCrossEndpointTokenReplayRejected:
    """A token minted for a harmless read endpoint cannot confirm actions."""

    def test_inventory_token_replayed_against_confirm_returns_401(
        self,
        client: FlaskClient,
        auth_headers: AuthHeaderFactory,
        pending_store: PendingActionsStore,
    ) -> None:
        """AC#1: a GET /inventory token posted to /actions/confirm is rejected.

        Stages a real action with a correctly-bound token, then attempts
        to confirm it with a token minted for an unrelated read
        endpoint. The replay must be rejected, and the staged action
        must remain untouched -- no action may execute as a side effect
        of a mismatched token.
        """
        action_id = _stage_via_api(
            client, auth_headers, "/api/v1/brands/set", _brand_body()
        )
        stolen_headers = auth_headers("GET", "/api/v1/inventory", b"")

        response = client.post(
            "/api/v1/actions/confirm",
            data=json.dumps({"action_id": action_id}).encode(),
            content_type="application/json",
            headers=stolen_headers,
        )

        assert response.status_code == 401
        action = pending_store.get_pending_action(action_id)
        assert action is not None
        assert action.status is PendingActionStatus.PENDING


# ---------------------------------------------------------------------------
# Issue #74 AC#2: auth-by-default via the blueprint's before_request hook
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV)
class TestAuthByDefault:
    """The api_v1 blueprint enforces auth even without a per-route call.

    ``_unauthenticated_throwaway_view`` (module scope, above) never
    calls ``require_bearer()`` itself. If the blueprint's
    ``before_request`` hook is doing its job, unauthenticated requests
    to it must still 401, and correctly-bound requests must still 200.
    """

    def test_unauthenticated_request_to_unguarded_view_returns_401(
        self, db_path: str
    ) -> None:
        """An unauthenticated request to the unguarded view is rejected.

        Args:
            db_path: Temporary database path fixture for test isolation.
        """
        application = create_app(db_path=db_path)
        application.config["TESTING"] = True
        response = application.test_client().get(UNAUTHENTICATED_THROWAWAY_PATH)

        assert response.status_code == 401

    def test_bound_token_for_unguarded_view_returns_200(self, db_path: str) -> None:
        """A correctly bound token still reaches the unguarded view.

        Args:
            db_path: Temporary database path fixture for test isolation.
        """
        application = create_app(db_path=db_path)
        application.config["TESTING"] = True
        test_client = application.test_client()
        headers = bearer_header("rubotpaul", "GET", UNAUTHENTICATED_THROWAWAY_PATH)

        response = test_client.get(UNAUTHENTICATED_THROWAWAY_PATH, headers=headers)

        assert response.status_code == 200
        assert response.get_json() == {"ok": True}

    def test_caller_id_helper_without_auth_context_aborts_401(self, app: Flask) -> None:
        """_request_caller_id aborts 401 when the hook never bound a caller.

        Guards the defensive branch for a future auth-exempt endpoint
        mistakenly asking for a caller identity it never authenticated:
        outside the hook, no caller_id is bound to ``flask.g``, so the
        helper must refuse rather than fabricate an identity.

        Args:
            app: Flask test app fixture (provides a request context).
        """
        from werkzeug.exceptions import Unauthorized

        from grocery_butler.api import _request_caller_id

        with (
            app.test_request_context("/api/v1/actions"),
            pytest.raises(Unauthorized),
        ):
            _request_caller_id()
