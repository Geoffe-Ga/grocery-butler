"""Tests for the transport that calls Instacart (#148).

Every test passes a fake sender. The one test that doesn't proves the
suite-wide guard stops a real connection.
"""

from __future__ import annotations

import http.client
import json
import logging
import secrets
from typing import Any

import pytest
from pydantic import ValidationError

from grocery_butler.fulfillment.instacart.adapter import InstacartAdapter
from grocery_butler.fulfillment.instacart.http_transport import (
    DEVELOPMENT_HOST,
    PRODUCTION_HOST,
    PRODUCTS_LINK_PATH,
    TIMEOUT_SECONDS,
    HttpTransport,
    ProviderResponse,
)
from grocery_butler.fulfillment.instacart.models import LineItem, ProductsLinkRequest
from grocery_butler.fulfillment.instacart.transport import (
    NotConfiguredTransport,
    StubTransport,
    TransportError,
)
from grocery_butler.fulfillment.registry import (
    INSTACART_ENABLED_ENV,
    INSTACART_ENV_VARIABLE,
    INSTACART_KEY_VARIABLE,
    default_registry,
    instacart_transport,
)
from grocery_butler.retries import is_transient
from tests.conftest import RealProviderContactedError

LINK = "https://example.invalid/l/abc"
REQUEST = ProductsLinkRequest(
    title="List", expires_in=7, line_items=[LineItem(name="carrots")]
)


@pytest.fixture()
def key() -> str:
    """Return an API key nobody has seen before."""
    return secrets.token_hex(24)


class _Sender:
    """A fake HTTPS sender: records the call, plays back an outcome."""

    def __init__(self, outcome: ProviderResponse | Exception) -> None:
        """Remember what to return or raise."""
        self._outcome = outcome
        self.calls: list[dict[str, Any]] = []

    def __call__(
        self, host: str, path: str, body: bytes, headers: dict[str, str], timeout: float
    ) -> ProviderResponse:
        """Record the request and play the outcome."""
        self.calls.append(
            {
                "host": host,
                "path": path,
                "body": body,
                "headers": headers,
                "timeout": timeout,
            }
        )
        if isinstance(self._outcome, Exception):
            raise self._outcome
        return self._outcome


def _ok(body: Any) -> ProviderResponse:
    """Build a 200 response carrying a JSON body."""
    return ProviderResponse(200, json.dumps(body).encode())


class TestRequest:
    """What is sent, and where."""

    def test_one_documented_post(self, key: str) -> None:
        """One POST to the documented path, with the key as a bearer token."""
        sender = _Sender(_ok({"products_link_url": LINK}))

        response = HttpTransport(
            key, DEVELOPMENT_HOST, send=sender
        ).create_products_link(REQUEST)

        call = sender.calls[0]
        assert response.products_link_url == LINK
        assert len(sender.calls) == 1
        assert call["host"] == "connect.dev.instacart.tools"
        assert call["path"] == PRODUCTS_LINK_PATH == "/idp/v1/products/products_link"
        assert call["headers"]["Authorization"] == f"Bearer {key}"
        assert call["headers"]["Content-Type"] == "application/json"
        assert call["timeout"] == TIMEOUT_SECONDS
        assert json.loads(call["body"]) == REQUEST.body()

    def test_production_uses_the_production_host(self, key: str) -> None:
        """The host is the one configured, never another."""
        sender = _Sender(_ok({"products_link_url": LINK}))

        HttpTransport(key, PRODUCTION_HOST, send=sender).create_products_link(REQUEST)

        assert sender.calls[0]["host"] == "connect.instacart.com"

    @pytest.mark.parametrize(
        "host", ["evil.example", "connect.instacart.com.evil.example", "", "localhost"]
    )
    def test_any_other_host_is_refused(self, key: str, host: str) -> None:
        """The key can only ever be sent to Instacart's two hosts."""
        with pytest.raises(ValueError, match="development or production host"):
            HttpTransport(key, host)

    def test_a_key_is_required(self) -> None:
        """There is no transport without a key."""
        with pytest.raises(ValueError, match="API key is required"):
            HttpTransport("", DEVELOPMENT_HOST)


class TestResponses:
    """How the provider's answers are read."""

    @pytest.mark.parametrize(
        ("status", "code", "transient"),
        [
            (400, "bad_request", False),
            (401, "unauthorized", False),
            (403, "unauthorized", False),
            (404, "not_found", False),
            (418, "unexpected_status", False),
            (429, "rate_limited", True),
            (500, "unavailable", True),
            (503, "unavailable", True),
        ],
    )
    def test_error_statuses_become_stable_codes(
        self, key: str, status: int, code: str, transient: bool
    ) -> None:
        """Each status maps to a code; only 429 and 5xx are worth retrying."""
        sender = _Sender(ProviderResponse(status, b'{"error": "provider detail"}'))

        with pytest.raises(TransportError) as raised:
            HttpTransport(key, DEVELOPMENT_HOST, send=sender).create_products_link(
                REQUEST
            )

        assert raised.value.code == code
        assert raised.value.status_code == status
        assert is_transient(raised.value) is transient
        assert "provider detail" not in str(raised.value)

    @pytest.mark.parametrize(
        "failure",
        [
            TimeoutError("timed out"),
            ConnectionRefusedError("refused"),
            OSError("network down"),
            http.client.RemoteDisconnected("gone"),
        ],
    )
    def test_an_unreachable_provider_is_worth_retrying(
        self, key: str, failure: Exception
    ) -> None:
        """A timeout or dropped connection is a passing fault with no cause chain."""
        transport = HttpTransport(key, DEVELOPMENT_HOST, send=_Sender(failure))

        with pytest.raises(TransportError) as raised:
            transport.create_products_link(REQUEST)

        assert raised.value.code == "unreachable"
        assert raised.value.status_code is None
        assert is_transient(raised.value) is True
        assert raised.value.__cause__ is None

    def test_a_body_that_is_not_json(self, key: str) -> None:
        """A 200 with an HTML error page is an invalid response."""
        sender = _Sender(ProviderResponse(200, b"<html>oops</html>"))

        with pytest.raises(TransportError) as raised:
            HttpTransport(key, DEVELOPMENT_HOST, send=sender).create_products_link(
                REQUEST
            )

        assert raised.value.code == "invalid_response"
        assert is_transient(raised.value) is False

    @pytest.mark.parametrize(
        "body",
        [
            {},
            {"products_link_url": ""},
            {"products_link_url": None},
            {"products_link_url": 7},
            {"products_link_url": "http://insecure.example/l"},
            ["not", "an", "object"],
        ],
    )
    def test_a_200_without_an_https_link_is_never_a_link(
        self, key: str, body: Any
    ) -> None:
        """No link in the answer means no response object, and so no handoff."""
        transport = HttpTransport(key, DEVELOPMENT_HOST, send=_Sender(_ok(body)))

        with pytest.raises(ValidationError):
            transport.create_products_link(REQUEST)


