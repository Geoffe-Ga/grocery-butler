"""Tests for versioned, deterministic shopping plans (ADR 0011, issue #143)."""

from __future__ import annotations

import datetime as dt
import hashlib
import itertools
import json
import os
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from grocery_butler.constraints import HouseholdRules
from grocery_butler.db import get_connection, init_db
from grocery_butler.household_items import HouseholdItemStore
from grocery_butler.models import (
    HardConstraintKind,
    HouseholdItemUpdate,
    Ingredient,
    IngredientCategory,
    NeedReasonKind,
    NeedStatus,
    OptionalDecision,
    OptionalProblemKind,
    ParsedMeal,
    PlanMeal,
    SoftPreferenceKind,
    Unit,
)
from grocery_butler.recipe_store import RecipeStore
from grocery_butler.replenishment import INVENTORY_SOURCE, ReplenishmentNeeds
from grocery_butler.shopping_plans import (
    OptionalDecisionsError,
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
    optional: bool = False,
) -> Ingredient:
    """Build a recipe ingredient."""
    return Ingredient(
        ingredient=name,
        quantity=quantity,
        unit=unit,
        category=category,
        is_pantry_item=pantry,
        is_optional=optional,
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


class TestSoftPreferencesOnLines:
    """A plan line carries every soft preference that applies to it (#215)."""

    def test_household_and_item_preferences_reach_the_line(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """Household-wide ones on every line, item ones on their own line."""
        milk_id = _approved_need(db_path, needs, "oat milk", NeedReasonKind.OUT)
        _approved_need(db_path, needs, "eggs", NeedReasonKind.OUT)
        rules = HouseholdRules(db_path)
        rules.add_soft(SoftPreferenceKind.ORGANIC, "", actor=ACTOR)
        rules.add_soft(SoftPreferenceKind.GENERIC, "", actor=ACTOR, item_id=milk_id)
        rules.add_soft(
            SoftPreferenceKind.AVOID_BRAND, "Silk", actor=ACTOR, item_id=milk_id
        )

        plan = plans.build([], actor=ACTOR)

        assert plan.lines[_line(plan, "Eggs")].constraints == {"organic": True}
        assert plan.lines[_line(plan, "Oat Milk")].constraints == {
            "organic": True,
            "generic": True,
            "avoid_brands": ["Silk"],
        }

    def test_a_verified_product_code_reaches_the_line(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """A code a member confirmed on a receipt travels with the plan."""
        item_id = _approved_need(db_path, needs, "oat milk", NeedReasonKind.OUT)
        items = HouseholdItemStore(db_path)
        with closing(get_connection(db_path)) as conn:
            items.verify_upc_in(
                conn,
                item_id,
                "012345678905",
                actor=ACTOR,
                verified_at="2026-11-26T12:00:00+00:00",
                evidence={"receipt_id": 1, "line_id": 1},
            )
            conn.commit()

        plan = plans.build([], actor=ACTOR)

        assert plan.lines[0].constraints == {"verified_upc": "012345678905"}

    def test_a_new_preference_makes_a_new_plan_version(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """What a line asks for is part of the plan an approval is bound to."""
        _approved_need(db_path, needs, "oat milk", NeedReasonKind.OUT)
        first = plans.build([], actor=ACTOR)

        HouseholdRules(db_path).add_soft(SoftPreferenceKind.ORGANIC, "", actor=ACTOR)
        second = plans.build([], actor=ACTOR)

        assert second.version == first.version + 1
        assert second.content_hash != first.content_hash


class TestHardConstraintsOnLines:
    """A plan line carries every hard constraint that applies to it."""

    def test_household_and_item_constraints_reach_the_line(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """Household-wide ones on every line, item ones on their own line."""
        milk_id = _approved_need(db_path, needs, "oat milk", NeedReasonKind.OUT)
        _approved_need(db_path, needs, "eggs", NeedReasonKind.OUT)
        rules = HouseholdRules(db_path)
        rules.add_hard(HardConstraintKind.ALLERGEN_EXCLUSION, "peanut", actor=ACTOR)
        rules.add_hard(
            HardConstraintKind.DO_NOT_SUBSTITUTE, "", actor=ACTOR, item_id=milk_id
        )

        plan = plans.build([], actor=ACTOR)

        by_name = {
            line.display_name: [
                (c.kind.value, c.value, c.scope) for c in line.hard_constraints
            ]
            for line in plan.lines
        }
        assert by_name == {
            "Eggs": [("allergen_exclusion", "peanut", "household")],
            "Oat Milk": [
                ("allergen_exclusion", "peanut", "household"),
                ("do_not_substitute", "", "item"),
            ],
        }

    def test_hard_constraints_are_kept_out_of_the_preferences_field(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """ADR 0012: a hard constraint can't be read as a preference."""
        item_id = _approved_need(db_path, needs, "oat milk", NeedReasonKind.OUT)
        HouseholdItemStore(db_path).update(
            item_id, HouseholdItemUpdate(preferred_brand="Oatly"), actor=ACTOR
        )
        HouseholdRules(db_path).add_hard(
            HardConstraintKind.ALLERGEN_EXCLUSION, "peanut", actor=ACTOR
        )

        line = plans.build([], actor=ACTOR).lines[0]

        assert line.constraints == {"preferred_brand": "Oatly"}
        assert [c.value for c in line.hard_constraints] == ["peanut"]

    def test_changing_a_hard_constraint_makes_the_plan_stale_and_a_new_version(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """An approval built on the old plan can't survive a new allergen."""
        _approved_need(db_path, needs, "oat milk", NeedReasonKind.OUT)
        first = plans.build([], actor=ACTOR)
        rules = HouseholdRules(db_path)

        constraint = rules.add_hard(
            HardConstraintKind.ALLERGEN_EXCLUSION, "peanut", actor=ACTOR
        )
        stale = plans.current_fingerprint(first.version)
        second = plans.build([], actor=ACTOR)
        rules.deactivate_hard(constraint.id, actor=ACTOR)
        third = plans.build([], actor=ACTOR)

        assert stale != first.content_hash
        assert (second.version, third.version) == (2, 3)
        assert third.lines[0].hard_constraints == []
        assert plans.get(2) == second

    def test_stored_constraints_round_trip(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """What was built is what is read back."""
        _approved_need(db_path, needs, "oat milk", NeedReasonKind.OUT)
        HouseholdRules(db_path).add_hard(
            HardConstraintKind.DIETARY_RULE, "vegetarian", actor=ACTOR
        )

        built = plans.build([], actor=ACTOR)

        assert plans.get(built.version) == built
        assert (
            built.lines[0].hard_constraints[0].kind is HardConstraintKind.DIETARY_RULE
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


def _sandwiches(db_path: str, name: str = "Chicken Sandwiches") -> int:
    """Save a recipe with one required ingredient and two optional ones."""
    return _recipe(
        db_path,
        name,
        2,
        [
            _ingredient("chicken breast", 1.0, Unit.LB, IngredientCategory.MEAT),
            _ingredient(
                "Original Ruffles",
                1.0,
                Unit.BAG,
                IngredientCategory.PANTRY_DRY,
                optional=True,
            ),
            _ingredient(
                "Cheddar Cheese Ruffles",
                1.0,
                Unit.BAG,
                IngredientCategory.PANTRY_DRY,
                optional=True,
            ),
        ],
    )


def _decided(
    plans: ShoppingPlans,
    recipe_id: int,
    include: dict[str, bool],
    servings: int | None = None,
) -> PlanMeal:
    """Answer a recipe's optional ingredients by name, with its current token."""
    (choices,) = plans.optional_choices(
        [PlanMeal(recipe_id=recipe_id, servings=servings)]
    )
    ids = {c.ingredient: c.ingredient_id for c in choices.optional_ingredients}
    return PlanMeal(
        recipe_id=recipe_id,
        servings=servings,
        optional_token=choices.optional_token,
        optional_decisions=[
            OptionalDecision(ingredient_id=ids[name], include=wanted)
            for name, wanted in include.items()
        ],
    )


def _refusal(plans: ShoppingPlans, meals: list[PlanMeal]) -> OptionalDecisionsError:
    """Build a plan that must be refused, and return the refusal."""
    with pytest.raises(OptionalDecisionsError) as caught:
        plans.build(meals, actor=ACTOR)
    return caught.value


class TestOptionalIngredients:
    """An optional ingredient is planned only on a member's say-so (ADR 0025)."""

    def test_a_recipe_without_optional_ingredients_hashes_as_it_always_did(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """Plans stored before optional ingredients existed stay current.

        The content written out below, key for key, is what the builder
        hashed for this recipe before this feature. If the hash moves,
        every stored plan turns stale and every pending approval is voided.
        """
        porridge = _recipe(
            db_path,
            "Porridge",
            2,
            [_ingredient("oats", 1.0, Unit.CUP, IngredientCategory.PANTRY_DRY)],
        )

        plan = plans.build([PlanMeal(recipe_id=porridge, servings=4)], actor=ACTOR)

        content_before_this_feature = {
            "meals": [{"recipe_id": porridge, "servings": 4}],
            "lines": [
                {
                    "item_id": plan.lines[0].item_id,
                    "display_name": "Oats",
                    "category": "pantry_dry",
                    "amounts": [{"quantity": 2.0, "unit": "cup"}],
                    "reasons": [
                        {
                            "kind": "recipe_demand",
                            "source_key": f"recipe:{porridge}",
                            "detail": {
                                "recipe": "Porridge",
                                "quantity": 2.0,
                                "unit": "cup",
                                "servings": 4,
                            },
                        }
                    ],
                    "constraints": {},
                    "need_id": None,
                    "hard_constraints": [],
                }
            ],
            "unplanned": [],
        }
        canonical = json.dumps(
            content_before_this_feature, sort_keys=True, separators=(",", ":")
        )
        assert plan.content_hash == hashlib.sha256(canonical.encode()).hexdigest()
        assert plan.model_dump(mode="json")["meals"] == [
            {"recipe_id": porridge, "servings": 4}
        ]
        assert plans.optional_choices(plan.meals) == []

    def test_undecided_optional_ingredients_refuse_the_build_and_store_nothing(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """Nothing is assumed: no plan, and no need is moved to planned."""
        item_id = _approved_need(db_path, needs, "oat milk", NeedReasonKind.OUT)
        recipe_id = _sandwiches(db_path)

        error = _refusal(plans, [PlanMeal(recipe_id=recipe_id)])

        assert error.kind is OptionalProblemKind.MISSING
        assert plans.list_plans() == []
        need = needs.open_need_for_item(item_id)
        assert need is not None
        assert need.status is NeedStatus.APPROVED
        assert "Chicken Sandwiches" in str(error)
        assert "Original Ruffles" in str(error)
        assert "Cheddar Cheese Ruffles" in str(error)

    def test_the_refusal_lists_each_choice_with_its_stated_amount(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """The amounts are the recipe's own, scaled to the servings asked for."""
        recipe_id = _sandwiches(db_path)

        error = _refusal(plans, [PlanMeal(recipe_id=recipe_id, servings=4)])

        (meal,) = error.meals
        assert (meal.recipe_id, meal.recipe, meal.servings) == (
            recipe_id,
            "Chicken Sandwiches",
            4,
        )
        assert [
            (c.ingredient, c.quantity, c.unit, c.category, c.is_pantry_item)
            for c in meal.optional_ingredients
        ] == [
            ("Original Ruffles", 2.0, Unit.BAG, IngredientCategory.PANTRY_DRY, False),
            (
                "Cheddar Cheese Ruffles",
                2.0,
                Unit.BAG,
                IngredientCategory.PANTRY_DRY,
                False,
            ),
        ]
        assert [(p.recipe_id, p.ingredient_id, p.problem) for p in error.problems] == [
            (recipe_id, c.ingredient_id, OptionalProblemKind.MISSING)
            for c in meal.optional_ingredients
        ]

    def test_the_same_question_always_carries_the_same_token(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """The requirement is deterministic, so asking twice is harmless."""
        recipe_id = _sandwiches(db_path)
        meals = [PlanMeal(recipe_id=recipe_id)]

        first = plans.optional_choices(meals)
        second = _refusal(plans, meals).meals

        assert first == second
        assert len(first[0].optional_token) == 64

    @pytest.mark.parametrize(
        ("include", "expected"),
        [
            ({"Original Ruffles": False, "Cheddar Cheese Ruffles": False}, []),
            (
                {"Original Ruffles": True, "Cheddar Cheese Ruffles": False},
                ["Original Ruffles"],
            ),
            (
                {"Original Ruffles": False, "Cheddar Cheese Ruffles": True},
                ["Cheddar Cheese Ruffles"],
            ),
            (
                {"Original Ruffles": True, "Cheddar Cheese Ruffles": True},
                ["Cheddar Cheese Ruffles", "Original Ruffles"],
            ),
        ],
        ids=["neither", "original", "cheddar", "both"],
    )
    def test_only_included_optional_ingredients_reach_the_plan(
        self,
        plans: ShoppingPlans,
        db_path: str,
        include: dict[str, bool],
        expected: list[str],
    ) -> None:
        """Each answer is honoured: included lines appear, excluded ones don't."""
        recipe_id = _sandwiches(db_path)

        plan = plans.build([_decided(plans, recipe_id, include)], actor=ACTOR)

        names = sorted(line.display_name for line in plan.lines)
        assert names == sorted(["Chicken Breast", *expected])
        for name in expected:
            line = plan.lines[_line(plan, name)]
            assert [(a.quantity, a.unit) for a in line.amounts] == [(1.0, Unit.BAG)]
            assert [r.kind for r in line.reasons] == [NeedReasonKind.RECIPE_DEMAND]

    def test_the_plan_records_the_decisions_it_was_built_from(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """A stored plan says what was chosen, in a stable order, with no token."""
        recipe_id = _sandwiches(db_path)
        meal = _decided(
            plans,
            recipe_id,
            {"Cheddar Cheese Ruffles": True, "Original Ruffles": False},
        )

        plan = plans.build([meal], actor=ACTOR)

        stored = plans.get(plan.version)
        assert stored == plan
        (planned,) = plan.model_dump(mode="json")["meals"]
        ids = sorted(d.ingredient_id for d in meal.optional_decisions)
        assert planned == {
            "recipe_id": recipe_id,
            "servings": 2,
            "optional_decisions": [
                {"ingredient_id": ids[0], "include": False},
                {"ingredient_id": ids[1], "include": True},
            ],
        }

    def test_an_included_optional_ingredient_is_planned_like_a_required_one(
        self, plans: ShoppingPlans, needs: ReplenishmentNeeds, db_path: str
    ) -> None:
        """It merges with other demand and carries the item's hard constraints."""
        item_id = _approved_need(db_path, needs, "salsa", NeedReasonKind.LOW_STOCK)
        HouseholdRules(db_path).add_hard(
            HardConstraintKind.DO_NOT_SUBSTITUTE, "", actor=ACTOR, item_id=item_id
        )
        tacos = _recipe(db_path, "Tacos", 2, [_ingredient("salsa", 1.0, Unit.JAR)])
        quesadilla = _recipe(
            db_path,
            "Chicken Quesadilla",
            2,
            [
                _ingredient("tortillas", 1.0, Unit.PACKAGE),
                _ingredient("salsa", 1.0, Unit.JAR, optional=True),
            ],
        )

        plan = plans.build(
            [
                PlanMeal(recipe_id=tacos),
                _decided(plans, quesadilla, {"salsa": True}),
            ],
            actor=ACTOR,
        )

        line = plan.lines[_line(plan, "Salsa")]
        assert line.item_id == item_id
        assert [(a.quantity, a.unit) for a in line.amounts] == [(2.0, Unit.JAR)]
        assert sorted(r.source_key for r in line.reasons) == [
            "inventory",
            f"recipe:{tacos}",
            f"recipe:{quesadilla}",
        ]
        assert [c.kind for c in line.hard_constraints] == [
            HardConstraintKind.DO_NOT_SUBSTITUTE
        ]
        assert line.need_id is not None

    def test_an_included_optional_pantry_item_adds_nothing_like_a_required_one(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """It is still asked about, and still left to the pantry."""
        recipe_id = _recipe(
            db_path,
            "Toast",
            1,
            [
                _ingredient("bread", 2.0, Unit.SLICE),
                _ingredient("butter", 1.0, Unit.TBSP, pantry=True, optional=True),
            ],
        )

        asked = _refusal(plans, [PlanMeal(recipe_id=recipe_id)])
        plan = plans.build([_decided(plans, recipe_id, {"butter": True})], actor=ACTOR)

        assert [c.is_pantry_item for c in asked.meals[0].optional_ingredients] == [True]
        assert [line.display_name for line in plan.lines] == ["Bread"]

    def test_meals_are_grouped_and_every_meal_must_be_answered(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """Answering one meal does not answer another."""
        first = _sandwiches(db_path)
        second = _sandwiches(db_path, "Pulled Chicken Sandwiches")
        plain = _recipe(db_path, "Porridge", 2, [_ingredient("oats", 1.0, Unit.CUP)])
        answered = _decided(
            plans, first, {"Original Ruffles": True, "Cheddar Cheese Ruffles": False}
        )

        error = _refusal(
            plans,
            [PlanMeal(recipe_id=plain), PlanMeal(recipe_id=second), answered],
        )

        assert error.kind is OptionalProblemKind.MISSING
        assert [meal.recipe_id for meal in error.meals] == [first, second]
        assert {p.recipe_id for p in error.problems} == {second}
        assert "Pulled Chicken Sandwiches" in str(error)
        assert plans.list_plans() == []

    def test_an_incomplete_answer_is_refused_naming_what_is_missing(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """One of two decided is not decided."""
        recipe_id = _sandwiches(db_path)
        meal = _decided(plans, recipe_id, {"Original Ruffles": True})

        error = _refusal(plans, [meal])

        assert error.kind is OptionalProblemKind.MISSING
        assert "Cheddar Cheese Ruffles" in str(error)
        assert "Original Ruffles" not in str(error)
        assert len(error.problems) == 1
        assert plans.list_plans() == []

    def test_a_decision_about_something_else_is_refused(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """An id that is not one of the recipe's optional ingredients is unknown."""
        recipe_id = _sandwiches(db_path)
        meal = _decided(
            plans, recipe_id, {"Original Ruffles": True, "Cheddar Cheese Ruffles": True}
        )
        meal.optional_decisions.append(
            OptionalDecision(ingredient_id=9999, include=True)
        )

        error = _refusal(plans, [meal])

        assert error.kind is OptionalProblemKind.UNKNOWN
        assert [(p.ingredient_id, p.problem) for p in error.problems] == [
            (9999, OptionalProblemKind.UNKNOWN)
        ]
        assert "9999" in str(error)
        assert plans.list_plans() == []

    def test_a_required_ingredient_cannot_be_excluded(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """Only optional ingredients take a decision."""
        recipe_id = _sandwiches(db_path)
        meal = _decided(
            plans, recipe_id, {"Original Ruffles": True, "Cheddar Cheese Ruffles": True}
        )
        with closing(get_connection(db_path)) as conn:
            required = conn.execute(
                "SELECT id FROM recipe_ingredients WHERE ingredient = ?",
                ("chicken breast",),
            ).fetchone()["id"]
        meal.optional_decisions.append(
            OptionalDecision(ingredient_id=required, include=False)
        )

        error = _refusal(plans, [meal])

        assert error.kind is OptionalProblemKind.UNKNOWN

    def test_two_decisions_for_one_ingredient_are_refused(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """Even two that agree: one decision per ingredient."""
        recipe_id = _sandwiches(db_path)
        meal = _decided(
            plans, recipe_id, {"Original Ruffles": True, "Cheddar Cheese Ruffles": True}
        )
        repeated = meal.optional_decisions[0]
        meal.optional_decisions.append(repeated.model_copy(update={"include": False}))

        error = _refusal(plans, [meal])

        assert error.kind is OptionalProblemKind.DUPLICATE
        assert [(p.ingredient_id, p.problem) for p in error.problems] == [
            (repeated.ingredient_id, OptionalProblemKind.DUPLICATE)
        ]
        assert plans.list_plans() == []

    def test_an_answer_to_a_question_that_has_changed_is_stale(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """The recipe was edited after the member was asked; ask again."""
        recipe_id = _sandwiches(db_path)
        meal = _decided(
            plans, recipe_id, {"Original Ruffles": True, "Cheddar Cheese Ruffles": True}
        )
        with closing(get_connection(db_path)) as conn:
            conn.execute(
                "UPDATE recipe_ingredients SET quantity_per_serving = 2"
                " WHERE ingredient = ?",
                ("Original Ruffles",),
            )
            conn.commit()

        error = _refusal(plans, [meal])

        assert error.kind is OptionalProblemKind.STALE
        assert [(p.ingredient_id, p.problem) for p in error.problems] == [
            (None, OptionalProblemKind.STALE)
        ]
        assert error.meals[0].optional_token != meal.optional_token
        assert error.meals[0].optional_ingredients[0].quantity == 4.0
        assert plans.list_plans() == []

    def test_an_answer_given_for_other_servings_is_stale(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """The amounts the member agreed to were for a different meal size."""
        recipe_id = _sandwiches(db_path)
        meal = _decided(
            plans, recipe_id, {"Original Ruffles": True, "Cheddar Cheese Ruffles": True}
        )

        error = _refusal(plans, [meal.model_copy(update={"servings": 6})])

        assert error.kind is OptionalProblemKind.STALE

    def test_decisions_without_a_token_are_stale(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """A decision must say which question it answers."""
        recipe_id = _sandwiches(db_path)
        meal = _decided(
            plans, recipe_id, {"Original Ruffles": True, "Cheddar Cheese Ruffles": True}
        )

        error = _refusal(plans, [meal.model_copy(update={"optional_token": None})])

        assert error.kind is OptionalProblemKind.STALE
        assert "optional_token" in str(error)

    def test_decisions_for_a_recipe_with_nothing_optional_are_stale(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """The optional ingredient was removed after the member was asked."""
        recipe_id = _sandwiches(db_path)
        meal = _decided(
            plans, recipe_id, {"Original Ruffles": True, "Cheddar Cheese Ruffles": True}
        )
        with closing(get_connection(db_path)) as conn:
            conn.execute("UPDATE recipe_ingredients SET is_optional = ?", (False,))
            conn.commit()

        error = _refusal(plans, [meal])

        assert error.kind is OptionalProblemKind.STALE
        assert error.meals == []

    def test_confirming_twice_stores_one_version(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """A retried confirmation changes nothing, whatever order it lists."""
        recipe_id = _sandwiches(db_path)
        meal = _decided(
            plans,
            recipe_id,
            {"Original Ruffles": True, "Cheddar Cheese Ruffles": False},
        )
        shuffled = meal.model_copy(
            update={"optional_decisions": list(reversed(meal.optional_decisions))}
        )

        first = plans.build([meal], actor=ACTOR)
        again = plans.build([shuffled], actor=ACTOR)

        assert again == first
        assert len(plans.list_plans()) == 1

    def test_a_different_answer_is_a_new_version(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """Changing your mind is a change to the plan, never an edit of it."""
        recipe_id = _sandwiches(db_path)
        yes = {"Original Ruffles": True, "Cheddar Cheese Ruffles": False}
        no = {"Original Ruffles": False, "Cheddar Cheese Ruffles": False}

        first = plans.build([_decided(plans, recipe_id, yes)], actor=ACTOR)
        second = plans.build([_decided(plans, recipe_id, no)], actor=ACTOR)

        assert (first.version, second.version) == (1, 2)
        assert plans.get(1) == first

    def test_a_stored_plan_stays_current_until_its_choices_change(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """A new optional ingredient makes the plan stale, and reading it works."""
        recipe_id = _sandwiches(db_path)
        plan = plans.build(
            [
                _decided(
                    plans,
                    recipe_id,
                    {"Original Ruffles": True, "Cheddar Cheese Ruffles": False},
                )
            ],
            actor=ACTOR,
        )
        before = plans.current_fingerprint(plan.version)
        with closing(get_connection(db_path)) as conn:
            conn.execute(
                "UPDATE recipe_ingredients SET is_optional = ? WHERE ingredient = ?",
                (True, "chicken breast"),
            )
            conn.commit()

        after = plans.current_fingerprint(plan.version)

        assert before == plan.content_hash
        assert after != plan.content_hash
        assert len(after) == 64

    def test_previewing_asks_the_same_question(
        self, plans: ShoppingPlans, db_path: str
    ) -> None:
        """A preview assumes nothing either."""
        recipe_id = _sandwiches(db_path)

        with pytest.raises(OptionalDecisionsError) as caught:
            plans.preview([PlanMeal(recipe_id=recipe_id)])

        assert caught.value.kind is OptionalProblemKind.MISSING


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
