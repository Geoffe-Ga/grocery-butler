"""E2E: CLI subcommands against a real SQLite database.

The CLI's ``order`` subcommand builds its own ``SafewayPipeline``
directly from ``Config`` with no transport-injection hook, so it is not
exercised here -- the API/pipeline tests in ``test_ordering.py`` and
``test_api_chain.py`` cover that pipeline through the injectable
``safeway_mock`` seam instead.
"""

from __future__ import annotations

import pytest

from grocery_butler import cli
from grocery_butler.models import Ingredient, IngredientCategory, ParsedMeal
from grocery_butler.pantry_manager import PantryManager
from grocery_butler.recipe_store import RecipeStore

pytestmark = pytest.mark.e2e


def _run(argv: list[str]) -> int:
    """Run the CLI and return its exit code.

    Args:
        argv: Argument list to pass to ``cli.main`` (without the program name).

    Returns:
        The process exit code raised via ``SystemExit``.
    """
    with pytest.raises(SystemExit) as exc_info:
        cli.main(argv)
    return int(exc_info.value.code or 0)


def _sample_meal() -> ParsedMeal:
    """Return a minimal ParsedMeal for seeding the recipe store directly.

    Returns:
        A simple two-ingredient meal named "e2e cli meal".
    """
    return ParsedMeal(
        name="e2e cli meal",
        servings=2,
        known_recipe=True,
        needs_confirmation=False,
        purchase_items=[
            Ingredient(
                ingredient="rice",
                quantity=1.0,
                unit="cup",
                category=IngredientCategory.PANTRY_DRY,
            ),
        ],
        pantry_items=[],
    )


def test_stock_lifecycle_and_restock_round_trip(
    db_path: str,
    no_claude_cli: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``stock add/low`` + ``restock`` show/clear round-trip via the real DB."""
    assert _run(["stock", "add", "cumin", "pantry_dry"]) == 0
    assert _run(["stock", "low", "cumin"]) == 0

    capsys.readouterr()
    assert _run(["restock"]) == 0
    restock_output = capsys.readouterr().out
    assert "Cumin" in restock_output

    assert _run(["restock", "clear"]) == 0
    clear_output = capsys.readouterr().out
    assert "Cleared 1 item" in clear_output

    manager = PantryManager(db_path)
    item = manager.get_item("cumin")
    assert item is not None
    assert item.status.value == "on_hand"


def test_recipes_and_pantry_round_trip(
    db_path: str,
    no_claude_cli: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``recipes`` list and ``pantry`` add/list/remove round-trip via the real DB."""
    RecipeStore(db_path).save_recipe(_sample_meal())

    capsys.readouterr()
    assert _run(["recipes"]) == 0
    recipes_output = capsys.readouterr().out
    assert "e2e cli meal" in recipes_output

    assert _run(["pantry", "add", "cumin", "pantry_dry"]) == 0
    assert _run(["pantry"]) == 0
    pantry_output = capsys.readouterr().out
    assert "Cumin" in pantry_output

    assert _run(["pantry", "remove", "cumin"]) == 0
    assert _run(["pantry"]) == 0
    after_output = capsys.readouterr().out
    assert "Cumin" not in after_output


def test_plan_seeded_recipe_prints_deterministic_shopping_list(
    seed_recipe: ParsedMeal,
    no_claude_cli: None,
    capsys: pytest.CaptureFixture[str],
) -> None:
    """``plan <seeded recipe>`` prints a deterministic shopping list."""
    capsys.readouterr()
    assert _run(["plan", seed_recipe.name]) == 0
    output = capsys.readouterr().out
    assert "ground beef" in output
    assert "spaghetti" in output
    assert "tomato sauce" in output
