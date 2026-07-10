"""Tests for the vendored RubotPaul HMAC bearer auth middleware."""

from __future__ import annotations

import asyncio
import os
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest
from flask import Flask

from grocery_butler.auth_middleware import (
    MAX_TOKEN_AGE_SECONDS,
    MAX_TOKEN_FUTURE_SKEW_SECONDS,
    SECRET_ENV_VAR,
    AuthError,
    _verify_token,
    aiohttp_auth_middleware,
    mint_token,
    require_bearer,
)

if TYPE_CHECKING:
    from types import ModuleType

    from flask.testing import FlaskClient

TEST_SECRET = "test-shared-secret"
SECRET_ENV = {SECRET_ENV_VAR: TEST_SECRET}
NOW = 1_700_000_000.0

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app() -> Flask:
    """Create a minimal Flask app with one bearer-protected route."""
    application = Flask(__name__)
    application.config["TESTING"] = True

    @application.get("/protected")
    def protected() -> dict[str, str]:
        caller_id = require_bearer()
        return {"caller_id": caller_id}

    return application


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    """Return a Flask test client for the protected app."""
    return app.test_client()


def _fake_aiohttp_request(headers: dict[str, str]) -> Any:
    """Build a minimal stand-in for an aiohttp request."""

    class FakeRequest:
        def __init__(self) -> None:
            self.headers = headers
            self.state: dict[str, str] = {}

        def __setitem__(self, key: str, value: str) -> None:
            self.state[key] = value

    return FakeRequest()


