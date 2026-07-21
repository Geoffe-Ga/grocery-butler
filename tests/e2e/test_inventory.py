"""E2E: inventory and restock queue endpoints against a real SQLite DB.

No Claude/Safeway seams needed here -- pure Flask + PantryManager + a
real temporary SQLite database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

pytestmark = pytest.mark.e2e


def test_stock_add_then_inventory_then_duplicate_conflicts(
    signed_request: Callable[..., Any],
) -> None:
    """Adding a new item succeeds once; a duplicate add returns 409."""
    body = {"item": "oat milk", "category": "dairy"}

    created = signed_request("POST", "/api/v1/stock/add", body)
    assert created.status_code == 201
    assert created.get_json()["ingredient"] == "oat milk"

    listed = signed_request("GET", "/api/v1/inventory")
    assert listed.status_code == 200
    ingredients = [item["ingredient"] for item in listed.get_json()["items"]]
    assert "oat milk" in ingredients

    duplicate = signed_request("POST", "/api/v1/stock/add", body)
    assert duplicate.status_code == 409


def test_restock_queue_lifecycle_and_untracked_update_404(
    signed_request: Callable[..., Any],
) -> None:
    """Restock queue reflects low/out status and clears; unknown items 404."""
    signed_request("POST", "/api/v1/stock/add", {"item": "eggs", "category": "dairy"})

    low = signed_request(
        "POST", "/api/v1/stock/update", {"item": "eggs", "status": "low"}
    )
    assert low.status_code == 200
    restock_low = signed_request("GET", "/api/v1/restock")
    assert "eggs" in [i["ingredient"] for i in restock_low.get_json()["items"]]

    out = signed_request(
        "POST", "/api/v1/stock/update", {"item": "eggs", "status": "out"}
    )
    assert out.status_code == 200
    restock_out = signed_request("GET", "/api/v1/restock")
    assert "eggs" in [i["ingredient"] for i in restock_out.get_json()["items"]]

    cleared = signed_request("POST", "/api/v1/restock/clear")
    assert cleared.status_code == 200
    assert cleared.get_json()["cleared"] >= 1
    restock_after = signed_request("GET", "/api/v1/restock")
    assert restock_after.get_json()["items"] == []

    missing = signed_request(
        "POST", "/api/v1/stock/update", {"item": "unobtainium", "status": "low"}
    )
    assert missing.status_code == 404
