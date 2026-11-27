"""The universal list export: a plan anyone can take to any store (P1.12).

Produces a grouped checklist as plain text, JSON and CSV. It contacts
nothing, needs no authorization, and knows no retailer. The household (or
OpenClaw, in the user's own session) takes the list wherever it shops.
"""

from __future__ import annotations

import csv
import io
import json
from typing import TYPE_CHECKING, Any

from grocery_butler.fulfillment.base import (
    AdapterDescription,
    Authorization,
    Capability,
    FulfillmentAdapter,
    HandoffResult,
    HandoffStatus,
    unenforced_constraints,
)
from grocery_butler.models import HardConstraintKind, NeedReasonKind
from grocery_butler.plan_preferences import (
    AVOID_BRANDS,
    GENERIC,
    ORGANIC,
    PREFERRED_BRAND,
    PREFERRED_SIZE,
    VERIFIED_UPC,
    unfiltered_phrases,
)

if TYPE_CHECKING:
    from grocery_butler.models import PlanLine, ShoppingPlan

EXPORT_FORMATS = ("text", "json", "csv")

USUAL = "the usual"
NO_CATEGORY = "Other"

_REASON_LABELS: dict[NeedReasonKind, str] = {
    NeedReasonKind.LOW_STOCK: "running low",
    NeedReasonKind.OUT: "out",
    NeedReasonKind.RECURRING_STAPLE: "staple",
    NeedReasonKind.MANUAL_REQUEST: "requested",
}

_CSV_COLUMNS = (
    "category",
    "item",
    "quantity",
    "unit",
    "reasons",
    "preferred_brand",
    "preferred_size",
    "other_preferences",
    "verified_upc",
    "must",
)

CHECK_CONSTRAINTS = (
    " A list can't enforce the lines marked MUST: check each of those "
    "products yourself."
)

# A spreadsheet treats a cell starting with one of these as a formula.
_FORMULA_PREFIXES = ("=", "+", "-", "@")


def _category(line: PlanLine) -> str:
    """Name a line's category for a person.

    Args:
        line: A plan line.

    Returns:
        A title-cased category, or ``Other`` when the item has none.
    """
    if line.category is None:
        return NO_CATEGORY
    return line.category.value.replace("_", " ").title()


def _reasons(line: PlanLine) -> list[str]:
    """Describe why a line is on the plan, in a few words each.

    Args:
        line: A plan line.

    Returns:
        One label per need reason, then one naming every recipe that wants
        the item.
    """
    labels = [
        _REASON_LABELS[reason.kind]
        for reason in line.reasons
        if reason.kind in _REASON_LABELS
    ]
    recipes = [
        str(reason.detail.get("recipe", ""))
        for reason in line.reasons
        if reason.kind is NeedReasonKind.RECIPE_DEMAND
    ]
    named = [recipe for recipe in recipes if recipe]
    if named:
        labels.append("for " + ", ".join(named))
    return labels


def _must(line: PlanLine) -> list[str]:
    """Word a line's hard constraints for a person.

    Args:
        line: A plan line.

    Returns:
        One short phrase per hard constraint, e.g. ``no peanut``.
    """
    phrases = []
    for constraint in line.hard_constraints:
        if constraint.kind is HardConstraintKind.ALLERGEN_EXCLUSION:
            phrases.append(f"no {constraint.value}")
        elif constraint.kind is HardConstraintKind.DO_NOT_SUBSTITUTE:
            phrases.append("no substitutes")
        else:
            phrases.append(constraint.value)
    return phrases


def _must_text(line: PlanLine) -> str:
    """Summarize a line's hard constraints for the checklist.

    Args:
        line: A plan line.

    Returns:
        `` {MUST: ...}``, or an empty string when the line has none.
    """
    phrases = _must(line)
    return f" {{MUST: {'; '.join(phrases)}}}" if phrases else ""


