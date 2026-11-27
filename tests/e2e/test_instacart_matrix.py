"""E2E: every way the Instacart handoff can be configured or can fail.

Each case drives a real handoff through API v2 (draft, approve, execute)
and then reads the stored attempt and the dashboard page. The provider is
replaced at the one place a socket would open, ``_send_https``, so the
registry, the transport, the adapter, the retry loop and the handoff
service all run as they do in production (issue #228).

What every case proves: a failure is a recorded attempt, never a link a
person might follow, never a claim that something was sent, and never a
leaked key.
"""

from __future__ import annotations

import json
import logging
import secrets
from typing import TYPE_CHECKING, Any

import pytest

from grocery_butler.api_v2 import RETRY_SLEEP_CONFIG
from grocery_butler.fulfillment.instacart import http_transport
from grocery_butler.fulfillment.instacart.http_transport import (
    DEVELOPMENT_HOST,
    PRODUCTS_LINK_PATH,
    TIMEOUT_SECONDS,
    ProviderResponse,
)
from grocery_butler.observability import AUDIT_LOGGER

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient

    from tests.e2e.conftest import V2

pytestmark = pytest.mark.e2e

LINK = "https://customers.dev.instacart.tools/store/shopping_lists/1"
GOOD = ProviderResponse(200, json.dumps({"products_link_url": LINK}).encode())
BUTTON = "Shop on Instacart"
EXPORT_LINK = "/handoff/export.txt"


