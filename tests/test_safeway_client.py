"""Tests for grocery_butler.safeway_client module."""

from __future__ import annotations

from datetime import UTC, datetime, timedelta
from unittest.mock import patch

import httpx
import pytest

from grocery_butler.safeway_client import (
    OKTA_CLIENT_ID,
    SafewayAPIError,
    SafewayAuthError,
    SafewayClient,
    TokenState,
    _extract_session_token,
    _parse_expires_in,
    _parse_token_from_redirect,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


def _make_authn_response(
    session_token: str = "test-session-token",
    status: str = "SUCCESS",
) -> httpx.Response:
    """Build a mock Okta authn response.

    Args:
        session_token: Session token to include.
        status: Okta response status.

    Returns:
        httpx.Response with JSON body.
    """
    return httpx.Response(
        200,
        json={"status": status, "sessionToken": session_token},
    )


def _make_authorize_redirect(
    access_token: str = "test-access-token",
    expires_in: int = 3600,
) -> httpx.Response:
    """Build a mock Okta authorize redirect response.

    Args:
        access_token: Token to include in fragment.
        expires_in: Token lifetime in seconds.

    Returns:
        httpx.Response with 302 redirect.
    """
    redirect_url = (
        f"https://www.safeway.com#"
        f"access_token={access_token}"
        f"&token_type=Bearer"
        f"&expires_in={expires_in}"
        f"&scope=openid+profile+email"
        f"&state=grocery-butler"
    )
    return httpx.Response(302, headers={"location": redirect_url})


def _make_api_response(
    data: dict | None = None,
    status_code: int = 200,
) -> httpx.Response:
    """Build a mock Nimbus API response.

    Args:
        data: JSON response body.
        status_code: HTTP status code.

    Returns:
        httpx.Response with JSON body.
    """
    return httpx.Response(status_code, json=data or {"ok": True})


class _MockTransport(httpx.BaseTransport):
    """Mock transport that returns pre-configured responses.

    Attributes:
        responses: List of responses to return in order.
        requests: List of requests received.
    """

    def __init__(self, responses: list[httpx.Response]) -> None:
        """Initialize with a list of responses.

        Args:
            responses: Ordered list of responses to return.
        """
        self.responses = list(responses)
        self.requests: list[httpx.Request] = []

    def handle_request(self, request: httpx.Request) -> httpx.Response:
        """Handle a request by returning the next response.

        Args:
            request: The incoming HTTP request.

        Returns:
            Next pre-configured response.
        """
        self.requests.append(request)
        if not self.responses:
            return httpx.Response(500, json={"error": "No more responses"})
        return self.responses.pop(0)


# ---------------------------------------------------------------------------
# TokenState tests
# ---------------------------------------------------------------------------


class TestTokenState:
    """Tests for TokenState dataclass."""

    def test_default_is_expired(self) -> None:
        """Test default TokenState is expired."""
        token = TokenState()
        assert token.is_expired is True
        assert token.access_token == ""

    def test_valid_token_not_expired(self) -> None:
        """Test token with future expiry is not expired."""
        token = TokenState(
            access_token="abc",
            expires_at=datetime.now(tz=UTC) + timedelta(hours=1),
        )
        assert token.is_expired is False

    def test_token_expired_within_buffer(self) -> None:
        """Test token within 5-minute refresh buffer is considered expired."""
        token = TokenState(
            access_token="abc",
            expires_at=datetime.now(tz=UTC) + timedelta(minutes=3),
        )
        assert token.is_expired is True

    def test_token_past_expiry(self) -> None:
        """Test token past expiry is expired."""
        token = TokenState(
            access_token="abc",
            expires_at=datetime.now(tz=UTC) - timedelta(hours=1),
        )
        assert token.is_expired is True


# ---------------------------------------------------------------------------
# Pure helper function tests
# ---------------------------------------------------------------------------


class TestExtractSessionToken:
    """Tests for _extract_session_token."""

    def test_valid_response(self) -> None:
        """Test extracting token from valid response."""
        data = {"status": "SUCCESS", "sessionToken": "abc123"}
        assert _extract_session_token(data) == "abc123"

    def test_missing_token_raises(self) -> None:
        """Test missing sessionToken raises SafewayAuthError."""
        with pytest.raises(SafewayAuthError, match="No session token"):
            _extract_session_token({"status": "LOCKED_OUT"})

    def test_empty_token_raises(self) -> None:
        """Test empty sessionToken raises SafewayAuthError."""
        with pytest.raises(SafewayAuthError, match="No session token"):
            _extract_session_token({"status": "SUCCESS", "sessionToken": ""})


class TestParseTokenFromRedirect:
    """Tests for _parse_token_from_redirect."""

    def test_valid_redirect(self) -> None:
        """Test parsing a valid redirect URL with token."""
        url = (
            "https://www.safeway.com#"
            "access_token=my-token"
            "&token_type=Bearer"
            "&expires_in=7200"
        )
        result = _parse_token_from_redirect(url)
        assert result.access_token == "my-token"
        assert result.is_expired is False

    def test_no_fragment_raises(self) -> None:
        """Test URL without fragment raises SafewayAuthError."""
        with pytest.raises(SafewayAuthError, match="No fragment"):
            _parse_token_from_redirect("https://example.com")

    def test_missing_access_token_raises(self) -> None:
        """Test fragment without access_token raises."""
        url = "https://example.com#token_type=Bearer&expires_in=3600"
        with pytest.raises(SafewayAuthError, match="No access_token"):
            _parse_token_from_redirect(url)

    def test_default_expires_in(self) -> None:
        """Test invalid expires_in defaults to 3600."""
        url = "https://example.com#access_token=tok&expires_in=invalid"
        result = _parse_token_from_redirect(url)
        assert result.access_token == "tok"
        assert result.is_expired is False


class TestParseExpiresIn:
    """Tests for _parse_expires_in."""

    def test_valid_integer(self) -> None:
        """Test parsing a valid integer string."""
        assert _parse_expires_in("7200") == 7200

    def test_invalid_defaults_to_3600(self) -> None:
        """Test invalid string defaults to 3600."""
        assert _parse_expires_in("not-a-number") == 3600


# ---------------------------------------------------------------------------
# SafewayClient tests
# ---------------------------------------------------------------------------


class TestSafewayClientInit:
    """Tests for SafewayClient initialization."""

    def test_basic_properties(self) -> None:
        """Test client stores configuration correctly."""
        transport = _MockTransport([])
        http = httpx.Client(transport=transport)
        client = SafewayClient("user", "pass", "1234", http_client=http)

        assert client.store_id == "1234"
        assert client.is_authenticated is False
        client.close()

    def test_not_authenticated_initially(self) -> None:
        """Test client is not authenticated before calling authenticate."""
        transport = _MockTransport([])
        http = httpx.Client(transport=transport)
        client = SafewayClient("user", "pass", "1234", http_client=http)
        assert client.is_authenticated is False
        client.close()


class TestSafewayClientAuth:
    """Tests for SafewayClient authentication flow."""

    @patch("grocery_butler.safeway_client.time.sleep")
    def test_authenticate_success(self, mock_sleep: object) -> None:
        """Test successful full authentication flow."""
        transport = _MockTransport(
            [
                _make_authn_response("session-123"),
                _make_authorize_redirect("access-456", 3600),
            ]
        )
        http = httpx.Client(transport=transport)
        client = SafewayClient("user", "pass", "1234", http_client=http)

        client.authenticate()

        assert client.is_authenticated is True
        assert len(transport.requests) == 2

        # Verify authn request
        authn_req = transport.requests[0]
        assert "/api/v1/authn" in str(authn_req.url)

        # Verify authorize request
        auth_req = transport.requests[1]
        assert "/v1/authorize" in str(auth_req.url)
        assert f"client_id={OKTA_CLIENT_ID}" in str(auth_req.url)

        client.close()

    @patch("grocery_butler.safeway_client.time.sleep")
    def test_authenticate_authn_failure(self, mock_sleep: object) -> None:
        """Test authentication fails when authn returns 401."""
        transport = _MockTransport(
            [
                httpx.Response(401, json={"error": "invalid credentials"}),
            ]
        )
        http = httpx.Client(transport=transport)
        client = SafewayClient("user", "bad", "1234", http_client=http)

        with pytest.raises(SafewayAuthError, match="Okta authn failed: 401"):
            client.authenticate()

        client.close()

    @patch("grocery_butler.safeway_client.time.sleep")
    def test_authenticate_no_redirect_fragment(
        self,
        mock_sleep: object,
    ) -> None:
        """Test auth fails when authorize redirect has no fragment."""
        transport = _MockTransport(
            [
                _make_authn_response("session-123"),
                httpx.Response(302, headers={"location": "https://example.com"}),
            ]
        )
        http = httpx.Client(transport=transport)
        client = SafewayClient("user", "pass", "1234", http_client=http)

        with pytest.raises(SafewayAuthError, match="No fragment"):
            client.authenticate()

        client.close()


class TestSafewayClientAPIRequests:
    """Tests for SafewayClient GET/POST API methods."""

    def _make_authenticated_client(
        self,
        api_responses: list[httpx.Response],
    ) -> tuple[SafewayClient, _MockTransport]:
        """Create a pre-authenticated client with mock responses.

        Args:
            api_responses: Responses for API calls (after auth).

        Returns:
            Tuple of (client, transport).
        """
        all_responses = [
            _make_authn_response(),
            _make_authorize_redirect(),
            *api_responses,
        ]
        transport = _MockTransport(all_responses)
        http = httpx.Client(transport=transport)
        client = SafewayClient("user", "pass", "1234", http_client=http)
        return client, transport

    @patch("grocery_butler.safeway_client.time.sleep")
    def test_get_success(self, mock_sleep: object) -> None:
        """Test successful GET request."""
        client, _transport = self._make_authenticated_client(
            [
                _make_api_response({"products": []}),
            ]
        )

        result = client.get("/api/v2/search", params={"q": "milk"})

        assert result == {"products": []}
        # 2 auth requests + 1 API request
        assert len(_transport.requests) == 3
        client.close()

    @patch("grocery_butler.safeway_client.time.sleep")
    def test_post_success(self, mock_sleep: object) -> None:
        """Test successful POST request."""
        client, _transport = self._make_authenticated_client(
            [
                _make_api_response({"cartId": "abc"}),
            ]
        )

        result = client.post("/api/v2/cart", json_data={"items": []})

        assert result == {"cartId": "abc"}
        assert len(_transport.requests) == 3
        client.close()

    @patch("grocery_butler.safeway_client.time.sleep")
    def test_get_retries_on_401(self, mock_sleep: object) -> None:
        """Test GET retries with re-auth on 401."""
        client, _transport = self._make_authenticated_client(
            [
                httpx.Response(401, json={"error": "expired"}),
                # Re-auth responses
                _make_authn_response("new-session"),
                _make_authorize_redirect("new-token"),
                # Retry succeeds
                _make_api_response({"ok": True}),
            ]
        )

        result = client.get("/api/v2/test")

        assert result == {"ok": True}
        client.close()

    @patch("grocery_butler.safeway_client.time.sleep")
    def test_get_raises_on_non_401_error(self, mock_sleep: object) -> None:
        """Test GET raises SafewayAPIError on non-401 errors."""
        client, _transport = self._make_authenticated_client(
            [
                httpx.Response(500, json={"error": "server error"}),
            ]
        )

        with pytest.raises(SafewayAPIError, match="500"):
            client.get("/api/v2/test")

        client.close()

    @patch("grocery_butler.safeway_client.time.sleep")
    def test_post_retries_on_401(self, mock_sleep: object) -> None:
        """Test POST retries with re-auth on 401."""
        client, _transport = self._make_authenticated_client(
            [
                httpx.Response(401, json={"error": "expired"}),
                _make_authn_response("new-session"),
                _make_authorize_redirect("new-token"),
                _make_api_response({"cartId": "abc"}),
            ]
        )

        result = client.post("/api/v2/cart", json_data={"items": []})

        assert result == {"cartId": "abc"}
        client.close()

    @patch("grocery_butler.safeway_client.time.sleep")
    def test_get_raises_after_retry_fails(self, mock_sleep: object) -> None:
        """Test GET raises after re-auth + retry also returns 401."""
        client, _transport = self._make_authenticated_client(
            [
                httpx.Response(401, json={"error": "expired"}),
                _make_authn_response("new-session"),
                _make_authorize_redirect("new-token"),
                httpx.Response(401, json={"error": "still expired"}),
            ]
        )

        with pytest.raises(SafewayAPIError, match="failed after re-auth"):
            client.get("/api/v2/test")

        client.close()


class TestSafewayClientAutoAuth:
    """Tests for automatic authentication on API calls."""

    @patch("grocery_butler.safeway_client.time.sleep")
    def test_get_auto_authenticates(self, mock_sleep: object) -> None:
        """Test GET auto-authenticates when not yet authenticated."""
        transport = _MockTransport(
            [
                _make_authn_response(),
                _make_authorize_redirect(),
                _make_api_response({"data": "value"}),
            ]
        )
        http = httpx.Client(transport=transport)
        client = SafewayClient("user", "pass", "1234", http_client=http)

        # Call get() without calling authenticate() first
        result = client.get("/api/v2/test")

        assert result == {"data": "value"}
        assert len(transport.requests) == 3
        client.close()


class TestSafewayClientRateLimiting:
    """Tests for rate limiting behavior."""

    def test_rate_limit_calls_sleep(self) -> None:
        """Test rate limiter sleeps between rapid requests."""
        transport = _MockTransport(
            [
                _make_authn_response(),
                _make_authorize_redirect(),
            ]
        )
        http = httpx.Client(transport=transport)
        client = SafewayClient("user", "pass", "1234", http_client=http)

        with patch("grocery_butler.safeway_client.time.sleep") as mock_sleep:
            with patch(
                "grocery_butler.safeway_client.time.monotonic",
                side_effect=[0.0, 0.0, 0.1, 0.1],
            ):
                client.authenticate()

            # First call: no sleep needed (no previous request)
            # Second call: 0.1s elapsed < 0.5s interval, should sleep
            assert mock_sleep.call_count >= 1

        client.close()


class TestSafewayClientClose:
    """Tests for client close behavior."""

    def test_close_owned_client(self) -> None:
        """Test close closes a client we created."""
        client = SafewayClient("user", "pass", "1234")
        # Should not raise
        client.close()

    def test_close_external_client_noop(self) -> None:
        """Test close does not close an externally provided client."""
        transport = _MockTransport([])
        http = httpx.Client(transport=transport)
        client = SafewayClient("user", "pass", "1234", http_client=http)

        client.close()
        # External client should still be usable
        assert not http.is_closed
        http.close()


# ---------------------------------------------------------------------------
# Config integration tests
# ---------------------------------------------------------------------------


class TestConfigSafewayFields:
    """Tests for Safeway config fields."""

    def test_config_has_safeway_fields(self) -> None:
        """Test Config dataclass includes Safeway fields."""
        from grocery_butler.config import Config

        cfg = Config(
            anthropic_api_key="test",
            safeway_username="user@example.com",
            safeway_password="secret",
            safeway_store_id="1234",
        )
        assert cfg.safeway_username == "user@example.com"
        assert cfg.safeway_password == "secret"
        assert cfg.safeway_store_id == "1234"

    def test_config_safeway_defaults_empty(self) -> None:
        """Test Safeway fields default to empty strings."""
        from grocery_butler.config import Config

        cfg = Config(anthropic_api_key="test")
        assert cfg.safeway_username == ""
        assert cfg.safeway_password == ""
        assert cfg.safeway_store_id == ""

    def test_load_config_reads_safeway_env(self) -> None:
        """Test load_config reads Safeway env vars."""
        from unittest.mock import patch as mock_patch

        from grocery_butler.config import load_config

        env = {
            "ANTHROPIC_API_KEY": "test-key",
            "SAFEWAY_USERNAME": "user@example.com",
            "SAFEWAY_PASSWORD": "pass123",
            "SAFEWAY_STORE_ID": "5678",
        }
        with (
            mock_patch(
                "grocery_butler.config.load_dotenv",
            ),
            mock_patch.dict("os.environ", env, clear=True),
        ):
            cfg = load_config()

        assert cfg.safeway_username == "user@example.com"
        assert cfg.safeway_password == "pass123"
        assert cfg.safeway_store_id == "5678"
