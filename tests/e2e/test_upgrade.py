"""E2E: a database from before the pivot is upgraded and then used.

Migrations are tested one at a time and on empty databases. Here a
database is built at migration 009, the last schema before household
items, and filled with the kind of rows a household really had: recipes,
staples, inventory in every status, a brand rule, dietary text, and one
row of each kind with a blank name. Then every later migration and hook
runs, and the product is used through HTTP (issue #230).

A migration that drops, duplicates or orphans old rows, or that only
works on an empty table, fails here. It runs on both dialects.
"""

from __future__ import annotations

from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest

from grocery_butler.data_lifecycle import (
    DELETE_PHRASE,
    RESTORE_PHRASE,
    HouseholdData,
)
from grocery_butler.db import get_connection
from grocery_butler.db.migrate import latest_version, migrate
from tests.e2e.conftest import DIALECTS, V2, empty_database

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator
    from pathlib import Path


pytestmark = pytest.mark.e2e

#: The last schema version before canonical household items existed.
LAST_OLD_VERSION = 9

#: One literal query per table that held an ``ingredient`` name before items.
BLANK_NAME_QUERIES = {
    "recipe_ingredients": "SELECT ingredient FROM recipe_ingredients",
    "household_inventory": "SELECT ingredient FROM household_inventory",
    "pantry_staples": "SELECT ingredient FROM pantry_staples",
}

OLD_ROWS: list[tuple[str, tuple[Any, ...]]] = [
    (
        "INSERT INTO recipes (id, name, display_name, default_servings)"
        " VALUES (?, ?, ?, ?)",
        (900, "veggie chili", "Veggie Chili", 4),
    ),
    (
        "INSERT INTO recipe_ingredients (recipe_id, ingredient, quantity, unit,"
        " category, is_pantry_item, quantity_per_serving)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (900, "kidney beans", 2.0, "can", "pantry_dry", False, 0.5),
    ),
    (
        "INSERT INTO recipe_ingredients (recipe_id, ingredient, quantity, unit,"
        " category, is_pantry_item, quantity_per_serving)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (900, "oat milk", 1.0, "cup", "dairy", False, 0.25),
    ),
    (
        "INSERT INTO recipe_ingredients (recipe_id, ingredient, quantity, unit,"
        " category, is_pantry_item, quantity_per_serving)"
        " VALUES (?, ?, ?, ?, ?, ?, ?)",
        (900, "  ", 1.0, "each", "other", False, 0.25),
    ),
    (
        "INSERT INTO household_inventory (ingredient, display_name, category,"
        " status) VALUES (?, ?, ?, ?)",
        ("oat milk", "Oat Milk", "dairy", "low"),
    ),
    (
        "INSERT INTO household_inventory (ingredient, display_name, category,"
        " status) VALUES (?, ?, ?, ?)",
        ("eggs", "Eggs", "dairy", "out"),
    ),
    (
        "INSERT INTO household_inventory (ingredient, display_name, category,"
        " status) VALUES (?, ?, ?, ?)",
        ("rice", "Rice", "pantry_dry", "on_hand"),
    ),
    (
        "INSERT INTO household_inventory (ingredient, display_name, status)"
        " VALUES (?, ?, ?)",
        (" ", "Unnamed", "low"),
    ),
    (
        "INSERT INTO pantry_staples (ingredient, display_name) VALUES (?, ?)",
        ("\t", "Unnamed staple"),
    ),
    (
        "INSERT INTO brand_preferences (match_target, match_type, brand,"
        " preference_type) VALUES (?, ?, ?, ?)",
        ("oat milk", "ingredient", "Oatly", "preferred"),
    ),
    (
        "INSERT INTO preferences (key, value) VALUES (?, ?) RETURNING key",
        ("dietary_restrictions", "vegetarian, no peanuts"),
    ),
]


@pytest.fixture(params=DIALECTS)
def upgraded(request: pytest.FixtureRequest, tmp_path: Path) -> Iterator[str]:
    """Yield a database built at the old schema, filled, then fully migrated."""
    with empty_database(request.param, tmp_path) as location:
        migrate(location, target=LAST_OLD_VERSION)
        with closing(get_connection(location)) as conn:
            for statement, values in OLD_ROWS:
                result = conn.execute(statement, values)
                if "RETURNING" in statement:
                    result.fetchall()  # drain it so the commit sees no open cursor
            conn.commit()
        migrate(location)
        yield location


@pytest.fixture()
def db_path(upgraded: str) -> str:
    """Point the suite's app, environment and client at the upgraded database."""
    return upgraded


def _names(items: list[dict[str, Any]], key: str = "ingredient") -> list[str]:
    """Return one field from each row, sorted."""
    return sorted(str(item[key]) for item in items)


def test_every_migration_ran(upgraded: str) -> None:
    """The old database reached today's schema version."""
    exported = HouseholdData(upgraded).export()

    assert exported["schema_version"] == latest_version()


