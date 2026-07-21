"""Tests for the vendored RubotPaul HMAC bearer auth middleware.

Issue #74: tokens are now bound to the exact request they authorize
(method + path + body hash) via ``RequestBinding``, closing the replay
window where a token minted for one endpoint/body could be reused
against any other endpoint within its 5-minute TTL. These tests specify
that contract; until ``RequestBinding`` and the new ``mint_token`` /
``_verify_token`` / ``require_bearer`` signatures exist in
``grocery_butler.auth_middleware``, this module fails to even import
(the correct Gate 1 RED state for issue #74).
"""

from __future__ import annotations

import asyncio
import hashlib
import hmac
import os
import sys
from types import SimpleNamespace
from typing import TYPE_CHECKING, Any, cast
from unittest.mock import patch

import pytest
from flask import Flask, request

from grocery_butler.auth_middleware import (
    MAX_TOKEN_AGE_SECONDS,
    MAX_TOKEN_FUTURE_SKEW_SECONDS,
    SECRET_ENV_VAR,
    AuthError,
    RequestBinding,
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

#: The binding used by most round-trip tests: a bodyless GET to the
#: canonical read endpoint that the Flask fixture app protects.
DEFAULT_BINDING = RequestBinding.of("GET", "/protected", b"")

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app() -> Flask:
    """Create a minimal Flask app with bearer-protected GET and POST routes.

    Returns:
        A Flask app with a bodyless ``GET /protected`` route and a
        JSON-body ``POST /protected-post`` route, both guarded by
        ``require_bearer()``.
    """
    application = Flask(__name__)
    application.config["TESTING"] = True

    @application.get("/protected")
    def protected() -> dict[str, str]:
        """Return the caller_id for a valid bodyless bearer request."""
        caller_id = require_bearer()
        return {"caller_id": caller_id}

    @application.post("/protected-post")
    def protected_post() -> dict[str, Any]:
        """Return the caller_id and echoed JSON body for a bound POST.

        Proves the request body is still readable by the view after
        ``require_bearer()`` has already consumed it via
        ``request.get_data()`` to compute the binding.
        """
        caller_id = require_bearer()
        body = request.get_json(silent=True) or {}
        return {"caller_id": caller_id, "echo": body}

    return application


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    """Return a Flask test client for the protected app.

    Args:
        app: The fixture Flask application.

    Returns:
        A Flask test client bound to ``app``.
    """
    return app.test_client()


def _fake_aiohttp_request(
    headers: dict[str, str],
    *,
    method: str = "GET",
    path: str = "/",
    body: bytes = b"",
) -> Any:
    """Build a minimal stand-in for an aiohttp request.

    Args:
        headers: Header mapping the fake request exposes.
        method: HTTP method the fake request reports.
        path: Request path the fake request reports.
        body: Raw bytes returned by the fake request's async ``read()``.

    Returns:
        An object duck-typing the subset of ``aiohttp.web.Request`` the
        middleware relies on: ``headers``, ``method``, ``path``,
        ``__setitem__``, and an async ``read()``.
    """

    class FakeRequest:
        """Duck-typed aiohttp request stand-in."""

        def __init__(self) -> None:
            """Store the fake request's headers, method, path, and body."""
            self.headers = headers
            self.method = method
            self.path = path
            self.state: dict[str, str] = {}
            self._body = body

        def __setitem__(self, key: str, value: str) -> None:
            """Stash a value on the fake request, mirroring aiohttp."""
            self.state[key] = value

        async def read(self) -> bytes:
            """Return the fake request's raw body bytes."""
            return self._body

    return FakeRequest()


@pytest.fixture()
def fake_aiohttp(monkeypatch: pytest.MonkeyPatch) -> None:
    """Install a stub aiohttp module so the middleware can import it.

    Args:
        monkeypatch: Pytest's monkeypatch fixture.
    """

    def json_response(payload: dict[str, str], status: int = 200) -> SimpleNamespace:
        """Return a stand-in for ``aiohttp.web.json_response``."""
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
# RequestBinding
# ---------------------------------------------------------------------------


class TestRequestBinding:
    """Tests for the RequestBinding value object."""

    def test_of_stores_method_and_path_verbatim(self) -> None:
        """``.of`` stores method and path exactly as given, uncanonicalized."""
        binding = RequestBinding.of("get", "/api/v1/inventory", b"")
        assert binding.method == "get"
        assert binding.path == "/api/v1/inventory"

    def test_of_hashes_body_with_sha256(self) -> None:
        """``.of`` stores the hex SHA-256 digest of the given body bytes."""
        body = b'{"a": 1}'
        binding = RequestBinding.of("POST", "/api/v1/actions/confirm", body)
        assert binding.body_sha256 == hashlib.sha256(body).hexdigest()

    def test_of_empty_body_hashes_empty_bytes(self) -> None:
        """An empty body hashes to SHA-256 of the empty byte string."""
        binding = RequestBinding.of("GET", "/protected", b"")
        assert binding.body_sha256 == hashlib.sha256(b"").hexdigest()

    def test_is_frozen(self) -> None:
        """RequestBinding instances are immutable (frozen dataclass)."""
        binding = RequestBinding.of("GET", "/protected", b"")
        with pytest.raises(AttributeError):
            # Issue #74: intentional illegal write to prove runtime
            # immutability of the frozen dataclass; mypy rightly flags it.
            binding.method = "POST"  # type: ignore[misc]  # Issue #74


# ---------------------------------------------------------------------------
# mint_token / _verify_token round-trip
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV, clear=True)
class TestTokenRoundTrip:
    """Tests for minting and verifying request-bound tokens."""

    def test_valid_token_returns_caller_id(self) -> None:
        """A freshly minted token verifies against its own binding."""
        token = mint_token("rubotpaul", DEFAULT_BINDING, now=NOW)
        assert _verify_token(token, DEFAULT_BINDING, now=NOW) == "rubotpaul"

    def test_token_has_three_dot_separated_parts(self) -> None:
        """Minted tokens follow <caller_id>.<timestamp>.<hmac_hex>."""
        token = mint_token("rubotpaul", DEFAULT_BINDING, now=NOW)
        caller_id, ts, sig = token.split(".")
        assert caller_id == "rubotpaul"
        assert ts == str(int(NOW))
        assert len(sig) == 64  # SHA-256 hex digest

    def test_token_at_max_age_is_accepted(self) -> None:
        """A token exactly MAX_TOKEN_AGE_SECONDS old is still valid."""
        token = mint_token("rubotpaul", DEFAULT_BINDING, now=NOW)
        assert (
            _verify_token(token, DEFAULT_BINDING, now=NOW + MAX_TOKEN_AGE_SECONDS)
            == "rubotpaul"
        )

    def test_expired_token_rejected(self) -> None:
        """A token older than MAX_TOKEN_AGE_SECONDS is rejected."""
        token = mint_token("rubotpaul", DEFAULT_BINDING, now=NOW)
        with pytest.raises(AuthError, match="token expired") as excinfo:
            _verify_token(token, DEFAULT_BINDING, now=NOW + MAX_TOKEN_AGE_SECONDS + 1)
        assert excinfo.value.status == 401

    def test_token_at_max_future_skew_is_accepted(self) -> None:
        """A token exactly MAX_TOKEN_FUTURE_SKEW_SECONDS ahead is tolerated."""
        token = mint_token(
            "rubotpaul", DEFAULT_BINDING, now=NOW + MAX_TOKEN_FUTURE_SKEW_SECONDS
        )
        assert _verify_token(token, DEFAULT_BINDING, now=NOW) == "rubotpaul"

    def test_token_from_future_rejected(self) -> None:
        """A token more than MAX_TOKEN_FUTURE_SKEW_SECONDS ahead is rejected."""
        token = mint_token(
            "rubotpaul", DEFAULT_BINDING, now=NOW + MAX_TOKEN_FUTURE_SKEW_SECONDS + 1
        )
        with pytest.raises(AuthError, match="token from future"):
            _verify_token(token, DEFAULT_BINDING, now=NOW)

    def test_defaults_to_current_time(self) -> None:
        """Without an explicit now, mint and verify use the wall clock."""
        token = mint_token("rubotpaul", DEFAULT_BINDING)
        assert _verify_token(token, DEFAULT_BINDING) == "rubotpaul"

    @pytest.mark.parametrize("token", ["", "no-dots", "one.dot", "a.b.c.d"])
    def test_malformed_token_rejected(self, token: str) -> None:
        """Tokens without exactly three dot-separated parts are rejected."""
        with pytest.raises(AuthError, match="malformed token"):
            _verify_token(token, DEFAULT_BINDING, now=NOW)

    def test_malformed_timestamp_rejected(self) -> None:
        """A non-integer timestamp is rejected."""
        with pytest.raises(AuthError, match="malformed timestamp"):
            _verify_token("rubotpaul.not-a-number.deadbeef", DEFAULT_BINDING, now=NOW)

    def test_bad_signature_rejected(self) -> None:
        """A tampered signature is rejected."""
        token = mint_token("rubotpaul", DEFAULT_BINDING, now=NOW)
        caller_id, ts, sig = token.split(".")
        tampered_sig = ("0" if sig[0] != "0" else "1") + sig[1:]
        with pytest.raises(AuthError, match="bad signature"):
            _verify_token(f"{caller_id}.{ts}.{tampered_sig}", DEFAULT_BINDING, now=NOW)

    def test_tampered_caller_id_rejected(self) -> None:
        """Changing the caller_id invalidates the signature."""
        token = mint_token("rubotpaul", DEFAULT_BINDING, now=NOW)
        _, ts, sig = token.split(".")
        with pytest.raises(AuthError, match="bad signature"):
            _verify_token(f"impostor.{ts}.{sig}", DEFAULT_BINDING, now=NOW)

    def test_token_minted_with_other_secret_rejected(self) -> None:
        """A token signed with a different secret is rejected."""
        with patch.dict(os.environ, {SECRET_ENV_VAR: "other-secret"}):
            token = mint_token("rubotpaul", DEFAULT_BINDING, now=NOW)
        with pytest.raises(AuthError, match="bad signature"):
            _verify_token(token, DEFAULT_BINDING, now=NOW)


