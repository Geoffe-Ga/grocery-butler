"""Safeway API client with Okta authentication and rate limiting.

Handles the Okta auth flow (username/password -> session token ->
access token), token lifecycle management, and rate-limited HTTP
requests to the Safeway Nimbus API.

Authentication flow:
1. POST credentials to ``https://albertsons.okta.com/api/v1/authn``
2. Exchange session token for access token via OAuth2 implicit grant
3. Use bearer token for Nimbus API calls
4. Auto-refresh when token approaches expiry
"""

from __future__ import annotations

import logging
import time
from dataclasses import dataclass, field
from datetime import UTC, datetime, timedelta
from typing import Any

import httpx

logger = logging.getLogger(__name__)

OKTA_BASE = "https://albertsons.okta.com"
OKTA_CLIENT_ID = "ausp6soxrIyPrm8rS2p6"
NIMBUS_BASE = "https://nimbus.safeway.com"

# Rate limiter: ~2 requests/second = 0.5s between requests
_MIN_REQUEST_INTERVAL = 0.5

# Refresh tokens 5 minutes before they expire
_TOKEN_REFRESH_BUFFER = timedelta(minutes=5)


class SafewayAuthError(Exception):
    """Raised when Safeway authentication fails."""


class SafewayAPIError(Exception):
    """Raised when a Safeway API call fails."""


@dataclass
class TokenState:
    """Internal token lifecycle state.

    Attributes:
        access_token: The OAuth2 bearer token.
        expires_at: UTC datetime when the token expires.
    """

    access_token: str = ""
    expires_at: datetime = field(
        default_factory=lambda: datetime.min.replace(tzinfo=UTC),
    )

    @property
    def is_expired(self) -> bool:
        """Check if the token needs refreshing.

        Returns:
            True if the token is expired or within the refresh buffer.
        """
        now = datetime.now(tz=UTC)
        # Guard against overflow when expires_at is near datetime.min
        try:
            threshold = self.expires_at - _TOKEN_REFRESH_BUFFER
        except OverflowError:
            return True
        return now >= threshold


