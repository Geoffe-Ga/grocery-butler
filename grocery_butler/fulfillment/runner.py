"""Run a fulfillment adapter with retries and an audit trail (ADR 0015).

Every handoff goes through :func:`create_handoff`, whichever adapter does
it. A passing fault is retried a bounded number of times. A handoff that
fails, by raising or by returning a failed status, writes an
``adapter.failed`` audit event, which the metrics count.

Like the rest of this package's shared code, this knows only the adapter
interface, never a concrete adapter.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING

from grocery_butler.fulfillment.base import HandoffStatus
from grocery_butler.observability import audit
from grocery_butler.retries import call_with_retry

if TYPE_CHECKING:
    from collections.abc import Callable

    from grocery_butler.fulfillment.base import FulfillmentAdapter, HandoffResult
    from grocery_butler.models import ShoppingPlan


def create_handoff(
    adapter: FulfillmentAdapter,
    plan: ShoppingPlan,
    *,
    sleep: Callable[[float], None] = time.sleep,
) -> HandoffResult:
    """Ask an adapter for a handoff, recording how it went.

    Args:
        adapter: The adapter to use.
        plan: The plan version to hand off.
        sleep: Waits between tries; tests pass a recorder.

    Returns:
        The adapter's result, which may itself report a failed status.

    Raises:
        RetriesExhaustedError: If every attempt failed with a passing fault.
        Exception: Any other failure, as the adapter raised it.
    """
    name = adapter.describe().name
    try:
        result = call_with_retry(
            lambda: adapter.create_handoff(plan), what=f"adapter.{name}", sleep=sleep
        )
    except Exception as err:
        audit(
            "adapter.failed",
            adapter=name,
            plan_version=plan.version,
            error=type(err).__name__,
        )
        raise
    if result.status is HandoffStatus.FAILED:
        audit(
            "adapter.failed",
            adapter=name,
            plan_version=plan.version,
            error="failed_status",
        )
    else:
        audit(
            "adapter.handoff_created",
            adapter=name,
            plan_version=plan.version,
            unenforced_constraints=len(result.unenforced_constraints),
        )
    return result
