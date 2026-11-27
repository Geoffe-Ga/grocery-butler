"""What we say about a handoff, in one place (ADR 0020).

A handoff creates a link. It is not an order, and nothing here may say or
imply that anything was bought, paid for or is being delivered. Every
sentence a person hears about a handoff, from OpenClaw or the dashboard, is
built in this module, and a test checks each one against
:data:`BANNED_PHRASES`.

The button text and the copy rules follow Instacart's launch checklist as
recorded in ADR 0014: the button says "Shop on Instacart", and we never use
the word "partner" or make a delivery claim.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from grocery_butler.fulfillment.handoffs import AttemptStatus

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

    from grocery_butler.fulfillment.handoffs import HandoffAttempt

# Words that would claim a purchase, a payment or a delivery, or that
# Instacart's copy rules forbid. Matched without regard to case.
BANNED_PHRASES = (
    "ordered",
    "purchased",
    "on the way",
    "bought",
    "paid",
    "delivered",
    "free delivery",
    "partner",
)

OPEN_BUTTON = "Shop on Instacart"
EXPORT_LINK = "Export list instead"
NOT_AN_ORDER = (
    "This is a link to review, not a purchase. You choose the store, check"
    " each item and check out yourself."
)

_HEADLINES = {
    AttemptStatus.CREATED: "Your plan is ready to review in {where}.",
    AttemptStatus.OPENED: "You opened this plan in {where}.",
    AttemptStatus.EXPIRED: (
        "The {where} link for this plan has run out. Hand the plan off again"
        " to get a new one."
    ),
    AttemptStatus.FAILED: (
        "No {where} link was made for this plan. You can use the list export instead."
    ),
    AttemptStatus.PENDING: "The {where} link for this plan is still being made.",
}
_MAX_NAMED = 3
_CONSTRAINT_ISSUE = "hard_constraint_unrepresentable"
_PREFERENCE_ISSUE = "preference_not_applied"
_PREFERENCE_NOTE = "These preferences can't be applied here, so check as you shop: "


def _constraint_check(entry: Mapping[str, Any]) -> str:
    """Word one unenforced hard constraint as something to check.

    Args:
        entry: An entry of the payload's ``unenforced_constraints``.

    Returns:
        For example ``Granola Bars: check for peanut``.
    """
    rule = str(entry.get("value") or entry.get("kind", "")).replace("_", " ")
    return f"{entry.get('display_name', 'an item')}: check for {rule}"


def _issue_check(issue: Mapping[str, Any]) -> str:
    """Word one translation issue as something to check.

    Args:
        issue: An entry of the payload's ``issues``.

    Returns:
        The item's name and what the handoff left out.
    """
    detail = str(issue.get("detail", "")).rstrip(".")
    return f"{issue.get('display_name', 'an item')}: {detail}"


def checks_for(payload: Mapping[str, Any]) -> list[str]:
    """List what a person should check before buying, one short clause each.

    Args:
        payload: The approved handoff payload.

    Returns:
        A clause per unenforced hard constraint and per other translation
        issue, in the payload's order, without repeats.
    """
    constraints = [
        _constraint_check(entry) for entry in payload.get("unenforced_constraints", [])
    ]
    reported = payload.get("issues", [])
    issues = [
        _issue_check(issue)
        for issue in reported
        if issue.get("kind") not in (_CONSTRAINT_ISSUE, _PREFERENCE_ISSUE)
    ]
    return list(dict.fromkeys(constraints + issues + _preference_note(reported)))


def _preference_note(issues: Iterable[Mapping[str, Any]]) -> list[str]:
    """Say once which soft preferences the handoff could not apply.

    A preference set for the whole household lands on every line. One
    clause naming the items keeps the list readable, where a clause per
    line would bury anything else worth checking (issue #215).

    Args:
        issues: The payload's ``issues``.

    Returns:
        One clause listing each affected item with its preferences, or
        nothing when every preference was applied.
    """
    affected = list(
        dict.fromkeys(
            f"{issue.get('display_name', 'an item')} ({issue.get('detail', '')})"
            for issue in issues
            if issue.get("kind") == _PREFERENCE_ISSUE
        )
    )
    return [_PREFERENCE_NOTE + ", ".join(affected)] if affected else []


def summarize(attempt: HandoffAttempt, payload: Mapping[str, Any]) -> str:
    """Say where a handoff stands, in words a person can be told.

    Args:
        attempt: The handoff attempt.
        payload: The approved handoff payload it was made from.

    Returns:
        One or two sentences. For a live link, it also says how many items
        need the person's own check and names the first few.
    """
    where = str(payload.get("adapter_display_name") or attempt.adapter)
    headline = _HEADLINES[attempt.status].format(where=where)
    if attempt.status not in (AttemptStatus.CREATED, AttemptStatus.OPENED):
        return headline
    checks = checks_for(payload)
    if not checks:
        return f"{headline} {NOT_AN_ORDER}"
    count = len(checks)
    noun = "item needs" if count == 1 else "items need"
    named = "; ".join(checks[:_MAX_NAMED])
    more = f"; and {count - _MAX_NAMED} more" if count > _MAX_NAMED else ""
    return f"{headline} {count} {noun} your check: {named}{more}. {NOT_AN_ORDER}"