# ---------------------------------------------------------------------------
# AC#1: a token bound to one request is rejected for a different request
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV, clear=True)
class TestRequestBindingMismatch:
    """A token's signature is invalid for any request it wasn't minted for."""

    def test_read_token_replayed_against_destructive_endpoint_rejected(self) -> None:
        """AC#1: a GET /inventory token is rejected for POST /actions/confirm.

        This is the headline vulnerability issue #74 closes: previously
        an unbound token minted for a harmless read endpoint could be
        replayed against any destructive endpoint within its 5-minute
        TTL.
        """
        read_binding = RequestBinding.of("GET", "/api/v1/inventory", b"")
        token = mint_token("rubotpaul", read_binding, now=NOW)

        confirm_binding = RequestBinding.of(
            "POST", "/api/v1/actions/confirm", b'{"action_id": "x"}'
        )
        with pytest.raises(AuthError, match="bad signature"):
            _verify_token(token, confirm_binding, now=NOW)

    def test_method_only_mismatch_rejected(self) -> None:
        """Changing only the method invalidates the signature."""
        token = mint_token(
            "rubotpaul", RequestBinding.of("GET", "/protected", b""), now=NOW
        )
        with pytest.raises(AuthError, match="bad signature"):
            _verify_token(token, RequestBinding.of("POST", "/protected", b""), now=NOW)

    def test_path_only_mismatch_rejected(self) -> None:
        """Changing only the path invalidates the signature."""
        token = mint_token(
            "rubotpaul", RequestBinding.of("GET", "/protected", b""), now=NOW
        )
        with pytest.raises(AuthError, match="bad signature"):
            _verify_token(token, RequestBinding.of("GET", "/other", b""), now=NOW)

    def test_body_only_mismatch_rejected(self) -> None:
        """Changing a single byte of the body invalidates the signature."""
        token = mint_token(
            "rubotpaul",
            RequestBinding.of("POST", "/protected", b"payload-a"),
            now=NOW,
        )
        with pytest.raises(AuthError, match="bad signature"):
            _verify_token(
                token,
                RequestBinding.of("POST", "/protected", b"payload-b"),
                now=NOW,
            )

    def test_empty_body_distinguished_from_nonempty_body(self) -> None:
        """A token minted for an empty body is rejected for a non-empty one."""
        token = mint_token(
            "rubotpaul", RequestBinding.of("POST", "/protected", b""), now=NOW
        )
        with pytest.raises(AuthError, match="bad signature"):
            _verify_token(
                token,
                RequestBinding.of("POST", "/protected", b"not-empty"),
                now=NOW,
            )

    def test_method_case_insensitive_at_verification(self) -> None:
        """Method casing is canonicalized to uppercase in the signing payload.

        Minting against a lowercase method and verifying against the
        uppercase form of the same method (as Flask/aiohttp always
        report it) must succeed -- the wire format never transmits the
        method, so canonicalization has to happen identically on both
        sides.
        """
        token = mint_token(
            "rubotpaul", RequestBinding.of("get", "/protected", b""), now=NOW
        )
        assert (
            _verify_token(token, RequestBinding.of("GET", "/protected", b""), now=NOW)
            == "rubotpaul"
        )


