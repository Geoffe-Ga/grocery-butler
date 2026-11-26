"""The Instacart adapter: a plan becomes a shopping list link (ADR 0014).

Each plan line is translated into a documented line item
(:mod:`.translate`), and everything the translation can't carry comes back
as an issue. The real network transport and the approval binding arrive
with link creation (#148).

What it guarantees:

- It claims one capability, ``shopping_list_link``.
- It enforces no hard constraint, so it reports every one as unenforced,
  and it refuses to send a plan that has any until a person has
  acknowledged them.
- A link is a handoff. Nothing here says, or can say, that anything was
  bought.
"""

from __future__ import annotations

import datetime as dt
import json
from typing import TYPE_CHECKING, Any

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
from grocery_butler.fulfillment.instacart.models import MAX_LINK_DAYS
from grocery_butler.fulfillment.instacart.translate import translate
from grocery_butler.fulfillment.instacart.transport import TransportError
from grocery_butler.inventory_ledger import utc_now
from grocery_butler.retries import is_transient

if TYPE_CHECKING:
    from collections.abc import Callable

    from grocery_butler.fulfillment.instacart.translate import TranslationResult
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
NEEDS_REVIEW = "hard_constraints_need_review"
ISSUES_ARTIFACT = "translation_issues"
_DEFAULT_FAILED_ACTION = (
    "No Instacart link was created. Use the list export instead, or try again."
)
_FAILED_ACTIONS = {
    NEEDS_REVIEW: (
        "No Instacart link was created. Instacart can't enforce some of this"
        " plan's hard constraints. Review them, then confirm you want to"
        " continue."
    ),
}


def _issue_artifacts(translated: TranslationResult) -> dict[str, str]:
    """Carry the translation issues on a handoff result.

    Args:
        translated: The translation.

    Returns:
        One artifact: the issues as a JSON array, in plan order.
    """
    issues = [issue.as_dict() for issue in translated.issues]
    return {ISSUES_ARTIFACT: json.dumps(issues, sort_keys=True)}


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

    def translate(self, plan: ShoppingPlan) -> TranslationResult:
        """Translate a plan into the request for its link, with every loss.

        Args:
            plan: The stored plan version.

        Returns:
            The request and the issues a person should see before the
            handoff.
        """
        return translate(plan, link_days=self._link_days)

    def preview_issues(self, plan: ShoppingPlan) -> tuple[dict[str, Any], ...]:
        """List what the link for this plan would leave out.

        Args:
            plan: The stored plan version.

        Returns:
            The translation issues as plain data, in plan order.
        """
        return tuple(issue.as_dict() for issue in self.translate(plan).issues)

    def create_handoff(
        self, plan: ShoppingPlan, *, constraints_acknowledged: bool = False
    ) -> HandoffResult:
        """Create a shopping list link for a stored plan version.

        Args:
            plan: The stored plan version to hand off.
            constraints_acknowledged: Whether a person has reviewed the
                hard constraints Instacart can't enforce. Without it, a
                plan that has any is refused and nothing is sent.

        Returns:
            A created handoff with its link and expiry, or a failed one
            with a stable reason. Never a purchase. Either way the
            translation issues are in ``artifacts["translation_issues"]``.

        Raises:
            TransportError: If the provider failed in a way worth retrying
                (a timeout, a rate limit, a server error). The handoff
                runner retries those; any other failure is returned as a
                failed handoff.
        """
        translated = self.translate(plan)
        if translated.request is None:
            return self._failed(plan, _NOTHING_TO_HAND_OFF, translated)
        if translated.needs_acknowledgement and not constraints_acknowledged:
            return self._failed(plan, NEEDS_REVIEW, translated)
        try:
            response = self._transport.create_products_link(translated.request)
        except TransportError as err:
            if is_transient(err):
                raise
            return self._failed(plan, err.code, translated)
        except ValidationError:
            return self._failed(plan, _BAD_RESPONSE, translated)
        return HandoffResult(
            adapter=ADAPTER_NAME,
            status=HandoffStatus.CREATED,
            plan_version=plan.version,
            plan_content_hash=plan.content_hash,
            human_action=_HUMAN_ACTION,
            artifacts=_issue_artifacts(translated),
            external_url=response.products_link_url,
            expires_at=self._clock() + dt.timedelta(days=self._link_days),
            unenforced_constraints=unenforced_constraints(self.describe(), plan),
        )

    def _failed(
        self, plan: ShoppingPlan, detail: str, translated: TranslationResult
    ) -> HandoffResult:
        """Build a failed handoff.

        Args:
            plan: The plan that was being handed off.
            detail: A stable reason, safe to show.
            translated: The translation, for its issues.

        Returns:
            The failed result. It still lists the unenforced constraints.
        """
        return HandoffResult(
            adapter=ADAPTER_NAME,
            status=HandoffStatus.FAILED,
            plan_version=plan.version,
            plan_content_hash=plan.content_hash,
            human_action=_FAILED_ACTIONS.get(detail, _DEFAULT_FAILED_ACTION),
            artifacts=_issue_artifacts(translated),
            detail=detail,
            unenforced_constraints=unenforced_constraints(self.describe(), plan),
        )
