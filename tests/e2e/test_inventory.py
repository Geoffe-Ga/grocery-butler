"""E2E: inventory and restock queue endpoints against a real SQLite DB.

No Claude/Safeway seams needed here -- pure Flask + PantryManager + a
real temporary SQLite database.
"""

from __future__ import annotations

from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

    from flask.testing import FlaskClient

pytestmark = pytest.mark.e2e


def test_stock_add_then_inventory_then_duplicate_conflicts(
    client: FlaskClient,
    signed_headers: Callable[..., dict[str, str]],
) -> None:
    """Adding a new item succeeds once; a duplicate add returns 409."""
    headers = signed_headers()
    body = {"item": "oat milk", "category": "dairy"}

    created = client.post("/api/v1/stock/add", json=body, headers=headers)
    assert created.status_code == 201
    assert created.get_json()["ingredient"] == "oat milk"

    listed = client.get("/api/v1/inventory", headers=headers)
    assert listed.status_code == 200
    ingredients = [item["ingredient"] for item in listed.get_json()["items"]]
    assert "oat milk" in ingredients

    duplicate = client.post("/api/v1/stock/add", json=body, headers=headers)
    assert duplicate.status_code == 409


def test_restock_queue_lifecycle_and_untracked_update_404(
    client: FlaskClient,
    signed_headers: Callable[..., dict[str, str]],
) -> None:
    """Restock queue reflects low/out status and clears; unknown items 404."""
    headers = signed_headers()
    client.post(
        "/api/v1/stock/add",
        json={"item": "eggs", "category": "dairy"},
        headers=headers,
    )

    low = client.post(
        "/api/v1/stock/update",
        json={"item": "eggs", "status": "low"},
        headers=headers,
    )
    assert low.status_code == 200
    restock_low = client.get("/api/v1/restock", headers=headers)
    assert "eggs" in [i["ingredient"] for i in restock_low.get_json()["items"]]

    out = client.post(
        "/api/v1/stock/update",
        json={"item": "eggs", "status": "out"},
        headers=headers,
    )
    assert out.status_code == 200
    restock_out = client.get("/api/v1/restock", headers=headers)
    assert "eggs" in [i["ingredient"] for i in restock_out.get_json()["items"]]

    cleared = client.post("/api/v1/restock/clear", headers=headers)
    assert cleared.status_code == 200
    assert cleared.get_json()["cleared"] >= 1
    restock_after = client.get("/api/v1/restock", headers=headers)
    assert restock_after.get_json()["items"] == []

    missing = client.post(
        "/api/v1/stock/update",
        json={"item": "unobtainium", "status": "low"},
        headers=headers,
    )
    assert missing.status_code == 404