class SafewayClient:
    """Safeway API client with Okta authentication and rate limiting.

    Manages the full authentication lifecycle and provides rate-limited
    HTTP methods for interacting with the Nimbus API.

    Args:
        username: Safeway account email/username.
        password: Safeway account password.
        store_id: Safeway store ID for product queries.
        http_client: Optional pre-configured httpx.Client for testing.
    """

    def __init__(
        self,
        username: str,
        password: str,
        store_id: str,
        http_client: httpx.Client | None = None,
    ) -> None:
        """Initialize the Safeway client.

        Args:
            username: Safeway account email/username.
            password: Safeway account password.
            store_id: Safeway store ID for product queries.
            http_client: Optional pre-configured httpx.Client for testing.
        """
        self._username = username
        self._password = password
        self._store_id = store_id
        self._client = http_client or httpx.Client(timeout=30.0)
        self._owns_client = http_client is None
        self._token = TokenState()
        self._last_request_time = 0.0

    @property
    def store_id(self) -> str:
        """Return the configured store ID.

        Returns:
            Safeway store ID string.
        """
        return self._store_id

    @property
    def is_authenticated(self) -> bool:
        """Check if the client has a valid, non-expired token.

        Returns:
            True if authenticated with a valid token.
        """
        return bool(self._token.access_token) and not self._token.is_expired

    # ------------------------------------------------------------------
    # Authentication
    # ------------------------------------------------------------------

    def authenticate(self) -> None:
        """Perform the full Okta authentication flow.

        Steps:
        1. POST credentials to Okta ``/api/v1/authn`` for session token
        2. Exchange session token for access token via OAuth2 authorize

        Raises:
            SafewayAuthError: If any authentication step fails.
        """
        session_token = self._get_session_token()
        self._exchange_for_access_token(session_token)

    def _get_session_token(self) -> str:
        """Get an Okta session token with username/password.

        Returns:
            Okta session token string.

        Raises:
            SafewayAuthError: If the authn request fails.
        """
        self._rate_limit()
        try:
            response = self._client.post(
                f"{OKTA_BASE}/api/v1/authn",
                json={
                    "username": self._username,
                    "password": self._password,
                },
                headers={
                    "Accept": "application/json",
                    "Content-Type": "application/json",
                },
            )
            response.raise_for_status()
        except httpx.HTTPStatusError as exc:
            raise SafewayAuthError(
                f"Okta authn failed: {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise SafewayAuthError(f"Okta authn request failed: {exc}") from exc

        return _extract_session_token(response.json())

    def _exchange_for_access_token(self, session_token: str) -> None:
        """Exchange a session token for an OAuth2 access token.

        Args:
            session_token: Okta session token from authn step.

        Raises:
            SafewayAuthError: If the authorize request fails.
        """
        self._rate_limit()
        try:
            response = self._client.get(
                f"{OKTA_BASE}/oauth2/{OKTA_CLIENT_ID}/v1/authorize",
                params={
                    "client_id": OKTA_CLIENT_ID,
                    "redirect_uri": "https://www.safeway.com",
                    "response_type": "token",
                    "scope": "openid profile email",
                    "sessionToken": session_token,
                    "state": "grocery-butler",
                },
                follow_redirects=False,
            )
        except httpx.HTTPError as exc:
            raise SafewayAuthError(f"Okta authorize request failed: {exc}") from exc

        location = response.headers.get("location", "")
        self._token = _parse_token_from_redirect(location)
        logger.info("Authenticated with Safeway")

    def _ensure_authenticated(self) -> None:
        """Ensure the client has a valid token, refreshing if needed.

        Raises:
            SafewayAuthError: If re-authentication fails.
        """
        if not self._token.access_token or self._token.is_expired:
            self.authenticate()

    # ------------------------------------------------------------------
    # Rate limiting
    # ------------------------------------------------------------------

    def _rate_limit(self) -> None:
        """Enforce rate limiting (~2 requests/second)."""
        now = time.monotonic()
        elapsed = now - self._last_request_time
        if elapsed < _MIN_REQUEST_INTERVAL:
            time.sleep(_MIN_REQUEST_INTERVAL - elapsed)
        self._last_request_time = time.monotonic()

    # ------------------------------------------------------------------
    # HTTP methods
    # ------------------------------------------------------------------

    def get(
        self,
        path: str,
        params: dict[str, str] | None = None,
    ) -> dict[str, Any]:
        """Make an authenticated GET request to the Nimbus API.

        Automatically handles authentication and retries once on 401.

        Args:
            path: API path (appended to Nimbus base URL).
            params: Optional query parameters.

        Returns:
            Parsed JSON response dict.

        Raises:
            SafewayAuthError: If authentication fails.
            SafewayAPIError: If the API request fails.
        """
        self._ensure_authenticated()
        return self._request("GET", path, params=params)

    def post(
        self,
        path: str,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Make an authenticated POST request to the Nimbus API.

        Automatically handles authentication and retries once on 401.

        Args:
            path: API path (appended to Nimbus base URL).
            json_data: Optional JSON body.

        Returns:
            Parsed JSON response dict.

        Raises:
            SafewayAuthError: If authentication fails.
            SafewayAPIError: If the API request fails.
        """
        self._ensure_authenticated()
        return self._request("POST", path, json_data=json_data)

    def _request(
        self,
        method: str,
        path: str,
        params: dict[str, str] | None = None,
        json_data: dict[str, Any] | None = None,
    ) -> dict[str, Any]:
        """Execute an API request with retry on 401.

        Args:
            method: HTTP method (GET or POST).
            path: API path relative to Nimbus base URL.
            params: Optional query parameters.
            json_data: Optional JSON body for POST requests.

        Returns:
            Parsed JSON response dict.

        Raises:
            SafewayAPIError: If the request fails after retry.
        """
        url = f"{NIMBUS_BASE}{path}"
        result = self._send_request(method, url, params, json_data)
        if result is not None:
            return result

        # 401 — re-authenticate and retry once
        self.authenticate()
        retry = self._send_request(method, url, params, json_data)
        if retry is not None:
            return retry
        raise SafewayAPIError(f"Safeway API failed after re-auth: {method} {path}")

    def _send_request(
        self,
        method: str,
        url: str,
        params: dict[str, str] | None,
        json_data: dict[str, Any] | None,
    ) -> dict[str, Any] | None:
        """Send a single HTTP request, returning None on 401.

        Args:
            method: HTTP method.
            url: Full URL.
            params: Optional query parameters.
            json_data: Optional JSON body.

        Returns:
            Parsed JSON response, or None if 401 received.

        Raises:
            SafewayAPIError: On non-401 HTTP errors.
        """
        self._rate_limit()
        try:
            response = self._client.request(
                method,
                url,
                params=params,
                json=json_data,
                headers=self._get_auth_headers(),
            )
            response.raise_for_status()
            return response.json()  # type: ignore[no-any-return]  # httpx returns Any
        except httpx.HTTPStatusError as exc:
            if exc.response.status_code == 401:
                return None
            raise SafewayAPIError(
                f"Safeway API error: {exc.response.status_code}"
            ) from exc
        except httpx.HTTPError as exc:
            raise SafewayAPIError(f"Safeway API request failed: {exc}") from exc

    def _get_auth_headers(self) -> dict[str, str]:
        """Build authorization headers for API requests.

        Returns:
            Dict with Authorization and Accept headers.
        """
        return {
            "Authorization": f"Bearer {self._token.access_token}",
            "Accept": "application/json",
        }

    # ------------------------------------------------------------------
    # Lifecycle
    # ------------------------------------------------------------------

    def close(self) -> None:
        """Close the underlying HTTP client if we own it."""
        if self._owns_client:
            self._client.close()


# ------------------------------------------------------------------
# Pure helper functions
# ------------------------------------------------------------------


def _extract_session_token(data: dict[str, Any]) -> str:
    """Extract session token from Okta authn response.

    Args:
        data: Parsed JSON response from Okta authn endpoint.

    Returns:
        Session token string.

    Raises:
        SafewayAuthError: If no session token in response.
    """
    token = data.get("sessionToken")
    if not token:
        status = data.get("status", "unknown")
        raise SafewayAuthError(f"No session token in Okta response (status={status})")
    return str(token)


def _parse_token_from_redirect(location: str) -> TokenState:
    """Parse an access token from an OAuth2 redirect URL fragment.

    The implicit grant flow returns tokens in the URL fragment:
    ``https://example.com#access_token=...&expires_in=3600``

    Args:
        location: The Location header from the OAuth2 redirect.

    Returns:
        Populated TokenState with access token and expiry.

    Raises:
        SafewayAuthError: If the redirect URL is missing or malformed.
    """
    if "#" not in location:
        raise SafewayAuthError("No fragment in redirect URL")

    fragment = location.split("#", 1)[1]
    params = dict(p.split("=", 1) for p in fragment.split("&") if "=" in p)

    access_token = params.get("access_token")
    if not access_token:
        raise SafewayAuthError("No access_token in redirect fragment")

    expires_in = _parse_expires_in(params.get("expires_in", "3600"))
    return TokenState(
        access_token=access_token,
        expires_at=datetime.now(tz=UTC) + timedelta(seconds=expires_in),
    )


def _parse_expires_in(raw: str) -> int:
    """Parse the expires_in value from an OAuth2 response.

    Args:
        raw: Raw string value of expires_in parameter.

    Returns:
        Token lifetime in seconds (defaults to 3600 on parse error).
    """
    try:
        return int(raw)
    except ValueError:
        return 3600
