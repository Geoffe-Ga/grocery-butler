"""How the Instacart adapter reaches Instacart, or pretends to (ADR 0014).

The adapter is given a transport and never makes a network call itself.
This module has the interface and two transports that touch no network:

- :class:`FakeTransport` for tests: it returns or raises what it was told
  to and records every request.
- :class:`StubTransport` for demos: it returns a deterministic, obviously
  fake link.

The transport that really calls Instacart arrives with link creation
(#148). Until then nothing in this package can reach a network.
"""

from __future__ import annotations

import hashlib
import json
from typing import Protocol

from grocery_butler.fulfillment.instacart.models import (
    ProductsLinkRequest,
    ProductsLinkResponse,
)

STUB_LINK_PREFIX = "https://example.invalid/instacart-stub/"
_STUB_DIGEST_LENGTH = 16


class TransportError(Exception):
    """A request to the provider failed.

    ``code`` is a stable word for the failure. ``status_code`` is the HTTP
    status when there was one, and ``transient`` marks a failure with no
    status that is still worth retrying (the provider could not be
    reached). The retry helper reads both.
    """

    def __init__(
        self, code: str, status_code: int | None = None, *, transient: bool = False
    ) -> None:
        """Describe the failure without any provider internals.

        Args:
            code: A stable failure code, e.g. ``rate_limited``.
            status_code: The HTTP status, if there was a response.
            transient: Whether trying again could help although there was
                no response.
        """
        self.code = code
        self.status_code = status_code
        self.transient = transient
        super().__init__(f"The provider request failed: {code}.")


class Transport(Protocol):
    """Creates a shopping list link from a request."""

    def create_products_link(
        self, request: ProductsLinkRequest
    ) -> ProductsLinkResponse:
        """Create the link.

        Args:
            request: The typed request body.

        Returns:
            The provider's response.

        Raises:
            TransportError: If the provider could not be reached or
                refused the request.
        """
        ...  # pragma: no cover


class NotConfiguredTransport:
    """A transport for a deployment that enabled Instacart without a key.

    It never sends anything. Every call fails with ``not_configured``, so
    the household is told to use the list export.
    """

    def create_products_link(
        self, request: ProductsLinkRequest
    ) -> ProductsLinkResponse:
        """Refuse, without contacting anything.

        Args:
            request: The typed request body. Not sent.

        Raises:
            TransportError: Always, with ``not_configured``.
        """
        del request
        raise TransportError("not_configured")


class FakeTransport:
    """A transport for tests: scripted outcomes, recorded requests."""

    def __init__(self, *outcomes: str | TransportError) -> None:
        """Queue the outcomes, in order. The last one repeats.

        Args:
            *outcomes: Each is a link to return or an error to raise.
        """
        self._outcomes = list(outcomes)
        self.requests: list[ProductsLinkRequest] = []

    @classmethod
    def ok(cls, link: str) -> FakeTransport:
        """Build a transport that always returns one link.

        Args:
            link: The link to return.

        Returns:
            The transport.
        """
        return cls(link)

    @classmethod
    def failing(cls, code: str, status_code: int | None = None) -> FakeTransport:
        """Build a transport that always fails the same way.

        Args:
            code: The failure code.
            status_code: The HTTP status to report, if any.

        Returns:
            The transport.
        """
        return cls(TransportError(code, status_code))

    def create_products_link(
        self, request: ProductsLinkRequest
    ) -> ProductsLinkResponse:
        """Record the request and play the next outcome.

        Args:
            request: The typed request body.

        Returns:
            The scripted response.

        Raises:
            TransportError: If the next outcome is an error.
        """
        self.requests.append(request)
        outcome = self._outcomes[0]
        if len(self._outcomes) > 1:
            self._outcomes.pop(0)
        if isinstance(outcome, TransportError):
            raise outcome
        return ProductsLinkResponse(products_link_url=outcome)


class StubTransport:
    """A transport for demos: a fake link that depends only on the request.

    The same plan version always gives the same link, and the link's host
    (``example.invalid``) can never resolve.
    """

    def create_products_link(
        self, request: ProductsLinkRequest
    ) -> ProductsLinkResponse:
        """Return a deterministic fake link.

        Args:
            request: The typed request body.

        Returns:
            A response whose link is derived from the request's content.
        """
        canonical = json.dumps(request.body(), sort_keys=True, separators=(",", ":"))
        digest = hashlib.sha256(canonical.encode()).hexdigest()[:_STUB_DIGEST_LENGTH]
        return ProductsLinkResponse(products_link_url=f"{STUB_LINK_PREFIX}{digest}")