def test_the_old_data_is_still_served_by_api_v1(
    signed_request: Callable[..., Any],
) -> None:
    """Inventory, recipes and brand rules come back; blank rows are left out."""
    inventory = signed_request("GET", "/api/v1/inventory").get_json()["items"]
    recipes = signed_request("GET", "/api/v1/recipes").get_json()["recipes"]
    detail = signed_request("GET", f"/api/v1/recipes/{recipes[0]['id']}").get_json()
    brands = signed_request("GET", "/api/v1/brands").get_json()

    assert _names(inventory) == ["eggs", "oat milk", "rice"]
    assert {item["ingredient"]: item["status"] for item in inventory} == {
        "eggs": "out",
        "oat milk": "low",
        "rice": "on_hand",
    }
    assert [recipe["name"] for recipe in recipes] == ["veggie chili"]
    assert "Oatly" in str(brands)
    assert "kidney beans" in str(detail)
    assert "oat milk" in str(detail)


def test_old_names_became_linked_household_items(v2: V2) -> None:
    """One item per distinct name, with the state the old inventory held."""
    items = {item["name"]: item for item in v2.ok("GET", "/items")["items"]}

    assert {"eggs", "oat milk", "rice", "kidney beans"} <= set(items)
    assert "" not in items
    assert " " not in items
    assert len([name for name in items if name == "oat milk"]) == 1
    assert items["oat milk"]["state"]["state"] == "low"
    assert items["eggs"]["state"]["state"] == "out"
    assert items["rice"]["state"]["state"] == "on_hand"


def test_low_and_out_items_have_needs_and_can_be_planned(v2: V2) -> None:
    """What the old inventory said was running out can be bought today."""
    items = {item["name"]: item for item in v2.ok("GET", "/items")["items"]}
    wanted = {items["oat milk"]["id"], items["eggs"]["id"]}

    needs = v2.ok("GET", "/needs")["needs"]
    assert {need["item_id"] for need in needs} == wanted

    for need in needs:
        v2.ok("POST", f"/needs/{need['id']}/approve")
    plan = v2.ok("POST", "/plans", {})
    export = v2.ok("GET", f"/plans/{plan['version']}/export")

    assert {line["item_id"] for line in plan["lines"]} == wanted
    assert "Oat Milk" in export["content"]
    assert "Eggs" in export["content"]


def test_the_dietary_text_became_a_hard_constraint(v2: V2) -> None:
    """The old free text is kept as a rule, marked as unverified."""
    constraints = v2.ok("GET", "/constraints")["constraints"]

    (rule,) = [each for each in constraints if each["kind"] == "dietary_rule"]
    assert rule["value"] == "vegetarian, no peanuts"
    assert "unverified" in rule["note"]


def test_the_upgraded_household_survives_export_delete_and_restore(
    upgraded: str, v2: V2
) -> None:
    """The portable copy of an upgraded database restores to the same data."""
    before = v2.ok("GET", "/items")["items"]
    needs_before = v2.ok("GET", "/needs")["needs"]
    data = HouseholdData(upgraded)
    exported = data.export()

    data.delete(DELETE_PHRASE)
    assert HouseholdData(upgraded).export()["tables"]["household_items"] == []

    data.restore(exported, RESTORE_PHRASE)

    after = v2.ok("GET", "/items")["items"]
    assert _names(after, "name") == _names(before, "name")
    assert {item["name"]: item["state"]["state"] for item in after} == {
        item["name"]: item["state"]["state"] for item in before
    }
    assert v2.ok("GET", "/needs")["needs"] == needs_before


@pytest.mark.parametrize(
    "table", ["recipe_ingredients", "household_inventory", "pantry_staples"]
)
def test_rows_with_no_name_were_deleted(upgraded: str, table: str) -> None:
    """Migration 025 removes them; nothing nameless is left to skip over."""
    with closing(get_connection(upgraded)) as conn:
        names = [
            row["ingredient"]
            for row in conn.execute(BLANK_NAME_QUERIES[table]).fetchall()
        ]

    assert names
    assert [name for name in names if not name.strip()] == []


def test_old_recipes_are_entirely_required_and_plan_without_a_question(
    signed_request: Callable[..., Any], v2: V2
) -> None:
    """Migration 027 leaves old recipes as they were: nothing in them is optional."""
    recipes = signed_request("GET", "/api/v1/recipes").get_json()["recipes"]
    recipe_id = recipes[0]["id"]
    detail = signed_request("GET", f"/api/v1/recipes/{recipe_id}").get_json()

    planned = v2.post("/plans", {"meals": [{"recipe_id": recipe_id}]})

    ingredients = [*detail["purchase_items"], *detail["pantry_items"]]
    plan = planned.get_json()
    assert _names(ingredients) == ["kidney beans", "oat milk"]
    assert [item["is_optional"] for item in ingredients] == [False, False]
    assert planned.status_code == 201, planned.get_data(as_text=True)
    assert plan["meals"] == [{"recipe_id": recipe_id, "servings": 4}]
    assert "kidney beans" in {line["display_name"].lower() for line in plan["lines"]}