def _number(quantity: float) -> str:
    """Format a quantity without padding.

    Args:
        quantity: The amount.

    Returns:
        ``4`` for 4.0 and ``1.5`` for 1.5.
    """
    return f"{quantity:g}"


def _amount_text(line: PlanLine) -> str:
    """Say how much of a line to buy.

    Args:
        line: A plan line.

    Returns:
        The amounts joined with ``+``, or "the usual" when nothing stated
        an amount.
    """
    if not line.amounts:
        return USUAL
    return " + ".join(
        f"{_number(amount.quantity)} {amount.unit.value}" for amount in line.amounts
    )


def _preference_text(line: PlanLine) -> str:
    """Summarize every soft preference on a line.

    Args:
        line: A plan line.

    Returns:
        For example `` [prefer Brand, Size, organic; avoid Other; UPC
        012345678905]``, or an empty string when there are none.
    """
    wanted = [
        str(line.constraints[key])
        for key in (PREFERRED_BRAND, PREFERRED_SIZE)
        if line.constraints.get(key)
    ]
    wanted += ["organic"] if line.constraints.get(ORGANIC) is True else []
    wanted += ["the store brand"] if line.constraints.get(GENERIC) is True else []
    avoided = [str(brand) for brand in line.constraints.get(AVOID_BRANDS) or []]
    upc = line.constraints.get(VERIFIED_UPC)
    parts = (
        ([f"prefer {', '.join(wanted)}"] if wanted else [])
        + ([f"avoid {', '.join(avoided)}"] if avoided else [])
        + ([f"UPC {upc}"] if upc else [])
    )
    return f" [{'; '.join(parts)}]" if parts else ""


def _other_preferences(line: PlanLine) -> list[str]:
    """List a line's preferences beyond brand and size, for one CSV cell.

    Args:
        line: A plan line.

    Returns:
        ``organic`` if wanted, then the preferences no filter expresses.
    """
    organic = ["organic"] if line.constraints.get(ORGANIC) is True else []
    return organic + unfiltered_phrases(line.constraints)


def _grouped(plan: ShoppingPlan) -> list[tuple[str, list[PlanLine]]]:
    """Group a plan's lines by category, keeping the plan's order.

    Lines without a category are gathered last, under ``Other``.

    Args:
        plan: The plan.

    Returns:
        ``(category, lines)`` pairs.
    """
    groups: dict[str, list[PlanLine]] = {}
    for line in plan.lines:
        groups.setdefault(_category(line), []).append(line)
    uncategorized = groups.pop(NO_CATEGORY, None)
    ordered = list(groups.items())
    if uncategorized:
        ordered.append((NO_CATEGORY, uncategorized))
    return ordered


def _safe_cell(value: str) -> str:
    """Stop a CSV cell being read as a spreadsheet formula.

    Item names come from recipes and people. A cell that begins with a
    formula character is prefixed with an apostrophe, which spreadsheets
    show as text.

    Args:
        value: The cell's text.

    Returns:
        The text, made inert if it could be a formula.
    """
    return f"'{value}" if value.startswith(_FORMULA_PREFIXES) else value


def render_text(plan: ShoppingPlan) -> str:
    """Render a plan as a checklist grouped by category.

    Args:
        plan: The plan.

    Returns:
        Plain text ending in a newline.
    """
    blocks = [f"Shopping plan v{plan.version}"]
    for category, lines in _grouped(plan):
        rows = [
            f"[ ] {line.display_name}: {_amount_text(line)}"
            f" ({'; '.join(_reasons(line))}){_preference_text(line)}"
            f"{_must_text(line)}"
            for line in lines
        ]
        blocks.append("\n".join([category, *rows]))
    if not plan.lines:
        blocks.append("Nothing to buy.")
    return "\n\n".join(blocks) + "\n"


