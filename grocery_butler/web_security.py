"""Browser-facing security baseline for the dashboard (backlog P0.10).

Three protections, registered once by ``create_app()``:

- **CSRF tokens.** Every state-changing dashboard request must carry the
  token stored in the visitor's session. The tailnet guard keeps strangers
  out, but it can't stop a public web page, open in a household member's
  browser, from posting a form at the dashboard's tailnet address. That
  page can't read the token, so its request is refused.
- **Security headers** on every response, including a Content Security
  Policy that allows same-origin content only.
- **A request size limit**, so an oversized body is refused before a view
  reads it.

``/api/v1`` is exempt from the CSRF check: it authenticates each request
with an HMAC bearer token and never reads the session cookie, so a browser
can't be tricked into making an authenticated call to it.
"""

from __future__ import annotations

import hmac
import secrets
from typing import TYPE_CHECKING

from flask import current_app, jsonify, render_template, request, session

if TYPE_CHECKING:
    from flask import Flask, Response

CSRF_FIELD = "csrf_token"
CSRF_HEADER = "X-CSRF-Token"
CSRF_CONFIG_KEY = "WEB_CSRF_ENABLED"
_SESSION_KEY = "_csrf_token"
_SAFE_METHODS = frozenset({"GET", "HEAD", "OPTIONS", "TRACE"})
_API_PREFIX = "/api/v1/"

# Forms and JSON only. Photo uploads (Epic 3) get their own, larger limit on
# their own route when they arrive.
MAX_REQUEST_BYTES = 1024 * 1024

CSRF_FAILURE_MESSAGE = (
    "This page expired before the change was sent, so nothing was changed. "
    "Go back, reload the page, and try again."
)

SECURITY_HEADERS: dict[str, str] = {
    "Content-Security-Policy": (
        "default-src 'self'; "
        "img-src 'self' data:; "
        "object-src 'none'; "
        "base-uri 'self'; "
        "form-action 'self'; "
        "frame-ancestors 'none'"
    ),
    "X-Content-Type-Options": "nosniff",
    "X-Frame-Options": "DENY",
    "Referrer-Policy": "same-origin",
}


def csrf_token() -> str:
    """Return this session's CSRF token, creating it on first use.

    Exposed to templates as ``csrf_token()``. The token stays the same for
    the life of the session, so a form opened in one tab still works after
    another tab loads a page.

    Returns:
        The URL-safe token bound to the current session.
    """
    token = session.get(_SESSION_KEY)
    if not isinstance(token, str):
        token = secrets.token_urlsafe(32)
        session[_SESSION_KEY] = token
    return token


def _submitted_token() -> str:
    """Read the token a request sent, from its header or its form.

    Returns:
        The submitted token, or an empty string when there is none.
    """
    return request.headers.get(CSRF_HEADER) or request.form.get(CSRF_FIELD, "")


def _token_matches() -> bool:
    """Check the submitted token against the session's, in constant time.

    Returns:
        True only when the session has a token and the request sent it.
    """
    expected = session.get(_SESSION_KEY)
    if not isinstance(expected, str):
        return False
    return hmac.compare_digest(expected.encode(), _submitted_token().encode())


def _csrf_failure() -> tuple[Response | str, int]:
    """Build the 400 response for a request that failed the CSRF check.

    Returns:
        JSON for a JSON request (the inventory toggle shows its ``error``),
        otherwise the HTML error page.
    """
    if request.is_json:
        return jsonify(success=False, error=CSRF_FAILURE_MESSAGE), 400
    return render_template("error.html", code=400, message=CSRF_FAILURE_MESSAGE), 400


def _enforce_csrf() -> tuple[Response | str, int] | None:
    """Refuse a state-changing dashboard request that lacks the token.

    Returns:
        None to let the request through, or the 400 response.
    """
    if request.method in _SAFE_METHODS or request.path.startswith(_API_PREFIX):
        return None
    if not current_app.config.get(CSRF_CONFIG_KEY, True):
        return None
    if _token_matches():
        return None
    return _csrf_failure()


def _add_security_headers(response: Response) -> Response:
    """Set the baseline security headers on a response.

    Args:
        response: The outgoing response.

    Returns:
        The same response, with every header in ``SECURITY_HEADERS`` set.
    """
    for name, value in SECURITY_HEADERS.items():
        response.headers[name] = value
    return response


def register_web_security(app: Flask) -> None:
    """Install CSRF protection, security headers and the request size limit.

    Call after the network guard is registered, so a request from outside
    the tailnet is rejected before the CSRF check reads its body.

    Args:
        app: Flask application instance to protect.
    """
    if app.config.get("MAX_CONTENT_LENGTH") is None:
        app.config["MAX_CONTENT_LENGTH"] = MAX_REQUEST_BYTES
    app.config["SESSION_COOKIE_SAMESITE"] = "Lax"
    app.config["SESSION_COOKIE_HTTPONLY"] = True
    app.jinja_env.globals[CSRF_FIELD] = csrf_token
    app.before_request(_enforce_csrf)
    app.after_request(_add_security_headers)
