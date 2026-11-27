"""E2E: optional recipe ingredients, from the saved recipe to the list (ADR 0025).

Each test saves a recipe through ``/api/v1/recipes/save``, asks ``/api/v2``
for a plan, answers the confirmation requirement the way OpenClaw would
after asking a member, and reads the plan and its export. Nothing is
mocked, and every test runs on both dialects.
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

import pytest

if TYPE_CHECKING:
    from collections.abc import Callable

    from tests.e2e.conftest import V2

pytestmark = pytest.mark.e2e

ORIGINAL = "Original Ruffles"
CHEDDAR = "Cheddar Cheese Ruffles"
SALSA = "Casa Sanchez Medium Salsa"
CHIPS = "Casa Sanchez Tortilla Chips"


def _ingredient(name: str, unit: str, *, optional: bool = False) -> dict[str, Any]:
    """Return one ingredient payload with an explicit quantity of one."""
    payload: dict[str, Any] = {
        "ingredient": name,
        "quantity": 1,
        "unit": unit,
        "category": "pantry_dry",
    }
    if optional:
        payload["is_optional"] = True
    return payload


def _save(
    signed_request: Callable[..., Any], name: str, ingredients: list[dict[str, Any]]
) -> int:
    """Save a recipe through the v1 API and return its id."""
    response = signed_request(
        "POST",
        "/api/v1/recipes/save",
        {"name": name, "servings": 2, "ingredients": ingredients},
    )
    assert response.status_code == 201, response.get_data(as_text=True)
    recipe_id: int = response.get_json()["id"]
    return recipe_id


def _sandwiches(signed_request: Callable[..., Any], name: str) -> int:
    """Save a sandwich recipe with an optional choice of two bags of Ruffles."""
    return _save(
        signed_request,
        name,
        [
            _ingredient("sandwich rolls", "package"),
            _ingredient(ORIGINAL, "bag", optional=True),
            _ingredient(CHEDDAR, "bag", optional=True),
        ],
    )


def _quesadilla(signed_request: Callable[..., Any], name: str) -> int:
    """Save a quesadilla recipe that optionally offers salsa and chips."""
    return _save(
        signed_request,
        name,
        [
            _ingredient("flour tortillas", "package"),
            _ingredient(SALSA, "jar", optional=True),
            _ingredient(CHIPS, "bag", optional=True),
        ],
    )


def _requirement(v2: V2, recipe_id: int) -> dict[str, Any]:
    """Ask for a plan undecided and return the meal's confirmation requirement."""
    response = v2.post("/plans", {"meals": [{"recipe_id": recipe_id}]})
    error = response.get_json()["error"]
    assert (response.status_code, error["code"]) == (
        409,
        "optional_decisions_required",
    )
    (meal,) = error["details"]["meals"]
    requirement: dict[str, Any] = meal
    return requirement


def _answer(requirement: dict[str, Any], wanted: set[str]) -> dict[str, Any]:
    """Answer every optional ingredient: include the wanted, exclude the rest."""
    return {
        "recipe_id": requirement["recipe_id"],
        "optional_token": requirement["optional_token"],
        "optional_decisions": [
            {
                "ingredient_id": choice["ingredient_id"],
                "include": choice["ingredient"] in wanted,
            }
            for choice in requirement["optional_ingredients"]
        ],
    }


def _line_names(plan: dict[str, Any]) -> set[str]:
    """Return the display names on a plan, lowercased."""
    return {line["display_name"].lower() for line in plan["lines"]}


@pytest.mark.parametrize("recipe", ["Chicken Sandwiches", "Pulled Chicken Sandwiches"])
@pytest.mark.parametrize(
    "wanted",
    [set(), {ORIGINAL}, {CHEDDAR}],
    ids=["neither", "original", "cheddar"],
)
def test_sandwiches_ask_which_ruffles_and_plan_only_the_answer(
    signed_request: Callable[..., Any], v2: V2, recipe: str, wanted: set[str]
) -> None:
    """Scenarios A and B: planning asks about each bag and assumes neither."""
    recipe_id = _sandwiches(signed_request, recipe)

    requirement = _requirement(v2, recipe_id)
    nothing_yet = v2.get("/plans").get_json()
    plan = v2.ok("POST", "/plans", {"meals": [_answer(requirement, wanted)]})
    export = v2.ok("GET", f"/plans/{plan['version']}/export")

    assert requirement["recipe"] == recipe
    assert [
        (choice["ingredient"], choice["quantity"], choice["unit"])
        for choice in requirement["optional_ingredients"]
    ] == [(ORIGINAL, 1.0, "bag"), (CHEDDAR, 1.0, "bag")]
    assert nothing_yet == {"plans": []}
    assert _line_names(plan) == {"sandwich rolls"} | {name.lower() for name in wanted}
    for name in (ORIGINAL, CHEDDAR):
        assert (name.lower() in export["content"].lower()) is (name in wanted)
    for line in plan["lines"]:
        assert line["amounts"] == [
            {
                "quantity": 1.0,
                "unit": "bag"
                if "ruffles" in line["display_name"].lower()
                else "package",
            }
        ]