def render_json(plan: ShoppingPlan) -> str:
    """Render a plan as structured JSON grouped by category.

    Args:
        plan: The plan.

    Returns:
        Indented JSON with sorted keys, ending in a newline.
    """
    categories: list[dict[str, Any]] = [
        {
            "category": category,
            "lines": [
                {
                    "item": line.display_name,
                    "amounts": [
                        {"quantity": amount.quantity, "unit": amount.unit.value}
                        for amount in line.amounts
                    ],
                    "reasons": _reasons(line),
                    "preferred_brand": line.constraints.get(PREFERRED_BRAND),
                    "preferred_size": line.constraints.get(PREFERRED_SIZE),
                    "organic": line.constraints.get(ORGANIC) is True,
                    "generic": line.constraints.get(GENERIC) is True,
                    "avoid_brands": list(line.constraints.get(AVOID_BRANDS) or []),
                    "verified_upc": line.constraints.get(VERIFIED_UPC),
                    "must": _must(line),
                }
                for line in lines
            ],
        }
        for category, lines in _grouped(plan)
    ]
    document = {
        "plan_version": plan.version,
        "content_hash": plan.content_hash,
        "categories": categories,
    }
    return json.dumps(document, indent=2, sort_keys=True) + "\n"


def render_csv(plan: ShoppingPlan) -> str:
    """Render a plan as CSV, one row per amount.

    A line with no stated amount gets one row with an empty quantity and
    unit. Cells are guarded against spreadsheet formula injection.

    Args:
        plan: The plan.

    Returns:
        CSV text with a header row and ``\\n`` line endings.
    """
    buffer = io.StringIO()
    writer = csv.writer(buffer, lineterminator="\n")
    writer.writerow(_CSV_COLUMNS)
    for category, lines in _grouped(plan):
        for line in lines:
            amounts = [
                (_number(amount.quantity), amount.unit.value) for amount in line.amounts
            ] or [("", "")]
            for quantity, unit in amounts:
                writer.writerow(
                    _safe_cell(cell)
                    for cell in (
                        category,
                        line.display_name,
                        quantity,
                        unit,
                        "; ".join(_reasons(line)),
                        str(line.constraints.get(PREFERRED_BRAND) or ""),
                        str(line.constraints.get(PREFERRED_SIZE) or ""),
                        "; ".join(_other_preferences(line)),
                        str(line.constraints.get(VERIFIED_UPC) or ""),
                        "; ".join(_must(line)),
                    )
                )
    return buffer.getvalue()


class ExportAdapter(FulfillmentAdapter):
    """Export a plan as a list the household can take to any store."""

    def describe(self) -> AdapterDescription:
        """Declare the export's single capability.

        Returns:
            The adapter's description.
        """
        return AdapterDescription(
            name="export",
            display_name="Shopping list export",
            capabilities=frozenset({Capability.EXPORT_LIST}),
            authorization=Authorization.NONE,
            data_freshness=(
                "Exactly the stored plan version. It knows nothing about any "
                "store's prices, stock or products."
            ),
            human_required=True,
        )

    def create_handoff(
        self, plan: ShoppingPlan, *, constraints_acknowledged: bool = False
    ) -> HandoffResult:
        """Render the plan in every export format.

        Args:
            plan: The stored plan version to export.
            constraints_acknowledged: Not needed here. The list prints
                every hard constraint on its line, so the person reading
                it is the review.

        Returns:
            A created handoff whose artifacts hold the text, JSON and CSV.
            A list can enforce nothing, so every hard constraint on the plan
            is printed on its line and reported as unenforced.
        """
        unenforced = unenforced_constraints(self.describe(), plan)
        return HandoffResult(
            adapter="export",
            status=HandoffStatus.CREATED,
            plan_version=plan.version,
            plan_content_hash=plan.content_hash,
            human_action=(
                "Take this list to any store or shopping site and buy what you "
                "choose there. Nothing has been ordered."
                + (CHECK_CONSTRAINTS if unenforced else "")
            ),
            unenforced_constraints=unenforced,
            artifacts={
                "text": render_text(plan),
                "json": render_json(plan),
                "csv": render_csv(plan),
            },
        )