@pytest.fixture()
def fake_aiohttp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a stub aiohttp module so the middleware can import it."""

    def json_response(payload: dict[str, str], status: int = 200) -> SimpleNamespace:
        return SimpleNamespace(payload=payload, status=status)

    web = SimpleNamespace(json_response=json_response)
    monkeypatch.setitem(
        sys.modules, "aiohttp", cast("ModuleType", SimpleNamespace(web=web))
    )


# ---------------------------------------------------------------------------
# AuthError
# ---------------------------------------------------------------------------


class TestAuthError:
    """Tests for the AuthError exception type."""

    def test_carries_reason_and_default_status(self) -> None:
        """AuthError stores the reason and defaults to HTTP 401."""
        exc = AuthError("bad signature")
        assert exc.reason == "bad signature"
        assert exc.status == 401
        assert str(exc) == "bad signature"

    def test_custom_status(self) -> None:
        """AuthError accepts an explicit status code."""
        exc = AuthError("forbidden", status=403)
        assert exc.status == 403


# ---------------------------------------------------------------------------
# mint_token / _verify_token round-trip
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV, clear=True)
class TestTokenRoundTrip:
    """Tests for minting and verifying tokens."""

    def test_valid_token_returns_caller_id(self) -> None:
        """A freshly minted token verifies and yields the caller_id."""
        token = mint_token("rubotpaul", now=NOW)
        assert _verify_token(token, now=NOW) == "rubotpaul"

    def test_token_has_three_dot_separated_parts(self) -> None:
        """Minted tokens follow <caller_id>.<timestamp>.<hmac_hex>."""
        token = mint_token("rubotpaul", now=NOW)
        caller_id, ts, sig = token.split(".")
        assert caller_id == "rubotpaul"
        assert ts == str(int(NOW))
        assert len(sig) == 64  # SHA-256 hex digest

    def test_token_at_max_age_is_accepted(self) -> None:
        """A token exactly MAX_TOKEN_AGE_SECONDS old is still valid."""
        token = mint_token("rubotpaul", now=NOW)
        assert _verify_token(token, now=NOW + MAX_TOKEN_AGE_SECONDS) == "rubotpaul"

    def test_expired_token_rejected(self) -> None:
        """A token older than MAX_TOKEN_AGE_SECONDS is rejected."""
        token = mint_token("rubotpaul", now=NOW)
        with pytest.raises(AuthError, match="token expired") as excinfo:
            _verify_token(token, now=NOW + MAX_TOKEN_AGE_SECONDS + 1)
        assert excinfo.value.status == 401

    def test_token_at_max_future_skew_is_accepted(self) -> None:
        """A token exactly MAX_TOKEN_FUTURE_SKEW_SECONDS ahead is tolerated."""
        token = mint_token("rubotpaul", now=NOW + MAX_TOKEN_FUTURE_SKEW_SECONDS)
        assert _verify_token(token, now=NOW) == "rubotpaul"

    def test_token_from_future_rejected(self) -> None:
        """A token more than MAX_TOKEN_FUTURE_SKEW_SECONDS ahead is rejected."""
        token = mint_token("rubotpaul", now=NOW + MAX_TOKEN_FUTURE_SKEW_SECONDS + 1)
        with pytest.raises(AuthError, match="token from future"):
            _verify_token(token, now=NOW)

    def test_defaults_to_current_time(self) -> None:
        """Without an explicit now, mint and verify use the wall clock."""
        token = mint_token("rubotpaul")
        assert _verify_token(token) == "rubotpaul"

    @pytest.mark.parametrize("token", ["", "no-dots", "one.dot", "a.b.c.d"])
    def test_malformed_token_rejected(self, token: str) -> None:
        """Tokens without exactly three dot-separated parts are rejected."""
        with pytest.raises(AuthError, match="malformed token"):
            _verify_token(token, now=NOW)

    def test_malformed_timestamp_rejected(self) -> None:
        """A non-integer timestamp is rejected."""
        with pytest.raises(AuthError, match="malformed timestamp"):
            _verify_token("rubotpaul.not-a-number.deadbeef", now=NOW)

    def test_bad_signature_rejected(self) -> None:
        """A tampered signature is rejected."""
        token = mint_token("rubotpaul", now=NOW)
        caller_id, ts, sig = token.split(".")
        tampered_sig = ("0" if sig[0] != "0" else "1") + sig[1:]
        with pytest.raises(AuthError, match="bad signature"):
            _verify_token(f"{caller_id}.{ts}.{tampered_sig}", now=NOW)

    def test_tampered_caller_id_rejected(self) -> None:
        """Changing the caller_id invalidates the signature."""
        token = mint_token("rubotpaul", now=NOW)
        _, ts, sig = token.split(".")
        with pytest.raises(AuthError, match="bad signature"):
            _verify_token(f"impostor.{ts}.{sig}", now=NOW)

    def test_token_minted_with_other_secret_rejected(self) -> None:
        """A token signed with a different secret is rejected."""
        with patch.dict(os.environ, {SECRET_ENV_VAR: "other-secret"}):
            token = mint_token("rubotpaul", now=NOW)
        with pytest.raises(AuthError, match="bad signature"):
            _verify_token(token, now=NOW)


# ---------------------------------------------------------------------------
# Missing shared secret
# ---------------------------------------------------------------------------


class TestMissingSecret:
    """Tests for missing RUBOTPAUL_SHARED_SECRET."""

    @patch.dict(os.environ, {}, clear=True)
    def test_mint_token_raises_when_secret_unset(self) -> None:
        """mint_token fails loud when the shared secret is not set."""
        with pytest.raises(RuntimeError, match=SECRET_ENV_VAR):
            mint_token("rubotpaul", now=NOW)

    @patch.dict(os.environ, {SECRET_ENV_VAR: ""}, clear=True)
    def test_mint_token_raises_when_secret_empty(self) -> None:
        """mint_token treats an empty shared secret as unset."""
        with pytest.raises(RuntimeError, match=SECRET_ENV_VAR):
            mint_token("rubotpaul", now=NOW)

    def test_verify_token_raises_when_secret_unset(self) -> None:
        """_verify_token fails loud when the shared secret is not set."""
        with patch.dict(os.environ, SECRET_ENV, clear=True):
            token = mint_token("rubotpaul", now=NOW)
        with (
            patch.dict(os.environ, {}, clear=True),
            pytest.raises(RuntimeError, match=SECRET_ENV_VAR),
        ):
            _verify_token(token, now=NOW)


# ---------------------------------------------------------------------------
# Flask integration (require_bearer)
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV, clear=True)
class TestRequireBearerFlask:
    """Tests for require_bearer via a minimal Flask app."""

    def test_missing_header_returns_401(self, client: FlaskClient) -> None:
        """Requests without an Authorization header get 401."""
        response = client.get("/protected")
        assert response.status_code == 401
        assert b"missing bearer token" in response.data

    def test_non_bearer_scheme_returns_401(self, client: FlaskClient) -> None:
        """Non-Bearer Authorization schemes get 401."""
        response = client.get(
            "/protected", headers={"Authorization": "Basic dXNlcjpwYXNz"}
        )
        assert response.status_code == 401
        assert b"missing bearer token" in response.data

    def test_bad_token_returns_401(self, client: FlaskClient) -> None:
        """A garbage bearer token gets 401 with the failure reason."""
        response = client.get(
            "/protected", headers={"Authorization": "Bearer not-a-real-token"}
        )
        assert response.status_code == 401
        assert b"malformed token" in response.data

    def test_bad_signature_returns_401(self, client: FlaskClient) -> None:
        """A well-formed token with a bad signature gets 401."""
        token = mint_token("rubotpaul")
        caller_id, ts, sig = token.split(".")
        tampered = f"{caller_id}.{ts}.{'0' * len(sig)}"
        response = client.get(
            "/protected", headers={"Authorization": f"Bearer {tampered}"}
        )
        assert response.status_code == 401
        assert b"bad signature" in response.data

    def test_expired_token_returns_401(self, client: FlaskClient) -> None:
        """An expired token gets 401."""
        import time

        stale = time.time() - MAX_TOKEN_AGE_SECONDS - 60
        token = mint_token("rubotpaul", now=stale)
        response = client.get(
            "/protected", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401
        assert b"token expired" in response.data

    def test_valid_token_returns_caller_id(self, client: FlaskClient) -> None:
        """A valid token passes and the view receives the caller_id."""
        token = mint_token("rubotpaul")
        response = client.get(
            "/protected", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        assert response.get_json() == {"caller_id": "rubotpaul"}


# ---------------------------------------------------------------------------
# aiohttp integration (aiohttp_auth_middleware)
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV, clear=True)
@pytest.mark.usefixtures("fake_aiohttp")
class TestAiohttpMiddleware:
    """Tests for the aiohttp middleware using a stub aiohttp module."""

    @staticmethod
    async def _handler(request: Any) -> str:
        return "handled"

    def _run(self, headers: dict[str, str]) -> tuple[Any, Any]:
        request = _fake_aiohttp_request(headers)
        middleware = asyncio.run(aiohttp_auth_middleware(None, self._handler))
        return asyncio.run(middleware(request)), request

    def test_missing_header_returns_401(self) -> None:
        """Requests without a bearer header get a 401 JSON response."""
        response, _ = self._run({})
        assert response.status == 401
        assert response.payload == {"error": "missing bearer token"}

    def test_bad_token_returns_401(self) -> None:
        """Invalid tokens get a 401 JSON response with the reason."""
        response, _ = self._run({"Authorization": "Bearer nope"})
        assert response.status == 401
        assert response.payload == {"error": "malformed token"}

    def test_valid_token_calls_handler_and_sets_caller_id(self) -> None:
        """Valid tokens reach the handler and stash caller_id on the request."""
        token = mint_token("rubotpaul")
        response, request = self._run({"Authorization": f"Bearer {token}"})
        assert response == "handled"
        assert request.state["caller_id"] == "rubotpaul"
