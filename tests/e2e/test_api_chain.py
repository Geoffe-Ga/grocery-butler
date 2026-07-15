"""E2E: the full happy-path chain from meal text to a confirmed order.

Exercises every layer with real wiring -- ``MealParser`` + a seeded
recipe, ``Consolidator``, ``SafewayPipeline`` (``CartBuilder`` +
``OrderService``) over a ``MockTransport``, and the pending-actions
staging/confirmation flow. Uses only items that resolve cleanly (no
substitutions): bug #70 blocks driving a substituted cart through
submit -> confirm, so that path is covered (and excluded past preview)
separately in ``test_ordering.py``.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

from grocery_butler.models import ParsedMeal, PendingActionStatus
from grocery_butler.pending_actions import PendingActionsStore

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.e2e.conftest import SafewayMockState

pytestmark = pytest.mark.e2e


def _stage_order(
    signed_request: Callable[..., Any],
    meal_name: str,
) -> str:
    """Run meals/parse -> shopping-list/preview -> order/preview -> submit.

    Args:
        signed_request: The bound-token request-sending fixture.
        meal_name: Name of a recipe already resolvable via the recipe store.

    Returns:
        The ``action_id`` of the staged ``safeway_order_submit`` action.
    """
    parsed = signed_request("POST", "/api/v1/meals/parse", {"text": meal_name})
    meals = parsed.get_json()["meals"]
    preview = signed_request(
        "POST",
        "/api/v1/shopping-list/preview",
        {"meals": meals, "include_restock": False},
    )
    shopping_list = preview.get_json()["items"]
    order_preview = signed_request(
        "POST", "/api/v1/order/preview", {"shopping_list": shopping_list}
    )
    order_body = order_preview.get_json()
    submitted = signed_request(
        "POST",
        "/api/v1/order/submit",
        {"cart": order_body["cart"], "total": order_body["total"]},
    )
    action_id: str = submitted.get_json()["action_id"]
    return action_id


def test_full_chain_meal_to_confirmed_order(
    signed_request: Callable[..., Any],
    seed_recipe: ParsedMeal,
    no_claude: None,
    safeway_mock: SafewayMockState,
    db_path: str,
) -> None:
    """meals/parse -> shopping-list/preview -> order/preview -> submit -> confirm."""
    parsed = signed_request("POST", "/api/v1/meals/parse", {"text": seed_recipe.name})
    assert parsed.status_code == 200
    meals = parsed.get_json()["meals"]
    assert len(meals) == 1
    assert meals[0]["known_recipe"] is True

    preview = signed_request(
        "POST",
        "/api/v1/shopping-list/preview",
        {"meals": meals, "include_restock": False},
    )
    assert preview.status_code == 200
    shopping_list = preview.get_json()["items"]
    assert {item["ingredient"] for item in shopping_list} == {
        "ground beef",
        "spaghetti",
        "tomato sauce",
    }

    order_preview = signed_request(
        "POST", "/api/v1/order/preview", {"shopping_list": shopping_list}
    )
    assert order_preview.status_code == 200
    order_body = order_preview.get_json()
    cart = order_body["cart"]
    assert cart["failed_items"] == []
    assert cart["substituted_items"] == []
    assert len(cart["items"]) == 3

    submitted = signed_request(
        "POST",
        "/api/v1/order/submit",
        {"cart": cart, "total": order_body["total"]},
    )
    assert submitted.status_code == 200
    submit_body = submitted.get_json()
    assert submit_body["status"] == "pending_confirmation"
    action_id = submit_body["action_id"]

    # "spaghetti" (box) and "tomato sauce" (can) don't compare against the
    # mock's uniform "1 lb" product size, so the real quantity calculator
    # flags both incomparable_units (issue #59). Per the chief-architect's
    # ruling, the staged message must name them and their reason before
    # the human is asked to confirm.
    staged_message = submit_body["message"]
    assert "spaghetti" in staged_message
    assert "tomato sauce" in staged_message
    assert "incomparable_units" in staged_message

    confirmed = signed_request(
        "POST", "/api/v1/actions/confirm", {"action_id": action_id}
    )
    assert confirmed.status_code == 200
    confirm_body = confirmed.get_json()
    assert confirm_body["status"] == "approved"
    assert confirm_body["result"]["order_id"] == "e2e-order-1"

    pending = PendingActionsStore(db_path).get_pending_action(action_id)
    assert pending is not None
    assert pending.status is PendingActionStatus.APPROVED

    assert "/abs/pub/web/orders" in safeway_mock.requested_paths


def test_disabled_submission_returns_501_default_off(
    signed_request: Callable[..., Any],
    seed_recipe: ParsedMeal,
    no_claude: None,
    safeway_mock: SafewayMockState,
    db_path: str,
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """Issue #60: with the flag unset (the production default), confirm returns 501.

    ``e2e_env`` opts this suite's full-chain tests into submission via
    ``SAFEWAY_ORDER_SUBMISSION_ENABLED=true`` (see conftest.py); this test
    reverts to the real, unset production default to prove the fail-safe
    gate blocks a live submission end-to-end -- through the real
    ``SafewayPipeline`` and Flask confirm endpoint, not just unit mocks --
    and that the mocked Safeway order endpoint is never hit.
    """
    from grocery_butler.order_service import ORDER_SUBMISSION_DISABLED_MESSAGE

    monkeypatch.delenv("SAFEWAY_ORDER_SUBMISSION_ENABLED", raising=False)
    action_id = _stage_order(signed_request, seed_recipe.name)

    confirmed = signed_request(
        "POST", "/api/v1/actions/confirm", {"action_id": action_id}
    )

    assert confirmed.status_code == 501
    assert confirmed.get_json()["error"] == ORDER_SUBMISSION_DISABLED_MESSAGE

    pending = PendingActionsStore(db_path).get_pending_action(action_id)
    assert pending is not None
    assert pending.status is PendingActionStatus.PENDING

    # The disabled gate must fire before any network call reaches Safeway.
    assert "/abs/pub/web/orders" not in safeway_mock.requested_paths


def test_confirming_same_action_twice_returns_409(
    signed_request: Callable[..., Any],
    seed_recipe: ParsedMeal,
    no_claude: None,
    safeway_mock: SafewayMockState,
) -> None:
    """A second confirm call for an already-resolved action is rejected."""
    action_id = _stage_order(signed_request, seed_recipe.name)

    first = signed_request("POST", "/api/v1/actions/confirm", {"action_id": action_id})
    assert first.status_code == 200

    second = signed_request("POST", "/api/v1/actions/confirm", {"action_id": action_id})
    assert second.status_code == 409
