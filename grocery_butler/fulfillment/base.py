"""The retailer-neutral fulfillment adapter interface (ADR 0001, P1.11).

An adapter moves an approved shopping plan toward a purchase the household
completes somewhere else. It declares what it can do, how it is authorized,
how fresh its data is and whether a person has to act. Nothing here knows
about any retailer.

Two rules hold for every adapter:

- A handoff is not an order. A result says a handoff was created or failed.
  It never says anything was purchased.
- A person completes the purchase. Every adapter requires a human to act.
- A hard constraint is enforced or reported, never dropped (ADR 0012). An
  adapter declares the kinds it can enforce by itself. Every other hard
  constraint on the plan is listed on the result as unenforced, for a
  person to check.
"""

from __future__ import annotations

import abc
import enum
from dataclasses import dataclass, field
from typing import TYPE_CHECKING

if TYPE_CHECKING:
    import datetime as dt

    from grocery_butler.models import HardConstraintKind, ShoppingPlan


class Capability(enum.StrEnum):
    """What an adapter can do. An adapter declares only what it supports."""

    EXPORT_LIST = "export_list"
    OPEN_SEARCH = "open_search"
    SHOPPING_LIST_LINK = "shopping_list_link"
    DRAFT_CART = "draft_cart"
    DEEP_LINK_CART = "deep_link_cart"
    READ_CART = "read_cart"
    READ_ORDER = "read_order"
    IMPORT_RECEIPT = "import_receipt"


class Authorization(enum.StrEnum):
    """How an adapter is allowed to act.

    ``PROVIDER_PROGRAM`` means access the provider grants through its own
    developer program, held by this deployment and never by the household.
    There is deliberately no member for stored retailer credentials: no
    adapter may hold a household's retailer password (ADR 0001).
    """

    NONE = "none"
    PROVIDER_PROGRAM = "provider_program"
    USER_SESSION = "user_session"


class HandoffStatus(enum.StrEnum):
    """What became of a handoff attempt. There is no "purchased"."""

    CREATED = "created"
    FAILED = "failed"


@dataclass(frozen=True)
class AdapterDescription:
    """What an adapter declares about itself.

    ``data_freshness`` says, in a sentence, how current anything the
    adapter reports about the outside world is. ``enforces`` lists the hard
    constraint kinds the adapter can guarantee by itself; anything else it
    must report as unenforced.
    """

    name: str
    display_name: str
    capabilities: frozenset[Capability]
    authorization: Authorization
    data_freshness: str
    human_required: bool
    enforces: frozenset[HardConstraintKind] = frozenset()


@dataclass(frozen=True)
class UnenforcedConstraint:
    """A hard constraint an adapter could not guarantee for one plan line."""

    item_id: int
    display_name: str
    kind: HardConstraintKind
    value: str


@dataclass(frozen=True)
class HandoffResult:
    """The outcome of handing a plan to an adapter.

    ``artifacts`` maps a format name to content the household can use (an
    export's text, say). ``external_url`` is where the household continues,
    for adapters that create one. ``human_action`` tells the person what to
    do next. ``unenforced_constraints`` lists every hard constraint on the
    plan that the adapter could not guarantee; a person has to check those.
    """

    adapter: str
    status: HandoffStatus
    plan_version: int
    plan_content_hash: str
    human_action: str
    artifacts: dict[str, str] = field(default_factory=dict)
    external_url: str | None = None
    expires_at: dt.datetime | None = None
    detail: str = ""
    unenforced_constraints: tuple[UnenforcedConstraint, ...] = ()


class UnsupportedCapabilityError(ValueError):
    """Raised when an adapter is asked for something it didn't declare."""

    def __init__(self, adapter: str, capability: Capability) -> None:
        """Name the adapter and the capability it lacks.

        Args:
            adapter: The adapter's name.
            capability: The capability that was asked for.
        """
        super().__init__(
            f'The "{adapter}" adapter does not support {capability.value}.'
        )
        self.adapter = adapter
        self.capability = capability


def unenforced_constraints(
    description: AdapterDescription, plan: ShoppingPlan
) -> tuple[UnenforcedConstraint, ...]:
    """List the plan's hard constraints an adapter can't guarantee.

    Every adapter builds its result's ``unenforced_constraints`` with this,
    so a constraint it does not enforce is always reported.

    Args:
        description: The adapter's own description.
        plan: The plan being handed off.

    Returns:
        One entry per line and hard constraint whose kind the adapter does
        not enforce, in plan order.
    """
    return tuple(
        UnenforcedConstraint(
            item_id=line.item_id,
            display_name=line.display_name,
            kind=constraint.kind,
            value=constraint.value,
        )
        for line in plan.lines
        for constraint in line.hard_constraints
        if constraint.kind not in description.enforces
    )


class FulfillmentAdapter(abc.ABC):
    """A way to hand an approved shopping plan off.

    Implementations must not import retailer-specific types into core
    modules, must never report a purchase, and must always need a person
    to finish.
    """

    @abc.abstractmethod
    def describe(self) -> AdapterDescription:
        """Declare what this adapter is and can do.

        Returns:
            The adapter's description.
        """

    @abc.abstractmethod
    def create_handoff(self, plan: ShoppingPlan) -> HandoffResult:
        """Hand a stored plan version off.

        Args:
            plan: The stored plan version to hand off.

        Returns:
            What was created, or how it failed. Never a purchase.
        """

    def require(self, capability: Capability) -> None:
        """Check that this adapter declared a capability.

        Args:
            capability: The capability a caller is about to rely on.

        Raises:
            UnsupportedCapabilityError: If the adapter didn't declare it.
        """
        description = self.describe()
        if capability not in description.capabilities:
            raise UnsupportedCapabilityError(description.name, capability)