class TestTheKeyStaysSecret:
    """The key appears in the Authorization header and nowhere else."""

    def test_repr_hides_the_key(self, key: str) -> None:
        """Printing the transport shows the host and a marker."""
        transport = HttpTransport(key, DEVELOPMENT_HOST)

        assert key not in repr(transport)
        assert key not in str(transport)
        assert repr(transport) == (
            "HttpTransport(host='connect.dev.instacart.tools', api_key=[redacted])"
        )

    @pytest.mark.parametrize(
        "outcome",
        [
            ProviderResponse(401, b"{}"),
            ProviderResponse(500, b"{}"),
            ProviderResponse(200, b"not json"),
            TimeoutError("timed out"),
        ],
    )
    def test_no_error_or_log_line_carries_the_key(
        self,
        key: str,
        outcome: ProviderResponse | Exception,
        caplog: pytest.LogCaptureFixture,
    ) -> None:
        """Whatever fails, the key is in no message, attribute or log record."""
        caplog.set_level(logging.DEBUG)
        transport = HttpTransport(key, DEVELOPMENT_HOST, send=_Sender(outcome))

        with pytest.raises(TransportError) as raised:
            transport.create_products_link(REQUEST)

        error = raised.value
        assert key not in str(error)
        assert key not in repr(error)
        assert key not in repr(vars(error))
        assert key not in repr(error.__context__)
        assert key not in caplog.text


class TestConfiguration:
    """INSTACART_ENV and INSTACART_API_KEY decide the transport."""

    @pytest.mark.parametrize(
        "env", [{}, {INSTACART_ENV_VARIABLE: "stub"}, {INSTACART_ENV_VARIABLE: " "}]
    )
    def test_the_default_is_the_stub(self, env: dict[str, str]) -> None:
        """Unset or stub means a fake link and no network."""
        assert isinstance(instacart_transport(env), StubTransport)

    @pytest.mark.parametrize(
        ("name", "host"),
        [
            ("development", DEVELOPMENT_HOST),
            ("Development", DEVELOPMENT_HOST),
            ("production", PRODUCTION_HOST),
        ],
    )
    def test_a_real_environment_with_a_key(
        self, key: str, name: str, host: str
    ) -> None:
        """With a key, the named environment's host is used."""
        transport = instacart_transport(
            {INSTACART_ENV_VARIABLE: name, INSTACART_KEY_VARIABLE: key}
        )

        assert isinstance(transport, HttpTransport)
        assert host in repr(transport)

    @pytest.mark.parametrize("name", ["development", "production"])
    def test_a_real_environment_without_a_key_is_not_configured(
        self, name: str
    ) -> None:
        """No key means every call is refused, and nothing is sent."""
        transport = instacart_transport({INSTACART_ENV_VARIABLE: name})

        assert isinstance(transport, NotConfiguredTransport)
        with pytest.raises(TransportError) as raised:
            transport.create_products_link(REQUEST)
        assert raised.value.code == "not_configured"
        assert is_transient(raised.value) is False

    def test_an_unknown_environment_never_falls_back_to_a_real_host(
        self, key: str
    ) -> None:
        """A typo fails closed instead of picking a host."""
        transport = instacart_transport(
            {INSTACART_ENV_VARIABLE: "prod", INSTACART_KEY_VARIABLE: key}
        )

        assert isinstance(transport, NotConfiguredTransport)

    def test_the_registry_wires_the_configured_transport(self, key: str) -> None:
        """Enabled with a key, the adapter is registered and described."""
        registry = default_registry(
            {
                INSTACART_ENABLED_ENV: "true",
                INSTACART_ENV_VARIABLE: "development",
                INSTACART_KEY_VARIABLE: key,
            }
        )

        assert registry.names() == ["export", "instacart"]
        assert isinstance(registry.get("instacart"), InstacartAdapter)
        assert key not in repr(registry.get("instacart").describe())


class TestNoRealCalls:
    """A normal test run never calls Instacart."""

    def test_a_transport_without_a_fake_sender_is_stopped(self, key: str) -> None:
        """The suite-wide guard fails a test that would open a connection."""
        transport = HttpTransport(key, DEVELOPMENT_HOST)

        with pytest.raises(RealProviderContactedError, match="never call Instacart"):
            transport.create_products_link(REQUEST)
