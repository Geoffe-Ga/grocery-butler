"""The Instacart adapter: a plan becomes a shopping list link (ADR 0014).

This is the tracer skeleton (#146). It sends each plan line by name only.
Quantities, units and preferences arrive with translation (#147); the real
network transport and approval binding arrive with link creation (#148).

What it already guarantees:

- It claims one capability, ``shopping_list_link``.
- It enforces no hard constraint, so it reports every one as unenforced.
- A link is a handoff. Nothing here says, or can say, that anything was
  bought.
"""

from __future__ import annotations

import datetime as dt
from typing import TYPE_CHECKING

from pydantic import ValidationError

from grocery_butler.fulfillment.base import (
    AdapterDescription,
    Authorization,
    Capability,
    FulfillmentAdapter,
    HandoffResult,
    HandoffStatus,
    unenforced_constraints,
)
from grocery_butler.fulfillment.instacart.models import (
    MAX_LINK_DAYS,
    LineItem,
    ProductsLinkRequest,
)
from grocery_butler.fulfillment.instacart.transport import TransportError
from grocery_butler.inventory_ledger import utc_now
from grocery_butler.retries import is_transient

if TYPE_CHECKING:
    from collections.abc import Callable

    from grocery_butler.fulfillment.instacart.transport import Transport
    from grocery_butler.models import ShoppingPlan

ADAPTER_NAME = "instacart"
DEFAULT_LINK_DAYS = 14

_HUMAN_ACTION = (
    "Open the link to review the list in Instacart. Pick a store, check each"
    " match, and check out there yourself."
)
_NOTHING_TO_HAND_OFF = "The plan has no lines, so there is nothing to hand off."
_BAD_RESPONSE = "invalid_response"


class InstacartAdapter(FulfillmentAdapter):
    """Creates an Instacart shopping list link for a plan version.

    Args:
        transport: How the provider is reached. Tests pass a fake.
        link_days: How many days the link stays valid, 1 to 365.
        clock: Returns the current UTC time; injectable for tests.
    """

    def __init__(
        self,
        transport: Transport,
        link_days: int = DEFAULT_LINK_DAYS,
        clock: Callable[[], dt.datetime] = utc_now,
    ) -> None:
        """Bind the adapter to a transport.

        Args:
            transport: How the provider is reached.
            link_days: How many days the link stays valid.
            clock: Returns the current UTC time.

        Raises:
            ValueError: If ``link_days`` is outside 1 to 365.
        """
        if not 1 <= link_days <= MAX_LINK_DAYS:
            msg = f"link_days must be between 1 and {MAX_LINK_DAYS}, got {link_days}."
            raise ValueError(msg)
        self._transport = transport
        self._link_days = link_days
        self._clock = clock

    def describe(self) -> AdapterDescription:
        """Declare what this adapter is and can do.

        Returns:
            The adapter's description: one capability, provider-program
            authorization, and no hard constraint it can enforce.
        """
        return AdapterDescription(
            name=ADAPTER_NAME,
            display_name="Instacart shopping list",
            capabilities=frozenset({Capability.SHOPPING_LIST_LINK}),
            authorization=Authorization.PROVIDER_PROGRAM,
            data_freshness=(
                "A link is created on request. Nothing is read back from"
                " Instacart: no cart, order, price or availability."
            ),
            human_required=True,
        )

    def build_request(self, plan: ShoppingPlan) -> ProductsLinkRequest | None:
        """Turn a plan into the request for its link.

        Args:
            plan: The stored plan version.

        Returns:
            The request, or None when the plan has no lines.
        """
        if not plan.lines:
            return None
        return ProductsLinkRequest(
            title=f"Grocery Butler shopping plan {plan.version}",
            line_items=[LineItem(name=line.display_name) for line in plan.lines],
            expires_in=self._link_days,
        )

    def create_handoff(self, plan: ShoppingPlan) -> HandoffResult:
        """Create a shopping list link for a stored plan version.

        Args:
            plan: The stored plan version to hand off.

        Returns:
            A created handoff with its link and expiry, or a failed one
            with a stable reason. Never a purchase.

        Raises:
            TransportError: If the provider failed in a way worth retrying
                (a timeout, a rate limit, a server error). The handoff
                runner retries those; any other failure is returned as a
                failed handoff.
        """
        request = self.build_request(plan)
        if request is None:
            return self._failed(plan, _NOTHING_TO_HAND_OFF)
        try:
            response = self._transport.create_products_link(request)
        except TransportError as err:
            if is_transient(err):
                raise
            return self._failed(plan, err.code)
        except ValidationError:
            return self._failed(plan, _BAD_RESPONSE)
        return HandoffResult(
            adapter=ADAPTER_NAME,
            status=HandoffStatus.CREATED,
            plan_version=plan.version,
            plan_content_hash=plan.content_hash,
            human_action=_HUMAN_ACTION,
            external_url=response.products_link_url,
            expires_at=self._clock() + dt.timedelta(days=self._link_days),
            unenforced_constraints=unenforced_constraints(self.describe(), plan),
        )

    def _failed(self, plan: ShoppingPlan, detail: str) -> HandoffResult:
        """Build a failed handoff.

        Args:
            plan: The plan that was being handed off.
            detail: A stable reason, safe to show.

        Returns:
            The failed result. It still lists the unenforced constraints.
        """
        return HandoffResult(
            adapter=ADAPTER_NAME,
            status=HandoffStatus.FAILED,
            plan_version=plan.version,
            plan_content_hash=plan.content_hash,
            human_action=(
                "No Instacart link was created. Use the list export instead,"
                " or try again."
            ),
            detail=detail,
            unenforced_constraints=unenforced_constraints(self.describe(), plan),
        )
