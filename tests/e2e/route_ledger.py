"""Which routes the end-to-end lane must drive, and which it may skip.

The app registers routes; ``tests/e2e`` drives some of them through the
real app. This module is the list of routes that no end-to-end test
reaches yet, each with the reason. A route that is neither reached nor
listed here fails the run, so a new route can't ship without either an
end-to-end test or a written reason (issue #229). A listed route that
*is* reached also fails the run, so the list can only shrink.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

if TYPE_CHECKING:
    from collections.abc import Iterable

    from flask import Flask

#: A route as the ledger names it: method, then the rule Flask registered.
Route = tuple[str, str]

_ONE_AT_A_TIME = (
    "Driven through the real app in tests/test_api_v2.py, one capability at"
    " a time. No journey in tests/e2e walks through it yet."
)
_DASHBOARD = (
    "Rendered and posted through the real app in tests/test_app.py. No"
    " end-to-end flow visits it yet."
)
_V1_READS = "Driven in tests/test_api.py. No end-to-end flow reads it yet."
_HEALTH = "Covered by tests/test_health.py; a probe, not part of any flow."

#: Routes no end-to-end test reaches yet, and why that is acceptable today.
NOT_YET_END_TO_END: dict[Route, str] = {
    ("GET", "/api/v1/actions"): _V1_READS,
    ("GET", "/api/v1/actions/<action_id>"): _V1_READS,
    ("GET", "/api/v1/preferences"): _V1_READS,
    ("POST", "/api/v2/constraints/<int:constraint_id>/deactivate"): _ONE_AT_A_TIME,
    ("GET", "/api/v2/fulfillment/adapters"): _ONE_AT_A_TIME,
    ("GET", "/api/v2/handoffs/<int:approval_id>"): _ONE_AT_A_TIME,
    ("POST", "/api/v2/handoffs/<int:approval_id>/reject"): _ONE_AT_A_TIME,
    ("GET", "/api/v2/household/export"): _ONE_AT_A_TIME,
    ("POST", "/api/v2/items/<int:item_id>/aliases"): _ONE_AT_A_TIME,
    ("POST", "/api/v2/items/<int:item_id>/aliases/remove"): _ONE_AT_A_TIME,
    ("GET", "/api/v2/items/<int:item_id>/check-reminder"): _ONE_AT_A_TIME,
    ("POST", "/api/v2/items/<int:item_id>/check-reminder"): _ONE_AT_A_TIME,
    ("GET", "/api/v2/items/<int:item_id>/history"): _ONE_AT_A_TIME,
    ("POST", "/api/v2/items/<int:item_id>/merge"): _ONE_AT_A_TIME,
    ("POST", "/api/v2/items/<int:item_id>/unmerge"): _ONE_AT_A_TIME,
    ("GET", "/api/v2/members"): _ONE_AT_A_TIME,
    ("POST", "/api/v2/members/<key>/deactivate"): _ONE_AT_A_TIME,
    ("POST", "/api/v2/members/<key>/reactivate"): _ONE_AT_A_TIME,
    ("GET", "/api/v2/needs/<int:need_id>"): _ONE_AT_A_TIME,
    ("POST", "/api/v2/needs/<int:need_id>/cancel"): _ONE_AT_A_TIME,
    ("POST", "/api/v2/needs/<int:need_id>/defer"): _ONE_AT_A_TIME,
    ("POST", "/api/v2/needs/<int:need_id>/dismiss"): _ONE_AT_A_TIME,
    ("POST", "/api/v2/needs/<int:need_id>/reopen"): _ONE_AT_A_TIME,
    ("GET", "/api/v2/observation-batches"): _ONE_AT_A_TIME,
    ("GET", "/api/v2/observation-batches/<int:batch_id>"): _ONE_AT_A_TIME,
    ("POST", "/api/v2/observation-batches/<int:batch_id>/reject"): _ONE_AT_A_TIME,
    ("POST", "/api/v2/observations/<int:observation_id>/correct"): _ONE_AT_A_TIME,
    ("POST", "/api/v2/observations/<int:observation_id>/retract"): _ONE_AT_A_TIME,
    ("GET", "/api/v2/plans"): _ONE_AT_A_TIME,
    ("GET", "/api/v2/plans/latest"): _ONE_AT_A_TIME,
    ("GET", "/api/v2/receipts"): _ONE_AT_A_TIME,
    ("GET", "/api/v2/receipts/<int:receipt_id>"): _ONE_AT_A_TIME,
    ("POST", "/api/v2/receipts/<int:receipt_id>/reject"): _ONE_AT_A_TIME,
    ("GET", "/api/v2/reminders/checks"): _ONE_AT_A_TIME,
    ("POST", "/api/v2/reminders/checks/issue"): _ONE_AT_A_TIME,
    ("GET", "/api/v2/soft-preferences"): _ONE_AT_A_TIME,
    ("GET", "/"): _DASHBOARD,
    ("GET", "/brands"): _DASHBOARD,
    ("POST", "/brands/<int:pref_id>/remove"): _DASHBOARD,
    ("POST", "/brands/add"): _DASHBOARD,
    ("GET", "/handoff/export.txt"): (
        "Driven in tests/test_handoff_ux.py. No end-to-end flow downloads it yet."
    ),
    ("GET", "/inventory"): _DASHBOARD,
    ("POST", "/inventory/add"): _DASHBOARD,
    ("POST", "/inventory/update"): _DASHBOARD,
    ("GET", "/pantry"): _DASHBOARD,
    ("GET", "/preferences"): _DASHBOARD,
    ("POST", "/preferences"): _DASHBOARD,
    ("GET", "/recipes"): _DASHBOARD,
    ("GET", "/recipes/<int:recipe_id>"): _DASHBOARD,
    ("POST", "/recipes/<int:recipe_id>/delete"): _DASHBOARD,
    ("GET", "/recipes/add"): _DASHBOARD,
    ("POST", "/recipes/add"): _DASHBOARD,
    ("GET", "/shopping-list"): _DASHBOARD,
    ("POST", "/shopping-list/generate"): _DASHBOARD,
    ("GET", "/health"): _HEALTH,
    ("GET", "/healthz"): _HEALTH,
}

_IGNORED_METHODS = frozenset({"HEAD", "OPTIONS"})


def registered_routes(app: Flask) -> set[Route]:
    """Return every route the app serves, except static files.

    Args:
        app: The application.

    Returns:
        One entry per method of each registered rule.
    """
    return {
        (method, rule.rule)
        for rule in app.url_map.iter_rules()
        if rule.endpoint != "static"
        for method in (rule.methods or set()) - _IGNORED_METHODS
    }


def problems(registered: Iterable[Route], reached: Iterable[Route]) -> list[str]:
    """Compare what exists, what was driven and what is excused.

    Args:
        registered: Every route the app serves.
        reached: Every route an end-to-end test sent a request to.

    Returns:
        One line per problem, sorted; empty when the ledger balances.
    """
    registered, reached = set(registered), set(reached)
    excused = set(NOT_YET_END_TO_END)
    found = [
        f"{method} {rule}: no end-to-end test reaches it, and it is not listed"
        " in tests/e2e/route_ledger.py. Add a test, or list it with a reason."
        for method, rule in registered - reached - excused
    ]
    found += [
        f"{method} {rule}: an end-to-end test reaches it now. Remove it from"
        " NOT_YET_END_TO_END in tests/e2e/route_ledger.py."
        for method, rule in excused & reached
    ]
    found += [
        f"{method} {rule}: listed in tests/e2e/route_ledger.py, but the app"
        " has no such route. Remove the entry."
        for method, rule in excused - registered
    ]
    return sorted(found)