# ---------------------------------------------------------------------------
# Legacy (unbound) tokens are rejected outright
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV, clear=True)
class TestLegacyTokenRejected:
    """Pre-issue-74 unbound tokens must be rejected, with no compatibility flag."""

    def test_legacy_signature_scheme_rejected(self) -> None:
        """A hand-crafted legacy-format token fails signature verification.

        The legacy scheme signed only ``f"{caller_id}.{ts}"`` with no
        method/path/body binding at all. This is a hard cutover: there
        is no environment flag to keep accepting it.
        """
        caller_id = "rubotpaul"
        ts = int(NOW)
        legacy_sig = hmac.new(
            TEST_SECRET.encode(),
            f"{caller_id}.{ts}".encode(),
            hashlib.sha256,
        ).hexdigest()
        legacy_token = f"{caller_id}.{ts}.{legacy_sig}"

        with pytest.raises(AuthError, match="bad signature"):
            _verify_token(legacy_token, DEFAULT_BINDING, now=NOW)


# ---------------------------------------------------------------------------
# Missing shared secret
# ---------------------------------------------------------------------------


class TestMissingSecret:
    """Tests for missing RUBOTPAUL_SHARED_SECRET."""

    @patch.dict(os.environ, {}, clear=True)
    def test_mint_token_raises_when_secret_unset(self) -> None:
        """mint_token fails loud when the shared secret is not set."""
        with pytest.raises(RuntimeError, match=SECRET_ENV_VAR):
            mint_token("rubotpaul", DEFAULT_BINDING, now=NOW)

    @patch.dict(os.environ, {SECRET_ENV_VAR: ""}, clear=True)
    def test_mint_token_raises_when_secret_empty(self) -> None:
        """mint_token treats an empty shared secret as unset."""
        with pytest.raises(RuntimeError, match=SECRET_ENV_VAR):
            mint_token("rubotpaul", DEFAULT_BINDING, now=NOW)

    def test_verify_token_raises_when_secret_unset(self) -> None:
        """_verify_token fails loud when the shared secret is not set."""
        with patch.dict(os.environ, SECRET_ENV, clear=True):
            token = mint_token("rubotpaul", DEFAULT_BINDING, now=NOW)
        with (
            patch.dict(os.environ, {}, clear=True),
            pytest.raises(RuntimeError, match=SECRET_ENV_VAR),
        ):
            _verify_token(token, DEFAULT_BINDING, now=NOW)


