"""Tests for the browser-facing security baseline (backlog P0.10, #138, #89).

Covers CSRF protection for dashboard mutations, the response security
headers, the request size limit, and the absence of third-party fetches.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import TYPE_CHECKING

import flask
import pytest

from grocery_butler.app import create_app
from grocery_butler.web_security import (
    CSRF_FIELD,
    CSRF_HEADER,
    MAX_REQUEST_BYTES,
    SECURITY_HEADERS,
    register_web_security,
)

if TYPE_CHECKING:
    from flask import Flask
    from flask.testing import FlaskClient

_PACKAGE = Path(__file__).resolve().parent.parent / "grocery_butler"
_TOKEN_RE = re.compile(r'name="csrf_token" value="([^"]+)"')
_META_RE = re.compile(r'<meta name="csrf-token" content="([^"]+)">')


@pytest.fixture()
def app(tmp_path: Path) -> Flask:
    """Create an app with CSRF protection on, as in production."""
    application = create_app(db_path=str(tmp_path / "web_security.db"))
    application.config["TESTING"] = True
    return application


@pytest.fixture()
def client(app: Flask) -> FlaskClient:
    """Return a test client that keeps its session cookie between requests."""
    return app.test_client()


def _form_token(client: FlaskClient, page: str = "/pantry") -> str:
    """Load a page and return the CSRF token its form carries.

    Args:
        client: Test client whose session receives the token.
        page: A page that renders at least one form.

    Returns:
        The token from the first hidden ``csrf_token`` input.
    """
    match = _TOKEN_RE.search(client.get(page).get_data(as_text=True))
    assert match is not None, f"{page} renders no csrf_token input"
    return match.group(1)


class TestCsrfProtection:
    """Dashboard mutations need the session's CSRF token."""

    def test_form_post_without_token_is_rejected(self, client: FlaskClient) -> None:
        """A cross-site form post carries no token and changes nothing."""
        response = client.post(
            "/pantry/add", data={"ingredient": "salt", "category": "pantry_dry"}
        )

        assert response.status_code == 400
        assert "expired" in response.get_data(as_text=True).lower()

    def test_form_post_with_token_succeeds(self, client: FlaskClient) -> None:
        """The token rendered into the form is accepted."""
        token = _form_token(client)

        response = client.post(
            "/pantry/add",
            data={"ingredient": "saffron", "category": "pantry_dry", CSRF_FIELD: token},
        )

        assert response.status_code == 302

    def test_wrong_token_is_rejected(self, client: FlaskClient) -> None:
        """A guessed token does not match the session's."""
        _form_token(client)

        response = client.post(
            "/pantry/add",
            data={"ingredient": "salt", "category": "pantry_dry", CSRF_FIELD: "nope"},
        )

        assert response.status_code == 400

    def test_token_from_another_session_is_rejected(self, app: Flask) -> None:
        """A token is bound to the session that was issued it."""
        attacker_token = _form_token(app.test_client())
        victim = app.test_client()
        _form_token(victim)

        response = victim.post(
            "/pantry/add",
            data={
                "ingredient": "salt",
                "category": "pantry_dry",
                CSRF_FIELD: attacker_token,
            },
        )

        assert response.status_code == 400

    def test_token_is_stable_within_a_session(self, client: FlaskClient) -> None:
        """Two pages in one session carry the same token, so tabs don't clash."""
        assert _form_token(client, "/pantry") == _form_token(client, "/brands")

    def test_json_post_without_token_gets_a_json_error(
        self, client: FlaskClient
    ) -> None:
        """The inventory toggle's fetch gets JSON it can show, not an HTML page."""
        response = client.post(
            "/inventory/update", json={"ingredient": "milk", "status": "low"}
        )

        assert response.status_code == 400
        assert "error" in response.get_json()

    def test_json_post_with_header_token_passes_the_check(
        self, client: FlaskClient
    ) -> None:
        """The token from the page's meta tag is accepted in the header."""
        match = _META_RE.search(client.get("/inventory").get_data(as_text=True))
        assert match is not None

        response = client.post(
            "/inventory/update",
            json={"ingredient": "no-such-item", "status": "low"},
            headers={CSRF_HEADER: match.group(1)},
        )

        assert response.status_code == 404

    def test_get_requests_need_no_token(self, client: FlaskClient) -> None:
        """Safe methods are never checked."""
        assert client.get("/pantry").status_code == 200

    def test_api_v1_is_exempt_and_still_needs_its_bearer_token(
        self, client: FlaskClient
    ) -> None:
        """The JSON API authenticates by bearer token, not a session cookie."""
        response = client.post("/api/v1/stock/update", json={"text": "out of milk"})

        assert response.status_code == 401

    def test_every_post_form_carries_the_token(self) -> None:
        """A new form without the hidden input fails here, not in production."""
        missing = []
        for template in sorted((_PACKAGE / "templates").glob("*.html")):
            html = template.read_text()
            for form in re.findall(r"<form\b.*?</form>", html, re.DOTALL):
                if 'method="post"' in form and "csrf_token()" not in form:
                    missing.append(template.name)

        assert missing == []

    def test_can_be_switched_off_for_tests_that_exercise_other_things(
        self, app: Flask
    ) -> None:
        """WEB_CSRF_ENABLED=False skips the check."""
        app.config["WEB_CSRF_ENABLED"] = False

        response = app.test_client().post(
            "/pantry/add", data={"ingredient": "salt", "category": "pantry_dry"}
        )

        assert response.status_code == 302


