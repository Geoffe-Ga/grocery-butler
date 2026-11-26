"""The registry of fulfillment adapters.

Callers ask the registry for an adapter by name. Nothing outside this
package constructs a concrete adapter, so adding or removing one never
touches core modules.
"""

from __future__ import annotations

import os
from typing import TYPE_CHECKING

from grocery_butler.fulfillment.export import ExportAdapter
from grocery_butler.fulfillment.instacart.adapter import InstacartAdapter
from grocery_butler.fulfillment.instacart.http_transport import (
    DEVELOPMENT_HOST,
    PRODUCTION_HOST,
    HttpTransport,
)
from grocery_butler.fulfillment.instacart.transport import (
    NotConfiguredTransport,
    StubTransport,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from grocery_butler.fulfillment.base import FulfillmentAdapter
    from grocery_butler.fulfillment.instacart.transport import Transport

INSTACART_ENABLED_ENV = "INSTACART_ENABLED"
INSTACART_ENV_VARIABLE = "INSTACART_ENV"
INSTACART_KEY_VARIABLE = "INSTACART_API_KEY"
STUB = "stub"
_HOSTS = {"development": DEVELOPMENT_HOST, "production": PRODUCTION_HOST}
_TRUTHY = frozenset({"1", "true", "yes"})


class AdapterNotFoundError(LookupError):
    """Raised when no adapter is registered under a name."""

    def __init__(self, name: str, known: list[str]) -> None:
        """Name the missing adapter and list the ones that exist.

        Args:
            name: The name that was asked for.
            known: The names that are registered.
        """
        listed = ", ".join(known) if known else "none"
        super().__init__(
            f'No fulfillment adapter is named "{name}". Available: {listed}.'
        )
        self.name = name


class AdapterRegistry:
    """Fulfillment adapters by name."""

    def __init__(self) -> None:
        """Start with no adapters."""
        self._adapters: dict[str, FulfillmentAdapter] = {}

    def register(self, adapter: FulfillmentAdapter) -> None:
        """Add an adapter under the name it declares.

        Args:
            adapter: The adapter to register.

        Raises:
            ValueError: If an adapter with that name is already registered.
        """
        name = adapter.describe().name
        if name in self._adapters:
            msg = f'A fulfillment adapter named "{name}" is already registered.'
            raise ValueError(msg)
        self._adapters[name] = adapter

    def get(self, name: str) -> FulfillmentAdapter:
        """Return the adapter registered under a name.

        Args:
            name: The adapter's name.

        Returns:
            The adapter.

        Raises:
            AdapterNotFoundError: If no adapter has that name.
        """
        if name not in self._adapters:
            raise AdapterNotFoundError(name, self.names())
        return self._adapters[name]

    def names(self) -> list[str]:
        """List the registered adapter names, sorted.

        Returns:
            The names.
        """
        return sorted(self._adapters)

    def adapters(self) -> list[FulfillmentAdapter]:
        """List the registered adapters, sorted by name.

        Returns:
            The adapters.
        """
        return [self._adapters[name] for name in self.names()]


def instacart_enabled(environ: Mapping[str, str]) -> bool:
    """Say whether this deployment turned the Instacart adapter on.

    Args:
        environ: The environment to read.

    Returns:
        True only when ``INSTACART_ENABLED`` is ``1``, ``true`` or ``yes``.
    """
    return environ.get(INSTACART_ENABLED_ENV, "").strip().lower() in _TRUTHY


def instacart_transport(environ: Mapping[str, str]) -> Transport:
    """Choose how the Instacart adapter reaches Instacart.

    ``INSTACART_ENV`` decides:

    - unset or ``stub``: a fake link, nothing contacted.
    - ``development`` or ``production``: the real host, with the key from
      ``INSTACART_API_KEY``.

    Anything else, or a real environment without a key, gives a transport
    that refuses every call with ``not_configured``. It never falls back
    to a different host.

    Args:
        environ: The environment to read.

    Returns:
        The transport.
    """
    chosen = environ.get(INSTACART_ENV_VARIABLE, STUB).strip().lower() or STUB
    if chosen == STUB:
        return StubTransport()
    host = _HOSTS.get(chosen)
    key = environ.get(INSTACART_KEY_VARIABLE, "")
    if host is None or not key:
        return NotConfiguredTransport()
    return HttpTransport(key, host)


def default_registry(environ: Mapping[str, str] | None = None) -> AdapterRegistry:
    """Build the registry of adapters this deployment offers.

    The universal list export is always there. The Instacart adapter
    (ADR 0014) is off unless ``INSTACART_ENABLED`` is set; how it reaches
    Instacart is decided by :func:`instacart_transport`.

    Args:
        environ: The environment to read. Defaults to the process's own.

    Returns:
        The registry.
    """
    env = os.environ if environ is None else environ
    registry = AdapterRegistry()
    registry.register(ExportAdapter())
    if instacart_enabled(env):
        registry.register(InstacartAdapter(instacart_transport(env)))
    return registry