class FakeProvider:
    """Stands where the HTTPS connection would open, and answers by script.

    Args:
        answers: What to return, or raise, for each call in turn. The last
            one repeats.
    """

    def __init__(self, *answers: ProviderResponse | BaseException) -> None:
        """Hold the script and start with no calls recorded."""
        self._answers = list(answers)
        self.calls: list[dict[str, Any]] = []

    def then(self, *answers: ProviderResponse | BaseException) -> None:
        """Replace the script, keeping the calls recorded so far."""
        self._answers = list(answers)

    def __call__(
        self, host: str, path: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> ProviderResponse:
        """Record the request and give the next scripted answer."""
        self.calls.append(
            {
                "host": host,
                "path": path,
                "body": body,
                "headers": headers,
                "timeout": timeout,
            }
        )
        answer = self._answers.pop(0) if len(self._answers) > 1 else self._answers[0]
        if isinstance(answer, BaseException):
            raise answer
        return answer


@pytest.fixture()
def api_key() -> str:
    """Return a key made for this run; its value means nothing."""
    return f"e2e{secrets.token_hex(12)}"


@pytest.fixture()
def provider(monkeypatch: pytest.MonkeyPatch) -> FakeProvider:
    """Replace the HTTPS sender with a scripted fake that returns a link."""
    fake = FakeProvider(GOOD)
    monkeypatch.setattr(http_transport, "_send_https", fake)
    return fake


@pytest.fixture()
def pauses(app: Flask) -> list[float]:
    """Record the waits the retry loop asks for, without waiting."""
    recorded: list[float] = []
    app.config[RETRY_SLEEP_CONFIG] = recorded.append
    return recorded


@pytest.fixture()
def development(monkeypatch: pytest.MonkeyPatch, api_key: str) -> None:
    """Configure the adapter the way the dogfood runs it."""
    monkeypatch.setenv("INSTACART_ENABLED", "true")
    monkeypatch.setenv("INSTACART_ENV", "development")
    monkeypatch.setenv("INSTACART_API_KEY", api_key)


@pytest.fixture()
def audit(caplog: pytest.LogCaptureFixture) -> pytest.LogCaptureFixture:
    """Capture the audit lines."""
    caplog.set_level(logging.INFO, logger=AUDIT_LOGGER)
    return caplog


def _approved_handoff(v2: V2) -> int:
    """Plan one item under an allergen rule, draft and approve a handoff.

    Returns:
        The approval id, ready to execute.
    """
    item_id = v2.ok("POST", "/items", {"name": "oat milk"})["id"]
    v2.ok("POST", "/constraints", {"kind": "allergen_exclusion", "value": "peanut"})
    v2.ok("POST", "/needs", {"item_id": item_id})
    version = v2.ok("POST", "/plans", {})["version"]
    draft = v2.ok("POST", f"/plans/{version}/handoffs", {"adapter": "instacart"})
    v2.ok(
        "POST",
        f"/handoffs/{draft['id']}/approve",
        {"payload_hash": draft["payload_hash"]},
    )
    approval_id: int = draft["id"]
    return approval_id


def _execute(v2: V2, approval_id: int, key: str = "matrix-execute") -> dict[str, Any]:
    """Execute an approved handoff and return the attempt as stored.

    A handoff that fails answers 502 with the attempt in the error's
    details. Either way the attempt is read back from the API, so the
    assertions are about what was recorded, not what one response said.
    """
    response = v2.post(f"/handoffs/{approval_id}/execute", key=key)
    payload = response.get_json()
    if response.status_code == 502:
        assert payload["error"]["code"] == "handoff_failed"
        attempt_id = payload["error"]["details"]["attempt"]["id"]
    else:
        assert response.status_code in {200, 201}, response.get_data(as_text=True)
        attempt_id = payload["id"]
    attempt: dict[str, Any] = v2.ok("GET", f"/handoffs/attempts/{attempt_id}")
    return attempt


def _assert_failed_safely(attempt: dict[str, Any], page: str, code: str) -> None:
    """Check a failed attempt offers no link and still offers the list."""
    assert attempt["status"] == "failed"
    assert attempt["error_code"] == code
    assert attempt["external_url"] is None
    assert BUTTON not in page
    assert EXPORT_LINK in page


@pytest.mark.parametrize(
    ("environment", "has_key"),
    [("development", False), ("staging", True)],
    ids=["development-without-a-key", "unknown-environment"],
)
def test_a_misconfigured_adapter_contacts_nothing(
    v2: V2,
    client: FlaskClient,
    provider: FakeProvider,
    monkeypatch: pytest.MonkeyPatch,
    api_key: str,
    environment: str,
    has_key: bool,
) -> None:
    """No key, or an environment we don't know: refuse, and never fall back."""
    monkeypatch.setenv("INSTACART_ENABLED", "true")
    monkeypatch.setenv("INSTACART_ENV", environment)
    if has_key:
        monkeypatch.setenv("INSTACART_API_KEY", api_key)
    else:
        monkeypatch.delenv("INSTACART_API_KEY", raising=False)

    attempt = _execute(v2, _approved_handoff(v2))

    _assert_failed_safely(
        attempt, client.get("/handoff").get_data(as_text=True), "not_configured"
    )
    assert attempt["retryable"] is False
    assert provider.calls == []


@pytest.mark.usefixtures("development")
def test_a_configured_adapter_sends_the_list_and_nothing_else(
    v2: V2, client: FlaskClient, provider: FakeProvider, api_key: str
) -> None:
    """One request to the development host, with the key only in its header."""
    attempt = _execute(v2, _approved_handoff(v2))

    (call,) = provider.calls
    sent = call["body"].decode()
    assert attempt["status"] == "created"
    assert attempt["external_url"] == LINK
    assert (call["host"], call["path"]) == (DEVELOPMENT_HOST, PRODUCTS_LINK_PATH)
    assert call["headers"]["Authorization"] == f"Bearer {api_key}"
    assert call["timeout"] == TIMEOUT_SECONDS
    assert "oat milk" in sent.lower()
    assert api_key not in sent
    assert "geoff" not in sent.lower()
    assert "peanut" not in sent.lower()
    assert BUTTON in client.get("/handoff").get_data(as_text=True)


@pytest.mark.usefixtures("development")
def test_a_rejected_key_fails_once_and_is_never_shown(
    v2: V2,
    client: FlaskClient,
    provider: FakeProvider,
    pauses: list[float],
    audit: pytest.LogCaptureFixture,
    api_key: str,
) -> None:
    """A 401 is not retried, and the key appears nowhere a person can read."""
    provider.then(ProviderResponse(401, b'{"error": "bad key"}'))

    attempt = _execute(v2, _approved_handoff(v2))
    page = client.get("/handoff").get_data(as_text=True)

    _assert_failed_safely(attempt, page, "unauthorized")
    assert attempt["retryable"] is False
    assert len(provider.calls) == 1
    assert pauses == []
    assert api_key not in json.dumps(attempt)
    assert api_key not in page
    assert api_key not in audit.text


@pytest.mark.usefixtures("development")
@pytest.mark.parametrize(
    "fault",
    [
        ProviderResponse(429, b""),
        ProviderResponse(503, b""),
        TimeoutError("timed out"),
    ],
    ids=["rate-limited", "unavailable", "timed-out"],
)
def test_a_passing_fault_is_retried_then_recorded_and_can_be_retried_again(
    v2: V2,
    client: FlaskClient,
    provider: FakeProvider,
    pauses: list[float],
    fault: ProviderResponse | BaseException,
) -> None:
    """Three bounded tries, a failed attempt, then success under the same approval.

    Once the tries run out, each of these is recorded the same way: the
    provider was unavailable. The pauses are recorded, not waited for, and
    a timeout is a raised error, so the test takes no real time.
    """
    provider.then(fault)
    approval_id = _approved_handoff(v2)

    failed = _execute(v2, approval_id)

    _assert_failed_safely(
        failed, client.get("/handoff").get_data(as_text=True), "provider_unavailable"
    )
    assert failed["retryable"] is True
    assert len(provider.calls) == 3
    assert pauses == [0.5, 1.0]

    provider.then(GOOD)
    retried = _execute(v2, approval_id)

    assert retried["id"] == failed["id"]
    assert retried["status"] == "created"
    assert retried["external_url"] == LINK
    assert len(provider.calls) == 4
    assert BUTTON in client.get("/handoff").get_data(as_text=True)


@pytest.mark.usefixtures("development")
@pytest.mark.parametrize(
    "body",
    [
        b"<html>gateway</html>",
        b"[]",
        json.dumps({"products_link_url": "http://insecure.invalid/list"}).encode(),
        json.dumps({"products_link_url": 7}).encode(),
    ],
    ids=["not-json", "not-an-object", "not-https", "not-a-string"],
)
def test_an_answer_that_is_not_a_link_stores_no_link(
    v2: V2, client: FlaskClient, provider: FakeProvider, body: bytes
) -> None:
    """A 200 we can't trust is a failure, with nothing to follow."""
    provider.then(ProviderResponse(200, body))

    attempt = _execute(v2, _approved_handoff(v2))

    _assert_failed_safely(
        attempt, client.get("/handoff").get_data(as_text=True), "invalid_response"
    )
    assert len(provider.calls) == 1


@pytest.mark.usefixtures("development")
def test_soft_preferences_are_filtered_or_said_once(
    v2: V2, provider: FakeProvider
) -> None:
    """Organic becomes a filter; what has no filter is one note (issue #215).

    The preferences are set for the whole household, so they land on both
    lines of a plan built from real needs. Instacart is sent ``ORGANIC`` on
    each line and no word about store brands or the brand to avoid. The
    attempt tells the member once, naming both items.
    """
    for name in ("oat milk", "butter"):
        item_id = v2.ok("POST", "/items", {"name": name})["id"]
        v2.ok("POST", "/needs", {"item_id": item_id})
    v2.ok("POST", "/soft-preferences", {"kind": "organic"})
    v2.ok("POST", "/soft-preferences", {"kind": "generic"})
    v2.ok("POST", "/soft-preferences", {"kind": "avoid_brand", "value": "Kerrygold"})
    plan = v2.ok("POST", "/plans", {})
    version = plan["version"]
    export = v2.ok("GET", f"/plans/{version}/export")
    draft = v2.ok("POST", f"/plans/{version}/handoffs", {"adapter": "instacart"})
    v2.ok(
        "POST",
        f"/handoffs/{draft['id']}/approve",
        {"payload_hash": draft["payload_hash"]},
    )

    attempt = _execute(v2, draft["id"])
    named = ", ".join(
        f"{line['display_name']} (prefer the store brand; avoid Kerrygold)"
        for line in plan["lines"]
    )

    (call,) = provider.calls
    sent = json.loads(call["body"])
    assert [line["filters"]["health_filters"] for line in sent["line_items"]] == [
        ["ORGANIC"],
        ["ORGANIC"],
    ]
    assert "Kerrygold" not in call["body"].decode()
    assert "store brand" not in call["body"].decode()
    assert attempt["status"] == "created"
    assert sorted(line["display_name"] for line in plan["lines"]) == [
        "Butter",
        "Oat Milk",
    ]
    assert attempt["needs_your_check"] == [
        f"These preferences can't be applied here, so check as you shop: {named}"
    ]
    assert (
        export["content"].count("[prefer organic, the store brand; avoid Kerrygold]")
        == 2
    )