class TestSessionCookie:
    """The session cookie that holds the token is not sent cross-site."""

    def test_session_cookie_is_samesite_lax_and_http_only(self, app: Flask) -> None:
        """Lax keeps the cookie off cross-site form posts."""
        assert app.config["SESSION_COOKIE_SAMESITE"] == "Lax"
        assert app.config["SESSION_COOKIE_HTTPONLY"] is True


class TestSecurityHeaders:
    """Every response carries the baseline headers."""

    @pytest.mark.parametrize(
        "path", ["/", "/health", "/no-such-page", "/static/js/app.js"]
    )
    def test_headers_on_every_kind_of_response(
        self, client: FlaskClient, path: str
    ) -> None:
        """Pages, health checks, errors and static files all get them."""
        response = client.get(path)

        for name, value in SECURITY_HEADERS.items():
            assert response.headers.get(name) == value, name

    def test_policy_allows_only_same_origin_content(self) -> None:
        """No inline script, no third-party origin, no framing."""
        policy = SECURITY_HEADERS["Content-Security-Policy"]

        assert "default-src 'self'" in policy
        assert "frame-ancestors 'none'" in policy
        assert "object-src 'none'" in policy
        assert "form-action 'self'" in policy
        assert "unsafe-inline" not in policy
        assert "http" not in policy

    def test_other_headers(self) -> None:
        """The standard hardening headers have their strict values."""
        assert SECURITY_HEADERS["X-Content-Type-Options"] == "nosniff"
        assert SECURITY_HEADERS["X-Frame-Options"] == "DENY"
        assert SECURITY_HEADERS["Referrer-Policy"] == "same-origin"


class TestContentSecurityPolicyCompliance:
    """The templates and assets keep the promises the policy makes."""

    def test_templates_have_no_inline_script_or_style(self) -> None:
        """Inline handlers and styles would be blocked by the policy."""
        offenders = []
        pattern = re.compile(r"\son[a-z]+=|\sstyle=|<style\b|<script(?![^>]*\ssrc=)")
        for template in sorted((_PACKAGE / "templates").glob("*.html")):
            if pattern.search(template.read_text()):
                offenders.append(template.name)

        assert offenders == []

    def test_no_third_party_origins_in_templates_or_assets(self) -> None:
        """Nothing the browser loads leaves the tailnet (#89 item 4)."""
        offenders = []
        files = [
            *(_PACKAGE / "templates").glob("*.html"),
            *(_PACKAGE / "static" / "css").glob("*.css"),
            *(_PACKAGE / "static" / "js").glob("*.js"),
        ]
        for path in sorted(files):
            if re.search(r"https?://", path.read_text()):
                offenders.append(path.name)

        assert offenders == []


class TestRequestSizeLimit:
    """Oversized request bodies are refused before a view reads them."""

    def test_limit_is_one_mebibyte(self, app: Flask) -> None:
        """The dashboard and API take small forms and JSON only."""
        assert MAX_REQUEST_BYTES == 1024 * 1024
        assert app.config["MAX_CONTENT_LENGTH"] == MAX_REQUEST_BYTES

    def test_an_explicit_limit_is_kept(self) -> None:
        """A limit set before registration (a future upload app) is not lowered."""
        application = flask.Flask(__name__)
        application.config["MAX_CONTENT_LENGTH"] = 5 * MAX_REQUEST_BYTES

        register_web_security(application)

        assert application.config["MAX_CONTENT_LENGTH"] == 5 * MAX_REQUEST_BYTES

    def test_oversized_form_post_is_rejected(self, client: FlaskClient) -> None:
        """A body over the limit gets 413."""
        token = _form_token(client)

        response = client.post(
            "/pantry/add",
            data={"ingredient": "x" * (MAX_REQUEST_BYTES + 1), CSRF_FIELD: token},
        )

        assert response.status_code == 413

    def test_oversized_api_post_gets_a_json_error(self, client: FlaskClient) -> None:
        """/api/v1 keeps its JSON error contract at the limit.

        The bearer check hashes the body, so the limit trips there, before
        the token is verified.
        """
        response = client.post(
            "/api/v1/meals/parse",
            data=b"x" * (MAX_REQUEST_BYTES + 1),
            content_type="application/json",
            headers={"Authorization": "Bearer any.token.value"},
        )

        assert response.status_code == 413
        assert response.get_json() == {"error": "request too large"}
