"""The transport that really calls Instacart (ADR 0014, #148).

This is the only module in the package that can reach a network. It posts
one documented request to one of two fixed hosts over HTTPS and maps the
answer to a typed response or a :class:`TransportError` with a stable code.

The API key is held here and nowhere else. It is sent in the
``Authorization`` header and never appears in ``repr``, a log line or an
error: errors carry a code and a status, not the request.
"""

from __future__ import annotations

import http.client
import json
from typing import TYPE_CHECKING, NamedTuple

from grocery_butler.fulfillment.instacart.models import (
    ProductsLinkRequest,
    ProductsLinkResponse,
)
from grocery_butler.fulfillment.instacart.transport import TransportError

if TYPE_CHECKING:
    from collections.abc import Callable

DEVELOPMENT_HOST = "connect.dev.instacart.tools"
PRODUCTION_HOST = "connect.instacart.com"
HOSTS = frozenset({DEVELOPMENT_HOST, PRODUCTION_HOST})
PRODUCTS_LINK_PATH = "/idp/v1/products/products_link"
TIMEOUT_SECONDS = 10.0

_OK = 200
_SERVER_ERROR = 500
_STATUS_CODES = {
    400: "bad_request",
    401: "unauthorized",
    403: "unauthorized",
    404: "not_found",
    429: "rate_limited",
}


class ProviderResponse(NamedTuple):
    """A raw HTTP answer: the status and the body."""

    status: int
    body: bytes


def _send_https(
    host: str, path: str, body: bytes, headers: dict[str, str], timeout: float
) -> ProviderResponse:
    """POST a body to a host over HTTPS.

    Args:
        host: The host name. HTTPS only; there is no scheme to choose.
        path: The request path.
        body: The encoded request body.
        headers: The request headers.
        timeout: Seconds to wait for the provider.

    Returns:
        The status and body of the answer.
    """
    connection = http.client.HTTPSConnection(host, timeout=timeout)
    try:
        connection.request("POST", path, body=body, headers=headers)
        response = connection.getresponse()
        return ProviderResponse(response.status, response.read())
    finally:
        connection.close()


def _code_for(status: int) -> str:
    """Name a failed HTTP status with a stable code.

    Args:
        status: The HTTP status.

    Returns:
        A code safe to store and show.
    """
    if status >= _SERVER_ERROR:
        return "unavailable"
    return _STATUS_CODES.get(status, "unexpected_status")


class HttpTransport:
    """Creates shopping list links by calling Instacart.

    Args:
        api_key: The deployment's Instacart API key.
        host: One of the two documented hosts.
        send: Performs the HTTPS POST; tests pass a fake.
        timeout: Seconds to wait for the provider.
    """

    def __init__(
        self,
        api_key: str,
        host: str,
        send: Callable[[str, str, bytes, dict[str, str], float], ProviderResponse]
        | None = None,
        timeout: float = TIMEOUT_SECONDS,
    ) -> None:
        """Bind the transport to a key and a host.

        Args:
            api_key: The deployment's Instacart API key.
            host: One of the two documented hosts.
            send: Performs the HTTPS POST. Defaults to a real connection.
            timeout: Seconds to wait for the provider.

        Raises:
            ValueError: If the key is empty or the host is not one of
                Instacart's two documented hosts.
        """
        if not api_key:
            msg = "An Instacart API key is required."
            raise ValueError(msg)
        if host not in HOSTS:
            msg = "The host must be Instacart's development or production host."
            raise ValueError(msg)
        self._api_key = api_key
        self._host = host
        self._send = send
        self._timeout = timeout

    def __repr__(self) -> str:
        """Describe the transport without its key.

        Returns:
            The host, and a marker where the key would be.
        """
        return f"HttpTransport(host={self._host!r}, api_key=[redacted])"

    def create_products_link(
        self, request: ProductsLinkRequest
    ) -> ProductsLinkResponse:
        """Create the link.

        Args:
            request: The typed request body.

        Returns:
            The provider's response.

        Raises:
            TransportError: With ``unreachable`` (worth retrying) when the
                provider can't be reached, ``invalid_response`` when its
                answer isn't JSON, or a code for its HTTP status.
            pydantic.ValidationError: If the answer has no HTTPS link.
        """
        send = self._send or _send_https
        headers = {
            "Authorization": f"Bearer {self._api_key}",
            "Content-Type": "application/json",
            "Accept": "application/json",
        }
        body = json.dumps(request.body()).encode()
        try:
            response = send(
                self._host, PRODUCTS_LINK_PATH, body, headers, self._timeout
            )
        except (OSError, http.client.HTTPException):
            # Raised bare: the original error is about the connection, and
            # nothing from the request should travel with this one.
            raise TransportError("unreachable", transient=True) from None
        if response.status != _OK:
            raise TransportError(_code_for(response.status), response.status)
        try:
            answer = json.loads(response.body)
        except ValueError:
            raise TransportError("invalid_response") from None
        link = answer.get("products_link_url", "") if isinstance(answer, dict) else ""
        return ProductsLinkResponse(
            products_link_url=link if isinstance(link, str) else ""
        )
