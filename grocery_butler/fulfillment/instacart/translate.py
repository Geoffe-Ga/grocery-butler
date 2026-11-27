"""Translate a shopping plan into Instacart line items (ADR 0014, #147).

The translation is lossy, and every loss is reported. A unit Instacart
doesn't list, amounts that can't be combined, a rule Instacart can't
enforce: each becomes a :class:`TranslationIssue` beside the request, so a
person sees what the link will and won't carry.

Rules:

- **Never guess.** An amount is sent only in a unit Instacart documents. A
  UPC is sent only when the caller says it is a verified household fact.
- **Never weaken a hard constraint.** None is ever sent as a filter, which
  Instacart may ignore. Each is reported, and the result then needs a
  person's acknowledgement before a link may be created.
- **Pure.** The same plan and arguments always give the same request and
  the same issues. No I/O.

The unit map was built from Instacart's "Units of measurement" page as read
on 2026-11-26. A unit not on it is reported, never approximated.
"""

from __future__ import annotations

from dataclasses import dataclass
from enum import StrEnum
from typing import TYPE_CHECKING, Any

from grocery_butler.fulfillment.instacart.models import (
    Filters,
    LineItem,
    Measurement,
    ProductsLinkRequest,
)
from grocery_butler.models import Unit
from grocery_butler.plan_preferences import (
    AVOID_BRANDS,
    GENERIC,
    ORGANIC,
    PREFERRED_BRAND,
    PREFERRED_SIZE,
    VERIFIED_UPC,
    is_product_code,
    unfiltered_phrases,
)

if TYPE_CHECKING:
    from collections.abc import Mapping

    from grocery_butler.models import PlanAmount, PlanLine, ShoppingPlan

# Grocery Butler unit -> (Instacart unit, multiplier). A dozen is exactly
# twelve each; every other entry is the same unit under Instacart's name.
UNIT_MAP: dict[Unit, tuple[str, float]] = {
    Unit.TSP: ("teaspoon", 1),
    Unit.TBSP: ("tablespoon", 1),
    Unit.CUP: ("cup", 1),
    Unit.FL_OZ: ("fl oz", 1),
    Unit.ML: ("ml", 1),
    Unit.L: ("liter", 1),
    Unit.GAL: ("gallon", 1),
    Unit.PINT: ("pint", 1),
    Unit.QUART: ("quart", 1),
    Unit.OZ: ("oz", 1),
    Unit.LB: ("lb", 1),
    Unit.G: ("gram", 1),
    Unit.KG: ("kilogram", 1),
    Unit.EACH: ("each", 1),
    Unit.DOZEN: ("each", 12),
    Unit.BUNCH: ("bunch", 1),
    Unit.HEAD: ("head", 1),
    Unit.CAN: ("can", 1),
    Unit.PACKAGE: ("package", 1),
}

ORGANIC_FILTER = "ORGANIC"

_BRAND = PREFERRED_BRAND
_SIZE = PREFERRED_SIZE
_ORGANIC = ORGANIC
_KNOWN_PREFERENCES = frozenset(
    {_BRAND, _SIZE, _ORGANIC, GENERIC, AVOID_BRANDS, VERIFIED_UPC}
)


class IssueKind(StrEnum):
    """What the translation could not carry across."""

    UNSUPPORTED_UNIT = "unsupported_unit"
    AMBIGUOUS_MAPPING = "ambiguous_mapping"
    PREFERENCE_NOT_APPLIED = "preference_not_applied"
    HARD_CONSTRAINT_UNREPRESENTABLE = "hard_constraint_unrepresentable"
    FILTER_DROPPED_BY_UPC = "filter_dropped_by_upc"
    INVALID_UPC = "invalid_upc"


@dataclass(frozen=True)
class TranslationIssue:
    """One thing a plan line asked for that the link will not carry."""

    kind: IssueKind
    item_id: int
    display_name: str
    detail: str

    def as_dict(self) -> dict[str, Any]:
        """Render the issue as plain data.

        Returns:
            The issue's fields, with the kind as its text value.
        """
        return {
            "kind": self.kind.value,
            "item_id": self.item_id,
            "display_name": self.display_name,
            "detail": self.detail,
        }