@pytest.mark.parametrize("recipe", ["Chicken Quesadilla", "Carne Asada Quesadilla"])
@pytest.mark.parametrize(
    "wanted",
    [set(), {SALSA}, {CHIPS}, {SALSA, CHIPS}],
    ids=["neither", "salsa", "chips", "both"],
)
def test_quesadillas_offer_salsa_and_chips_each_on_its_own(
    signed_request: Callable[..., Any], v2: V2, recipe: str, wanted: set[str]
) -> None:
    """Scenario C: each of the two is included or excluded by its own answer."""
    recipe_id = _quesadilla(signed_request, recipe)

    requirement = _requirement(v2, recipe_id)
    plan = v2.ok("POST", "/plans", {"meals": [_answer(requirement, wanted)]})

    assert [c["ingredient"] for c in requirement["optional_ingredients"]] == [
        SALSA,
        CHIPS,
    ]
    assert _line_names(plan) == {"flour tortillas"} | {name.lower() for name in wanted}
    assert plan["is_current"] is True


def test_a_recipe_with_nothing_optional_plans_in_one_step(
    signed_request: Callable[..., Any], v2: V2
) -> None:
    """Scenarios D and F: no new field sent, no requirement, the same plan shape."""
    recipe_id = _save(signed_request, "Plain Rolls", [_ingredient("rolls", "package")])

    response = v2.post("/plans", {"meals": [{"recipe_id": recipe_id}]})

    plan = response.get_json()
    assert response.status_code == 201
    assert plan["meals"] == [{"recipe_id": recipe_id, "servings": 2}]
    assert _line_names(plan) == {"rolls"}


def test_an_incomplete_answer_never_becomes_the_current_plan(
    signed_request: Callable[..., Any], v2: V2
) -> None:
    """Scenario G: one decision of two leaves the earlier plan current."""
    plain = _save(signed_request, "Plain Rolls", [_ingredient("rolls", "package")])
    earlier = v2.ok("POST", "/plans", {"meals": [{"recipe_id": plain}]})
    recipe_id = _quesadilla(signed_request, "Chicken Quesadilla")
    answer = _answer(_requirement(v2, recipe_id), {SALSA})
    answer["optional_decisions"].pop()

    refused = v2.post("/plans", {"meals": [answer]})

    error = refused.get_json()["error"]
    assert (refused.status_code, error["code"]) == (409, "optional_decisions_required")
    assert CHIPS in error["message"]
    assert v2.ok("GET", "/plans/latest")["version"] == earlier["version"]
    assert len(v2.ok("GET", "/plans")["plans"]) == 1


def test_two_meals_are_asked_about_together_and_answered_apart(
    signed_request: Callable[..., Any], v2: V2
) -> None:
    """The requirement groups choices by meal, and the plan merges what is kept."""
    chicken = _quesadilla(signed_request, "Chicken Quesadilla")
    carne = _quesadilla(signed_request, "Carne Asada Quesadilla")

    asked = v2.post("/plans", {"meals": [{"recipe_id": chicken}, {"recipe_id": carne}]})
    by_recipe = {
        meal["recipe"]: meal for meal in asked.get_json()["error"]["details"]["meals"]
    }
    plan = v2.ok(
        "POST",
        "/plans",
        {
            "meals": [
                _answer(by_recipe["Chicken Quesadilla"], {SALSA}),
                _answer(by_recipe["Carne Asada Quesadilla"], {SALSA, CHIPS}),
            ]
        },
    )

    lines = {line["display_name"].lower(): line for line in plan["lines"]}
    assert list(by_recipe) == ["Chicken Quesadilla", "Carne Asada Quesadilla"]
    assert lines[SALSA.lower()]["amounts"] == [{"quantity": 2.0, "unit": "jar"}]
    assert lines[CHIPS.lower()]["amounts"] == [{"quantity": 1.0, "unit": "bag"}]
    assert len(lines[SALSA.lower()]["reasons"]) == 2


def test_the_older_preview_refuses_to_decide_for_the_caller(
    signed_request: Callable[..., Any], no_claude: None
) -> None:
    """A saved optional ingredient reaches the v1 preview flagged, and is refused."""
    _quesadilla(signed_request, "Chicken Quesadilla")
    parsed = signed_request(
        "POST", "/api/v1/meals/parse", {"text": "Chicken Quesadilla"}
    ).get_json()["meals"]

    refused = signed_request("POST", "/api/v1/shopping-list/preview", {"meals": parsed})
    for meal in parsed:
        meal["purchase_items"] = [
            {**item, "is_optional": False}
            for item in meal["purchase_items"]
            if item["ingredient"] != CHIPS
        ]
    decided = signed_request("POST", "/api/v1/shopping-list/preview", {"meals": parsed})

    assert refused.status_code == 409
    assert refused.get_json()["optional_ingredients"] == {
        "Chicken Quesadilla": [SALSA, CHIPS]
    }
    assert decided.status_code == 200
    names = {item["ingredient"] for item in decided.get_json()["items"]}
    assert SALSA in names
    assert CHIPS not in names
