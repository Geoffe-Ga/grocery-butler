"""Tests for versioned, deterministic shopping plans (ADR 0011, issue #143)."""

from __future__ import annotations

import datetime as dt
import itertools
import os
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from grocery_butler.db import get_connection, init_db
from grocery_butler.household_items import HouseholdItemStore
from grocery_butler.models import (
    HouseholdItemUpdate,
    Ingredient,
    IngredientCategory,
    NeedReasonKind,
    NeedStatus,
    ParsedMeal,
    PlanMeal,
    Unit,
)
from grocery_butler.recipe_store import RecipeStore
from grocery_butler.replenishment import INVENTORY_SOURCE, ReplenishmentNeeds
from grocery_butler.shopping_plans import (
    PlanNotFoundError,
    RecipeNotFoundError,
    ShoppingPlans,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from grocery_butler.models import ShoppingPlan

NOW = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.UTC)
ACTOR = "geoff@rubotpaul"


class FakeClock:
    """A clock tests move by hand."""

    def __init__(self) -> None:
        """Start at NOW."""
        self.now = NOW

    def __call__(self) -> dt.datetime:
        """Return the current fake time."""
        return self.now


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a migrated temporary database path."""
    path = str(tmp_path / "plans.db")
    init_db(path)
    return path


@pytest.fixture()
def clock() -> FakeClock:
    """Return a clock fixed at NOW."""
    return FakeClock()


@pytest.fixture()
def plans(db_path: str, clock: FakeClock) -> ShoppingPlans:
    """Return a plan service driven by the fake clock."""
    return ShoppingPlans(db_path, clock=clock)


@pytest.fixture()
def needs(db_path: str, clock: FakeClock) -> ReplenishmentNeeds:
    """Return a needs service sharing the fake clock."""
    return ReplenishmentNeeds(db_path, clock=clock)


def _ingredient(
    name: str,
    quantity: float,
    unit: Unit,
    category: IngredientCategory = IngredientCategory.OTHER,
    *,
    pantry: bool = False,
) -> Ingredient:
    """Build a recipe ingredient."""
    return Ingredient(
        ingredient=name,
        quantity=quantity,
        unit=unit,
        category=category,
        is_pantry_item=pantry,
    )


def _recipe(
    db_path: str, name: str, servings: int, ingredients: list[Ingredient]
) -> int:
    """Save a recipe and return its id."""
    return RecipeStore(db_path).save_recipe(
        ParsedMeal(
            name=name,
            servings=servings,
            known_recipe=True,
            needs_confirmation=False,
            purchase_items=[i for i in ingredients if not i.is_pantry_item],
            pantry_items=[i for i in ingredients if i.is_pantry_item],
        )
    )


def _approved_need(
    db_path: str, needs: ReplenishmentNeeds, name: str, kind: NeedReasonKind
) -> int:
    """Create an item with an approved need and return the item id."""
    item = HouseholdItemStore(db_path).resolve_or_create(name, actor="t")
    need = needs.add_reason(item.id, kind, source_key=INVENTORY_SOURCE, actor="system")
    needs.transition(need.id, NeedStatus.APPROVED, actor=ACTOR)
    return item.id


def _line(plan: ShoppingPlan, name: str) -> int:
    """Return the index of the line with a display name."""
    return [line.display_name for line in plan.lines].index(name)


class TestBuildFromNeeds:
    """A plan works from replenishment needs alone (ADR 0004)."""

    def test_approved_need_becomes_a_line_with_its_reason(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """One approved need, one line, no amount: buy the usual."""
        item_id = _approved_need(db_path, needs, "oat milk", NeedReasonKind.OUT)

        plan = plans.build([], actor=ACTOR)

        assert plan.version == 1
        assert plan.created_by == ACTOR
        assert plan.created_at == NOW
        assert plan.meals == []
        assert len(plan.lines) == 1
        line = plan.lines[0]
        assert line.item_id == item_id
        assert line.display_name == "Oat Milk"
        assert line.amounts == []
        assert [r.kind for r in line.reasons] == [NeedReasonKind.OUT]
        assert line.need_id is not None

    def test_only_approved_and_planned_needs_are_planned(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """A suggestion nobody approved is not on the plan."""
        items = HouseholdItemStore(db_path)
        suggested = items.resolve_or_create("eggs", actor="t")
        needs.add_reason(
            suggested.id, NeedReasonKind.LOW_STOCK, source_key="inventory", actor="s"
        )
        deferred = items.resolve_or_create("rice", actor="t")
        need = needs.add_reason(
            deferred.id, NeedReasonKind.LOW_STOCK, source_key="inventory", actor="s"
        )
        needs.transition(need.id, NeedStatus.DEFERRED, actor=ACTOR)
        _approved_need(db_path, needs, "oat milk", NeedReasonKind.OUT)

        plan = plans.build([], actor=ACTOR)

        assert [line.display_name for line in plan.lines] == ["Oat Milk"]

    def test_building_marks_the_needs_planned(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """A need on a stored plan is planned, attributed to the builder."""
        item_id = _approved_need(db_path, needs, "oat milk", NeedReasonKind.OUT)

        plans.build([], actor=ACTOR)

        need = needs.open_need_for_item(item_id)
        assert need is not None
        assert need.status is NeedStatus.PLANNED
        assert needs.history(need.id)[-1].actor == ACTOR

    def test_withdrawn_reasons_are_left_off_the_line(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """Only reasons that still hold are listed."""
        item_id = _approved_need(db_path, needs, "oat milk", NeedReasonKind.LOW_STOCK)
        needs.add_reason(
            item_id, NeedReasonKind.MANUAL_REQUEST, source_key="member:x", actor=ACTOR
        )
        needs.withdraw_reason(
            item_id, NeedReasonKind.LOW_STOCK, source_key=INVENTORY_SOURCE, actor="s"
        )

        plan = plans.build([], actor=ACTOR)

        assert [r.kind for r in plan.lines[0].reasons] == [
            NeedReasonKind.MANUAL_REQUEST
        ]

    def test_item_preferences_become_constraints(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """An adapter gets the brand and size from the line, not the household."""
        item_id = _approved_need(db_path, needs, "peanut butter", NeedReasonKind.OUT)
        HouseholdItemStore(db_path).update(
            item_id,
            HouseholdItemUpdate(preferred_brand="Adams", preferred_size="16 oz"),
            actor=ACTOR,
        )

        plan = plans.build([], actor=ACTOR)

        assert plan.lines[0].constraints == {
            "preferred_brand": "Adams",
            "preferred_size": "16 oz",
        }

    def test_nothing_to_buy_is_an_empty_first_version(
        self, plans: ShoppingPlans
    ) -> None:
        """An empty plan is still a plan."""
        plan = plans.build([], actor=ACTOR)

        assert (plan.version, plan.lines, plan.unplanned) == (1, [], [])


class TestBuildFromMeals:
    """Recipes add stated amounts and their own reasons."""

    def test_recipe_ingredients_are_scaled_and_listed(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """Amounts scale with servings; the recipe is the reason."""
        recipe_id = _recipe(
            db_path,
            "Pancakes",
            2,
            [
                _ingredient("buttermilk", 1.0, Unit.CUP, IngredientCategory.DAIRY),
                _ingredient("blueberries", 6.0, Unit.OZ, IngredientCategory.PRODUCE),
            ],
        )

        plan = plans.build([PlanMeal(recipe_id=recipe_id, servings=4)], actor=ACTOR)

        buttermilk = plan.lines[_line(plan, "Buttermilk")]
        assert plan.meals == [PlanMeal(recipe_id=recipe_id, servings=4)]
        assert [(a.quantity, a.unit) for a in buttermilk.amounts] == [(2.0, Unit.CUP)]
        assert buttermilk.need_id is None
        reason = buttermilk.reasons[0]
        assert reason.kind is NeedReasonKind.RECIPE_DEMAND
        assert reason.source_key == f"recipe:{recipe_id}"
        assert reason.detail == {
            "recipe": "Pancakes",
            "quantity": 2.0,
            "unit": "cup",
            "servings": 4,
        }

    def test_servings_default_to_the_recipes_own(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """No servings given, no scaling."""
        recipe_id = _recipe(
            db_path, "Pancakes", 2, [_ingredient("buttermilk", 1.0, Unit.CUP)]
        )

        plan = plans.build([PlanMeal(recipe_id=recipe_id)], actor=ACTOR)

        assert plan.meals == [PlanMeal(recipe_id=recipe_id, servings=2)]
        assert plan.lines[0].amounts[0].quantity == 1.0

    def test_one_item_wanted_by_two_recipes_and_a_need_is_one_line(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """The acceptance test: one line, every reason, compatible units summed."""
        item_id = _approved_need(db_path, needs, "buttermilk", NeedReasonKind.LOW_STOCK)
        pancakes = _recipe(
            db_path, "Pancakes", 2, [_ingredient("Buttermilk", 2.0, Unit.CUP)]
        )
        biscuits = _recipe(
            db_path, "Biscuits", 4, [_ingredient("buttermilk", 1.0, Unit.PINT)]
        )

        plan = plans.build(
            [PlanMeal(recipe_id=pancakes), PlanMeal(recipe_id=biscuits)], actor=ACTOR
        )

        assert len(plan.lines) == 1
        line = plan.lines[0]
        assert line.item_id == item_id
        assert [(a.quantity, a.unit) for a in line.amounts] == [(4.0, Unit.CUP)]
        assert [(r.kind, r.source_key) for r in line.reasons] == [
            (NeedReasonKind.LOW_STOCK, INVENTORY_SOURCE),
            (NeedReasonKind.RECIPE_DEMAND, f"recipe:{pancakes}"),
            (NeedReasonKind.RECIPE_DEMAND, f"recipe:{biscuits}"),
        ]

    def test_incompatible_units_stay_separate_amounts_on_one_line(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """A jar is never added to cups, but the item is still one line."""
        curry = _recipe(db_path, "Curry", 2, [_ingredient("yogurt", 1.0, Unit.CUP)])
        parfait = _recipe(db_path, "Parfait", 2, [_ingredient("yogurt", 1.0, Unit.JAR)])

        plan = plans.build(
            [PlanMeal(recipe_id=curry), PlanMeal(recipe_id=parfait)], actor=ACTOR
        )

        assert len(plan.lines) == 1
        assert [(a.quantity, a.unit) for a in plan.lines[0].amounts] == [
            (1.0, Unit.CUP),
            (1.0, Unit.JAR),
        ]

    def test_pantry_ingredients_add_nothing_on_their_own(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """A recipe's pantry item, or a household staple, is assumed on hand."""
        recipe_id = _recipe(
            db_path,
            "Toast",
            2,
            [
                _ingredient("bread", 4.0, Unit.EACH),
                _ingredient("cinnamon", 1.0, Unit.TSP, pantry=True),
                _ingredient("butter", 2.0, Unit.TBSP),
            ],
        )

        plan = plans.build([PlanMeal(recipe_id=recipe_id)], actor=ACTOR)

        assert [line.display_name for line in plan.lines] == ["Bread"]

    def test_low_staple_reaches_the_plan_through_its_need(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """Inventory overrides the staple exclusion, by way of an approved need."""
        _approved_need(db_path, needs, "butter", NeedReasonKind.OUT)
        recipe_id = _recipe(
            db_path, "Toast", 2, [_ingredient("butter", 2.0, Unit.TBSP)]
        )

        plan = plans.build([PlanMeal(recipe_id=recipe_id)], actor=ACTOR)

        assert [line.display_name for line in plan.lines] == ["Butter"]
        assert [r.kind for r in plan.lines[0].reasons] == [NeedReasonKind.OUT]
        assert plan.lines[0].amounts == []

    def test_merged_items_share_a_line(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """Demand follows the canonical item."""
        soup = _recipe(db_path, "Soup", 2, [_ingredient("tomato", 2.0, Unit.EACH)])
        salad = _recipe(db_path, "Salad", 2, [_ingredient("tomatoes", 3.0, Unit.EACH)])
        items = HouseholdItemStore(db_path)
        tomato = items.resolve("tomato")
        tomatoes = items.resolve("tomatoes")
        assert tomato is not None
        assert tomatoes is not None
        items.merge(tomatoes.id, tomato.id, actor="t")

        plan = plans.build(
            [PlanMeal(recipe_id=soup), PlanMeal(recipe_id=salad)], actor=ACTOR
        )

        assert [(line.item_id, line.amounts[0].quantity) for line in plan.lines] == [
            (tomato.id, 5.0)
        ]

    def test_ingredient_without_an_item_is_reported_not_dropped(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """A recipe row that was never linked is listed as unplanned."""
        recipe_id = _recipe(db_path, "Soup", 2, [_ingredient("leeks", 2.0, Unit.EACH)])
        with closing(get_connection(db_path)) as conn:
            conn.execute(
                "UPDATE recipe_ingredients SET item_id = NULL WHERE recipe_id = ?",
                (recipe_id,),
            )
            conn.commit()

        plan = plans.build([PlanMeal(recipe_id=recipe_id)], actor=ACTOR)

        assert plan.lines == []
        assert [(u.recipe_id, u.ingredient) for u in plan.unplanned] == [
            (recipe_id, "leeks")
        ]
        assert "household item" in plan.unplanned[0].problem

    def test_unknown_recipe_is_refused(self, plans: ShoppingPlans) -> None:
        """A plan can't shop for a recipe that doesn't exist."""
        with pytest.raises(RecipeNotFoundError, match="99999"):
            plans.build([PlanMeal(recipe_id=99999)], actor=ACTOR)

        assert plans.latest() is None

    def test_the_same_recipe_twice_is_refused(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """Say how many servings instead of listing a meal twice."""
        recipe_id = _recipe(db_path, "Soup", 2, [_ingredient("leeks", 2.0, Unit.EACH)])

        with pytest.raises(ValueError, match="once"):
            plans.build(
                [PlanMeal(recipe_id=recipe_id), PlanMeal(recipe_id=recipe_id)],
                actor=ACTOR,
            )


class TestDeterminismAndVersions:
    """Same inputs, same plan; any change, a new version."""

    def _household(self, db_path: str, needs: ReplenishmentNeeds) -> list[PlanMeal]:
        """Set up needs and recipes, returning the meals to plan."""
        _approved_need(db_path, needs, "oat milk", NeedReasonKind.OUT)
        _approved_need(db_path, needs, "eggs", NeedReasonKind.LOW_STOCK)
        pancakes = _recipe(
            db_path,
            "Pancakes",
            2,
            [
                _ingredient("eggs", 2.0, Unit.EACH, IngredientCategory.DAIRY),
                _ingredient("blueberries", 6.0, Unit.OZ, IngredientCategory.PRODUCE),
            ],
        )
        omelette = _recipe(
            db_path, "Omelette", 1, [_ingredient("eggs", 3.0, Unit.EACH)]
        )
        return [
            PlanMeal(recipe_id=pancakes, servings=4),
            PlanMeal(recipe_id=omelette),
        ]

    def test_building_twice_returns_the_same_version(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """Nothing changed, so nothing new is stored."""
        meals = self._household(db_path, needs)

        first = plans.build(meals, actor=ACTOR)
        second = plans.build(meals, actor="jess@rubotpaul")

        assert second.version == first.version == 1
        assert second.content_hash == first.content_hash
        assert second.created_by == ACTOR
        assert len(plans.list_plans()) == 1

    def test_meal_order_does_not_change_the_plan(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """Every ordering of the same meals hashes the same."""
        meals = self._household(db_path, needs)

        hashes = {
            plans.preview(list(order)).content_hash
            for order in itertools.permutations(meals)
        }
        first = plans.build(meals, actor=ACTOR)
        reversed_build = plans.build(list(reversed(meals)), actor=ACTOR)

        assert hashes == {first.content_hash}
        assert reversed_build.version == first.version

    def test_lines_are_in_a_stable_order(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """Lines sort by category, then name, whatever order they arrived in."""
        meals = self._household(db_path, needs)

        plan = plans.build(meals, actor=ACTOR)

        keys = [(str(line.category or ""), line.display_name) for line in plan.lines]
        assert keys == sorted(keys)
        assert len(keys) == 3

    def test_a_new_need_produces_the_next_version_and_keeps_the_old(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """Marking milk out makes version N+1; version N stays readable."""
        meals = self._household(db_path, needs)
        first = plans.build(meals, actor=ACTOR)
        _approved_need(db_path, needs, "milk", NeedReasonKind.OUT)

        second = plans.build(meals, actor=ACTOR)

        assert second.version == 2
        assert second.content_hash != first.content_hash
        assert len(second.lines) == len(first.lines) + 1
        assert plans.get(1) == first
        assert plans.latest() == second
        assert [p.version for p in plans.list_plans()] == [1, 2]

    def test_changing_servings_or_meals_produces_a_new_version(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """Meal inputs are part of the content."""
        meals = self._household(db_path, needs)
        plans.build(meals, actor=ACTOR)

        more = plans.build(
            [meals[0].model_copy(update={"servings": 8}), meals[1]], actor=ACTOR
        )
        fewer = plans.build([meals[1]], actor=ACTOR)

        assert (more.version, fewer.version) == (2, 3)

    def test_a_stored_version_is_never_changed_by_later_builds(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """Versions are immutable once stored."""
        meals = self._household(db_path, needs)
        first = plans.build(meals, actor=ACTOR)
        HouseholdItemStore(db_path).update(
            first.lines[0].item_id,
            HouseholdItemUpdate(display_name="Renamed"),
            actor=ACTOR,
        )

        plans.build(meals, actor=ACTOR)

        assert plans.get(1) == first

    def test_two_builders_racing_for_a_version_number_both_get_one(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """A builder that lost the race for version N stores N+1 instead."""
        meals = self._household(db_path, needs)
        first = plans.build(meals, actor=ACTOR)
        _approved_need(db_path, needs, "milk", NeedReasonKind.OUT)

        # This builder worked out "the next version is 1" just before the
        # other one stored version 1.
        draft_version = plans._store(
            plans._assemble(meals), version=first.version, actor=ACTOR
        )

        assert draft_version == 2
        assert [p.version for p in plans.list_plans()] == [1, 2]
        assert plans.get(1) == first

    def test_fingerprint_follows_the_state_under_a_plan(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """An unchanged plan is current; a changed household makes it stale."""
        meals = self._household(db_path, needs)
        plan = plans.build(meals, actor=ACTOR)

        before = plans.current_fingerprint(plan.version)
        _approved_need(db_path, needs, "milk", NeedReasonKind.OUT)
        after = plans.current_fingerprint(plan.version)

        assert before == plan.content_hash
        assert after != plan.content_hash

    def test_missing_version(self, plans: ShoppingPlans) -> None:
        """Unknown versions are reported."""
        assert plans.get(7) is None
        with pytest.raises(PlanNotFoundError, match="7"):
            plans.current_fingerprint(7)


_PG_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.mark.skipif(not _PG_URL, reason="TEST_DATABASE_URL not set")
class TestPostgres:
    """Plans store and read back the same on Postgres."""

    @pytest.fixture()
    def clean(self) -> Iterator[None]:
        """Remove this suite's plan rows before and after."""
        init_db(_PG_URL)
        self._clean()
        yield
        self._clean()

    def _clean(self) -> None:
        """Delete plans this suite created."""
        with closing(get_connection(_PG_URL)) as conn:
            conn.execute(
                "DELETE FROM shopping_plan_lines WHERE plan_id IN"
                " (SELECT id FROM shopping_plans WHERE created_by = ?)",
                ("pg-plan-test",),
            )
            conn.execute(
                "DELETE FROM shopping_plans WHERE created_by = ?", ("pg-plan-test",)
            )
            conn.commit()

    @pytest.mark.usefixtures("clean")
    def test_build_is_stable_and_round_trips(self) -> None:
        """A stored plan reads back equal, and rebuilding it adds nothing."""
        plans = ShoppingPlans(_PG_URL)

        first = plans.build([], actor="pg-plan-test")
        again = plans.build([], actor="pg-plan-test")

        assert again.version == first.version
        assert plans.get(first.version) == first
        assert plans.current_fingerprint(first.version) == first.content_hash