@dataclass(frozen=True)
class TranslationResult:
    """A request and everything it leaves out.

    ``request`` is None when the plan has no lines.
    """

    request: ProductsLinkRequest | None
    issues: tuple[TranslationIssue, ...]

    @property
    def needs_acknowledgement(self) -> bool:
        """Say whether a person must review before a link is created.

        Returns:
            True when any hard constraint could not be expressed.
        """
        return any(
            issue.kind is IssueKind.HARD_CONSTRAINT_UNREPRESENTABLE
            for issue in self.issues
        )


def _amount_text(amount: PlanAmount) -> str:
    """Word an amount for a person.

    Args:
        amount: The amount.

    Returns:
        For example ``2 lb``.
    """
    return f"{amount.quantity:g} {amount.unit.value}"


def _measurements(
    line: PlanLine,
) -> tuple[list[Measurement], list[tuple[IssueKind, str]]]:
    """Translate a line's amounts.

    Instacart reads several measurements on one line as alternatives for
    the same amount, so amounts in more than one unit are never sent
    together.

    Args:
        line: The plan line.

    Returns:
        The measurements to send, and what could not be sent.
    """
    if not line.amounts:
        return [], []
    stated = ", ".join(_amount_text(amount) for amount in line.amounts)
    if len(line.amounts) > 1:
        detail = (
            f"The plan wants {stated}. Instacart can't add amounts in different"
            " units, so no amount was sent."
        )
        return [], [(IssueKind.AMBIGUOUS_MAPPING, detail)]
    amount = line.amounts[0]
    # The plan model doesn't forbid a zero amount, and Instacart answers one
    # with a 400, so it is caught here rather than sent.
    if amount.quantity <= 0:
        detail = f"{stated} is not an amount that can be bought; none was sent."
        return [], [(IssueKind.AMBIGUOUS_MAPPING, detail)]
    mapped = UNIT_MAP.get(amount.unit)
    if mapped is None:
        detail = (
            f"Instacart has no unit for {stated}, so no amount was sent."
            " Check the quantity in Instacart."
        )
        return [], [(IssueKind.UNSUPPORTED_UNIT, detail)]
    unit, multiplier = mapped
    return [Measurement(quantity=amount.quantity * multiplier, unit=unit)], []


def _text(value: Any) -> str:
    """Read a preference value as trimmed text.

    Args:
        value: The stored value.

    Returns:
        The text, or an empty string when there is none.
    """
    return value.strip() if isinstance(value, str) else ""


def _preferences(line: PlanLine) -> tuple[Filters | None, list[str], list[str]]:
    """Read a line's soft preferences.

    Args:
        line: The plan line.

    Returns:
        The filters to send (or None), a description of each filter for
        reporting, and the names of preferences this translation doesn't
        understand.
    """
    brand = _text(line.constraints.get(_BRAND))
    organic = line.constraints.get(_ORGANIC) is True
    described = ([f"brand {brand}"] if brand else []) + ([_ORGANIC] if organic else [])
    unknown = sorted(
        key
        for key, value in line.constraints.items()
        if key not in _KNOWN_PREFERENCES and value
    )
    if not described:
        return None, [], unknown
    filters = Filters(
        brand_filters=[brand] if brand else [],
        health_filters=[ORGANIC_FILTER] if organic else [],
    )
    return filters, described, unknown


def _upc(
    line: PlanLine, verified_upcs: Mapping[int, str], used: set[str]
) -> tuple[str | None, str | None]:
    """Decide whether a line's verified UPC can be sent.

    Args:
        line: The plan line.
        verified_upcs: Verified UPCs by item id.
        used: UPCs already sent on earlier lines.

    Returns:
        The UPC to send (or None), and a reason when one was refused.
    """
    if line.item_id not in verified_upcs:
        return None, None
    upc = verified_upcs[line.item_id]
    if not is_product_code(upc):
        return None, "The stored UPC is not 12 or 14 digits, so it was not sent."
    if upc in used:
        return None, (
            "Another line already uses this UPC. Instacart rejects duplicates,"
            " so this line was sent by name."
        )
    return upc, None