# ---------------------------------------------------------------------------
# Flask integration (require_bearer)
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV, clear=True)
class TestRequireBearerFlask:
    """Tests for require_bearer via a minimal Flask app.

    ``require_bearer()`` takes no arguments -- it builds the
    ``RequestBinding`` itself from the live Flask ``request`` (method,
    path, and raw body bytes).
    """

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
        token = mint_token("rubotpaul", DEFAULT_BINDING)
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
        token = mint_token("rubotpaul", DEFAULT_BINDING, now=stale)
        response = client.get(
            "/protected", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 401
        assert b"token expired" in response.data

    def test_valid_token_returns_caller_id(self, client: FlaskClient) -> None:
        """A valid bodyless GET token passes and the view gets the caller_id."""
        token = mint_token("rubotpaul", DEFAULT_BINDING)
        response = client.get(
            "/protected", headers={"Authorization": f"Bearer {token}"}
        )
        assert response.status_code == 200
        assert response.get_json() == {"caller_id": "rubotpaul"}

    def test_valid_bound_post_token_passes_and_body_still_readable(
        self, client: FlaskClient
    ) -> None:
        """A token bound to the exact POST body succeeds; the view reads it.

        Confirms ``require_bearer()`` reading ``request.get_data()`` to
        build the binding does not consume the body stream out from
        under the view -- ``request.get_json()`` still works afterward.
        """
        body = b'{"greeting": "hi"}'
        binding = RequestBinding.of("POST", "/protected-post", body)
        token = mint_token("rubotpaul", binding)
        response = client.post(
            "/protected-post",
            data=body,
            content_type="application/json",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 200
        assert response.get_json() == {
            "caller_id": "rubotpaul",
            "echo": {"greeting": "hi"},
        }

    def test_token_bound_to_different_route_returns_401(
        self, client: FlaskClient
    ) -> None:
        """A token minted for GET /protected is rejected on the POST route."""
        token = mint_token("rubotpaul", DEFAULT_BINDING)
        response = client.post(
            "/protected-post",
            data=b"{}",
            content_type="application/json",
            headers={"Authorization": f"Bearer {token}"},
        )
        assert response.status_code == 401
        assert b"bad signature" in response.data


# ---------------------------------------------------------------------------
# aiohttp integration (aiohttp_auth_middleware)
# ---------------------------------------------------------------------------


@patch.dict(os.environ, SECRET_ENV, clear=True)
@pytest.mark.usefixtures("fake_aiohttp")
class TestAiohttpMiddleware:
    """Tests for the aiohttp middleware using a stub aiohttp module.

    The middleware builds its ``RequestBinding`` from
    ``request.method``, ``request.path``, and ``await request.read()``.
    """

    @staticmethod
    async def _handler(request: Any) -> str:
        """Return a fixed sentinel proving the wrapped handler ran."""
        return "handled"

    def _run(
        self,
        headers: dict[str, str],
        *,
        method: str = "GET",
        path: str = "/",
        body: bytes = b"",
    ) -> tuple[Any, Any]:
        """Run the middleware against one fake request; return (result, request).

        Args:
            headers: Header mapping for the fake request.
            method: HTTP method the fake request reports.
            path: Path the fake request reports.
            body: Raw body bytes the fake request's ``read()`` returns.

        Returns:
            A tuple of the middleware's return value and the fake
            request object it was given (so tests can inspect stashed
            state).
        """
        fake_request = _fake_aiohttp_request(
            headers, method=method, path=path, body=body
        )
        middleware = asyncio.run(aiohttp_auth_middleware(None, self._handler))
        return asyncio.run(middleware(fake_request)), fake_request

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
        """A token bound to the exact fake request reaches the handler.

        The caller_id is stashed on the request for the wrapped handler.
        """
        binding = RequestBinding.of("GET", "/", b"")
        token = mint_token("rubotpaul", binding)
        response, fake_request = self._run(
            {"Authorization": f"Bearer {token}"}, method="GET", path="/", body=b""
        )
        assert response == "handled"
        assert fake_request.state["caller_id"] == "rubotpaul"

    def test_valid_token_with_body_binds_on_read_bytes(self) -> None:
        """A token bound to a specific body succeeds when read() matches."""
        body = b'{"k": "v"}'
        binding = RequestBinding.of("POST", "/webhook", body)
        token = mint_token("rubotpaul", binding)
        response, _ = self._run(
            {"Authorization": f"Bearer {token}"},
            method="POST",
            path="/webhook",
            body=body,
        )
        assert response == "handled"

    def test_cross_endpoint_replay_rejected(self) -> None:
        """A token minted for one path is rejected when replayed on another.

        This is the aiohttp-side mirror of AC#1: a token bound to
        ``GET /`` must not authorize a different endpoint.
        """
        binding = RequestBinding.of("GET", "/", b"")
        token = mint_token("rubotpaul", binding)
        response, _ = self._run(
            {"Authorization": f"Bearer {token}"},
            method="POST",
            path="/webhook",
            body=b'{"attack": true}',
        )
        assert response.status == 401
        assert response.payload == {"error": "bad signature"}
