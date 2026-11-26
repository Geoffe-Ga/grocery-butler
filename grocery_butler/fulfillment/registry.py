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
from grocery_butler.fulfillment.instacart.transport import StubTransport

if TYPE_CHECKING:
    from collections.abc import Mapping

    from grocery_butler.fulfillment.base import FulfillmentAdapter

INSTACART_ENABLED_ENV = "INSTACART_ENABLED"
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


def default_registry(environ: Mapping[str, str] | None = None) -> AdapterRegistry:
    """Build the registry of adapters this deployment offers.

    The universal list export is always there. The Instacart adapter
    (ADR 0014) is off unless ``INSTACART_ENABLED`` is set, and today it
    runs on a stub transport that returns a fake link and reaches no
    network.

    Args:
        environ: The environment to read. Defaults to the process's own.

    Returns:
        The registry.
    """
    registry = AdapterRegistry()
    registry.register(ExportAdapter())
    if instacart_enabled(os.environ if environ is None else environ):
        registry.register(InstacartAdapter(StubTransport()))
    return registry