def _display_text(line: PlanLine) -> str:
    """Word what the shopper sees for a line.

    Instacart has no field for a preferred size, so it is shown here.

    Args:
        line: The plan line.

    Returns:
        The item's name, with the preferred size in brackets if there is one.
    """
    size = _text(line.constraints.get(_SIZE))
    return f"{line.display_name} ({size})" if size else line.display_name


def _translate_line(
    line: PlanLine, verified_upcs: Mapping[int, str], used_upcs: set[str]
) -> tuple[LineItem, list[tuple[IssueKind, str]]]:
    """Translate one plan line.

    Args:
        line: The plan line.
        verified_upcs: Verified UPCs by item id.
        used_upcs: UPCs already sent; updated when this line sends one.

    Returns:
        The line item, and what it could not carry.
    """
    measurements, found = _measurements(line)
    filters, described, unknown = _preferences(line)
    upc, refused = _upc(line, verified_upcs, used_upcs)
    if refused is not None:
        found.append((IssueKind.INVALID_UPC, refused))
    if upc is not None:
        used_upcs.add(upc)
        if described:
            detail = (
                f"Sent by UPC, so Instacart ignores these: {', '.join(described)}."
                " Check the match in Instacart."
            )
            found.append((IssueKind.FILTER_DROPPED_BY_UPC, detail))
        filters = None
    found.extend(
        (
            IssueKind.AMBIGUOUS_MAPPING,
            f'The preference "{name}" has no Instacart equivalent and was not sent.',
        )
        for name in unknown
    )
    # A store brand and a brand to avoid have no filter. They are left out
    # of the request and reported, so the handoff can say once what to check
    # (decided on 2026-11-26; issue #215).
    unfiltered = unfiltered_phrases(line.constraints)
    if unfiltered:
        found.append((IssueKind.PREFERENCE_NOT_APPLIED, "; ".join(unfiltered)))
    found.extend(
        (
            IssueKind.HARD_CONSTRAINT_UNREPRESENTABLE,
            f"Instacart can't enforce {constraint.kind.value}"
            f"{': ' + constraint.value if constraint.value else ''}."
            " Check this item yourself before buying.",
        )
        for constraint in line.hard_constraints
    )
    item = LineItem(
        name=line.display_name.lower(),
        display_text=_display_text(line),
        line_item_measurements=measurements,
        upcs=[upc] if upc is not None else [],
        filters=filters,
    )
    return item, found


def translate(
    plan: ShoppingPlan,
    *,
    link_days: int,
    verified_upcs: Mapping[int, str] | None = None,
) -> TranslationResult:
    """Translate a plan version into a shopping list request.

    Args:
        plan: The stored plan version.
        link_days: How many days the link should stay valid, 1 to 365.
        verified_upcs: Extra verified UPCs by item id, added to the ones
            the plan's own lines carry. Only verified codes are ever sent;
            none is invented.

    Returns:
        The request (None for a plan with no lines) and every issue, in
        plan order.
    """
    upcs = {
        line.item_id: str(line.constraints[VERIFIED_UPC])
        for line in plan.lines
        if line.constraints.get(VERIFIED_UPC)
    } | dict(verified_upcs or {})
    used: set[str] = set()
    items: list[LineItem] = []
    issues: list[TranslationIssue] = []
    for line in plan.lines:
        item, found = _translate_line(line, upcs, used)
        items.append(item)
        issues.extend(
            TranslationIssue(kind, line.item_id, line.display_name, detail)
            for kind, detail in found
        )
    if not items:
        return TranslationResult(request=None, issues=())
    request = ProductsLinkRequest(
        title=f"Grocery Butler shopping plan {plan.version}",
        line_items=items,
        expires_in=link_days,
    )
    return TranslationResult(request=request, issues=tuple(issues))
