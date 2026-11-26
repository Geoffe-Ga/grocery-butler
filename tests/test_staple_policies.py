"""Tests for staple policies and target amounts (ADR 0017, #168)."""

from __future__ import annotations

import os
from contextlib import closing
from typing import TYPE_CHECKING
from urllib.parse import quote

import pytest
from pydantic import ValidationError

from grocery_butler.db import get_connection, init_db
from grocery_butler.db.migrate import migrate
from grocery_butler.household_items import (
    HouseholdItemStore,
    TargetIncompleteError,
)
from grocery_butler.inventory_ledger import InventoryLedger
from grocery_butler.models import (
    HouseholdItemUpdate,
    Ingredient,
    IngredientCategory,
    NeedReasonKind,
    NeedStatus,
    NewObservation,
    ObservationSource,
    ParsedMeal,
    PlanAmount,
    PlanMeal,
    StaplePolicy,
    StockState,
    Unit,
)
from grocery_butler.recipe_store import RecipeStore
from grocery_butler.replenishment import (
    STANDING_APPROVAL,
    SYNC_ACTOR,
    ReplenishmentNeeds,
)
from grocery_butler.shopping_plans import NEVER_SUGGEST_PROBLEM, ShoppingPlans

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from grocery_butler.models import HouseholdItem, ReplenishmentNeed

ACTOR = "policy-test"


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a migrated temporary database path."""
    path = str(tmp_path / "policies.db")
    init_db(path)
    return path


def _item(db_path: str, name: str, policy: StaplePolicy | None) -> HouseholdItem:
    """Create an item with a policy."""
    items = HouseholdItemStore(db_path)
    item = items.resolve_or_create(name, actor=ACTOR)
    return items.update(item.id, HouseholdItemUpdate(staple_policy=policy), actor=ACTOR)


def _observe(db_path: str, item_id: int, state: StockState) -> ReplenishmentNeed | None:
    """Record a member's observation and sync the item's need."""
    InventoryLedger(db_path).record(
        NewObservation(
            item_id=item_id,
            state=state,
            source=ObservationSource.HUMAN,
            actor=ACTOR,
        )
    )
    return ReplenishmentNeeds(db_path).sync_item(item_id)


def _recipe_with(db_path: str, ingredient: str) -> int:
    """Save a recipe using one ingredient; return its id."""
    return RecipeStore(db_path).save_recipe(
        ParsedMeal(
            name=f"Dish with {ingredient}",
            servings=2,
            known_recipe=True,
            needs_confirmation=False,
            purchase_items=[
                Ingredient(
                    ingredient=ingredient,
                    quantity=2.0,
                    unit=Unit.TSP,
                    category=IngredientCategory.PANTRY_DRY,
                )
            ],
            pantry_items=[],
        )
    )


class TestInventoryNeeds:
    """What running low does, under each policy."""

    def test_replenish_when_low_suggests_and_waits_for_a_member(
        self, db_path: str
    ) -> None:
        """Low raises a suggestion; a member decides."""
        item = _item(db_path, "rice", StaplePolicy.REPLENISH_WHEN_LOW)

        need = _observe(db_path, item.id, StockState.LOW)

        assert need is not None
        assert need.status is NeedStatus.SUGGESTED
        assert [r.kind for r in need.active_reasons] == [NeedReasonKind.LOW_STOCK]

    def test_no_policy_behaves_like_replenish_when_low(self, db_path: str) -> None:
        """An item nobody set a policy on is suggested when low."""
        item = _item(db_path, "rice", None)

        need = _observe(db_path, item.id, StockState.OUT)

        assert need is not None
        assert need.status is NeedStatus.SUGGESTED

    def test_always_keep_is_approved_by_the_policy(self, db_path: str) -> None:
        """Coffee marked low goes straight to approved, attributed to the policy."""
        item = _item(db_path, "coffee", StaplePolicy.ALWAYS_KEEP)

        need = _observe(db_path, item.id, StockState.LOW)

        assert need is not None
        assert need.status is NeedStatus.APPROVED
        last = ReplenishmentNeeds(db_path).history(need.id)[-1]
        assert (last.actor, last.note) == (SYNC_ACTOR, STANDING_APPROVAL)

    def test_always_keep_stands_down_when_restocked(self, db_path: str) -> None:
        """A need the policy approved is cancelled once the item is on hand."""
        item = _item(db_path, "coffee", StaplePolicy.ALWAYS_KEEP)
        need = _observe(db_path, item.id, StockState.LOW)
        assert need is not None

        after = _observe(db_path, item.id, StockState.ON_HAND)

        closed = ReplenishmentNeeds(db_path).get(need.id)
        assert after is None
        assert closed is not None
        assert closed.status is NeedStatus.CANCELLED

    def test_a_policy_approval_stands_down_when_the_policy_changes(
        self, db_path: str
    ) -> None:
        """Approved by always keep, then switched to meals only: the need goes."""
        item = _item(db_path, "coffee", StaplePolicy.ALWAYS_KEEP)
        need = _observe(db_path, item.id, StockState.LOW)
        assert need is not None
        HouseholdItemStore(db_path).update(
            item.id,
            HouseholdItemUpdate(staple_policy=StaplePolicy.MEALS_ONLY),
            actor=ACTOR,
        )

        after = ReplenishmentNeeds(db_path).sync_item(item.id)

        closed = ReplenishmentNeeds(db_path).get(need.id)
        assert after is None
        assert closed is not None
        assert closed.status is NeedStatus.CANCELLED

    def test_a_member_approved_need_is_not_stood_down_by_the_policy(
        self, db_path: str
    ) -> None:
        """Only the policy's own approval is withdrawn automatically."""
        item = _item(db_path, "coffee", StaplePolicy.REPLENISH_WHEN_LOW)
        need = _observe(db_path, item.id, StockState.LOW)
        assert need is not None
        needs = ReplenishmentNeeds(db_path)
        needs.transition(need.id, NeedStatus.APPROVED, actor=ACTOR)
        HouseholdItemStore(db_path).update(
            item.id,
            HouseholdItemUpdate(staple_policy=StaplePolicy.ALWAYS_KEEP),
            actor=ACTOR,
        )

        after = _observe(db_path, item.id, StockState.ON_HAND)

        assert after is not None
        assert after.status is NeedStatus.APPROVED

    def test_always_keep_respects_a_dismissal(self, db_path: str) -> None:
        """A member who dismissed the need isn't overruled by the policy."""
        item = _item(db_path, "coffee", StaplePolicy.ALWAYS_KEEP)
        need = _observe(db_path, item.id, StockState.LOW)
        assert need is not None
        needs = ReplenishmentNeeds(db_path)
        needs.transition(need.id, NeedStatus.DISMISSED, actor=ACTOR)

        assert needs.sync_item(item.id) is None

    @pytest.mark.parametrize(
        "policy", [StaplePolicy.MEALS_ONLY, StaplePolicy.NEVER_SUGGEST]
    )
    @pytest.mark.parametrize("state", [StockState.LOW, StockState.OUT])
    def test_meals_only_and_never_suggest_raise_no_need(
        self, db_path: str, policy: StaplePolicy, state: StockState
    ) -> None:
        """Paprika running out is not, by itself, a reason to buy paprika."""
        item = _item(db_path, "paprika", policy)

        assert _observe(db_path, item.id, state) is None
        assert ReplenishmentNeeds(db_path).list_needs() == []

    @pytest.mark.parametrize(
        "policy", [StaplePolicy.MEALS_ONLY, StaplePolicy.NEVER_SUGGEST]
    )
    def test_changing_to_a_quiet_policy_withdraws_the_suggestion(
        self, db_path: str, policy: StaplePolicy
    ) -> None:
        """A suggestion raised under the old policy goes when the policy changes."""
        item = _item(db_path, "paprika", StaplePolicy.REPLENISH_WHEN_LOW)
        need = _observe(db_path, item.id, StockState.LOW)
        assert need is not None
        HouseholdItemStore(db_path).update(
            item.id, HouseholdItemUpdate(staple_policy=policy), actor=ACTOR
        )

        after = ReplenishmentNeeds(db_path).sync_item(item.id)

        closed = ReplenishmentNeeds(db_path).get(need.id)
        assert after is None
        assert closed is not None
        assert closed.status is NeedStatus.CANCELLED

    def test_a_manual_request_works_under_never_suggest(self, db_path: str) -> None:
        """A member can always ask for an item by name."""
        item = _item(db_path, "saffron", StaplePolicy.NEVER_SUGGEST)
        needs = ReplenishmentNeeds(db_path)

        need = needs.add_reason(
            item.id, NeedReasonKind.MANUAL_REQUEST, source_key="ask", actor=ACTOR
        )
        synced = needs.sync_item(item.id)

        assert need.status is NeedStatus.APPROVED
        assert synced is not None
        assert synced.status is NeedStatus.APPROVED


class TestPlanBuilder:
    """What the plan builder does with each policy."""

    def _lines(self, db_path: str, *recipes: int) -> dict[str, list[NeedReasonKind]]:
        """Build a plan; return each line's reason kinds by item name."""
        plan = ShoppingPlans(db_path).preview(
            [PlanMeal(recipe_id=recipe) for recipe in recipes]
        )
        return {
            line.display_name: [reason.kind for reason in line.reasons]
            for line in plan.lines
        }

    def test_always_keep_is_on_the_plan_when_low(self, db_path: str) -> None:
        """Coffee set to always keep appears once it is marked low."""
        item = _item(db_path, "coffee", StaplePolicy.ALWAYS_KEEP)
        before = self._lines(db_path)

        _observe(db_path, item.id, StockState.LOW)

        assert "Coffee" not in before
        assert self._lines(db_path)["Coffee"] == [NeedReasonKind.LOW_STOCK]

    def test_replenish_when_low_is_on_the_plan_only_after_approval(
        self, db_path: str
    ) -> None:
        """A suggestion is not yet a plan line."""
        item = _item(db_path, "rice", StaplePolicy.REPLENISH_WHEN_LOW)
        need = _observe(db_path, item.id, StockState.LOW)
        assert need is not None
        suggested = self._lines(db_path)

        ReplenishmentNeeds(db_path).transition(
            need.id, NeedStatus.APPROVED, actor=ACTOR
        )

        assert "Rice" not in suggested
        assert "Rice" in self._lines(db_path)

    def test_meals_only_appears_only_when_a_meal_needs_it(self, db_path: str) -> None:
        """Paprika is bought for the dish that uses it, and not otherwise."""
        item = _item(db_path, "paprika", StaplePolicy.MEALS_ONLY)
        _observe(db_path, item.id, StockState.OUT)
        recipe = _recipe_with(db_path, "paprika")

        without_meal = self._lines(db_path)
        with_meal = self._lines(db_path, recipe)

        assert "Paprika" not in without_meal
        assert with_meal["Paprika"] == [NeedReasonKind.RECIPE_DEMAND]

    @pytest.mark.parametrize(
        "policy", [StaplePolicy.ALWAYS_KEEP, StaplePolicy.REPLENISH_WHEN_LOW]
    )
    def test_a_kept_item_is_not_bought_per_meal(
        self, db_path: str, policy: StaplePolicy
    ) -> None:
        """A meal doesn't add an item the household keeps in stock."""
        _item(db_path, "cumin", policy)
        recipe = _recipe_with(db_path, "cumin")

        assert "Cumin" not in self._lines(db_path, recipe)

    def test_never_suggest_is_left_off_and_reported(self, db_path: str) -> None:
        """The recipe's use of it is reported, never silently dropped."""
        _item(db_path, "saffron", StaplePolicy.NEVER_SUGGEST)
        recipe = _recipe_with(db_path, "saffron")

        plan = ShoppingPlans(db_path).preview([PlanMeal(recipe_id=recipe)])

        assert "Saffron" not in [line.display_name for line in plan.lines]
        assert [(u.ingredient, u.problem) for u in plan.unplanned] == [
            ("saffron", NEVER_SUGGEST_PROBLEM)
        ]

    def test_no_policy_and_not_a_staple_is_bought_for_the_meal(
        self, db_path: str
    ) -> None:
        """An ordinary ingredient goes on the plan, as before."""
        recipe = _recipe_with(db_path, "leeks")

        assert self._lines(db_path, recipe)["Leeks"] == [NeedReasonKind.RECIPE_DEMAND]

    def test_a_policy_overrides_the_old_staple_list(self, db_path: str) -> None:
        """A listed staple switched to meals only is bought for the meal."""
        store = RecipeStore(db_path)
        store.add_pantry_staple("nutmeg", "pantry_dry")
        item = HouseholdItemStore(db_path).resolve("nutmeg")
        assert item is not None
        recipe = _recipe_with(db_path, "nutmeg")
        as_staple = self._lines(db_path, recipe)

        HouseholdItemStore(db_path).update(
            item.id,
            HouseholdItemUpdate(staple_policy=StaplePolicy.MEALS_ONLY),
            actor=ACTOR,
        )

        assert "Nutmeg" not in as_staple
        assert "Nutmeg" in self._lines(db_path, recipe)

    def test_a_policy_change_changes_the_plan_fingerprint(self, db_path: str) -> None:
        """A plan built before the change is no longer current."""
        _item(db_path, "cumin", StaplePolicy.REPLENISH_WHEN_LOW)
        recipe = _recipe_with(db_path, "cumin")
        plans = ShoppingPlans(db_path)
        plan = plans.build([PlanMeal(recipe_id=recipe)], actor=ACTOR)
        cumin = HouseholdItemStore(db_path).resolve("cumin")
        assert cumin is not None

        HouseholdItemStore(db_path).update(
            cumin.id,
            HouseholdItemUpdate(staple_policy=StaplePolicy.MEALS_ONLY),
            actor=ACTOR,
        )

        assert plans.current_fingerprint(plan.version) != plan.content_hash


class TestTargets:
    """A target amount is optional, whole, and used when nothing else says how much."""

    def test_a_target_becomes_the_amount_on_a_need_line(self, db_path: str) -> None:
        """Coffee with a target of 2 bags is planned as 2 bags."""
        item = _item(db_path, "coffee", StaplePolicy.ALWAYS_KEEP)
        HouseholdItemStore(db_path).update(
            item.id,
            HouseholdItemUpdate(target_quantity=2, target_unit=Unit.BAG),
            actor=ACTOR,
        )
        _observe(db_path, item.id, StockState.LOW)

        plan = ShoppingPlans(db_path).preview([])

        assert plan.lines[0].amounts == [PlanAmount(quantity=2.0, unit=Unit.BAG)]

    def test_no_target_means_buy_the_usual(self, db_path: str) -> None:
        """Without a target a need line has no amount."""
        item = _item(db_path, "coffee", StaplePolicy.ALWAYS_KEEP)
        _observe(db_path, item.id, StockState.LOW)

        assert ShoppingPlans(db_path).preview([]).lines[0].amounts == []

    def test_a_recipe_amount_wins_over_the_target(self, db_path: str) -> None:
        """What a meal states is used; the target is only a fallback."""
        item = _item(db_path, "paprika", StaplePolicy.MEALS_ONLY)
        HouseholdItemStore(db_path).update(
            item.id,
            HouseholdItemUpdate(target_quantity=1, target_unit=Unit.JAR),
            actor=ACTOR,
        )
        recipe = _recipe_with(db_path, "paprika")

        plan = ShoppingPlans(db_path).preview([PlanMeal(recipe_id=recipe)])

        assert [amount.unit for amount in plan.lines[0].amounts] == [Unit.TSP]

    @pytest.mark.parametrize(
        "changes",
        [
            {"target_quantity": 2},
            {"target_unit": Unit.BAG},
        ],
    )
    def test_half_a_target_is_refused(
        self, db_path: str, changes: dict[str, object]
    ) -> None:
        """A quantity needs a unit, and a unit needs a quantity."""
        item = _item(db_path, "coffee", None)
        items = HouseholdItemStore(db_path)

        with pytest.raises(TargetIncompleteError, match="both a quantity and a unit"):
            items.update(
                item.id, HouseholdItemUpdate.model_validate(changes), actor=ACTOR
            )

        unchanged = items.get(item.id)
        assert unchanged is not None
        assert (unchanged.target_quantity, unchanged.target_unit) == (None, None)

    def test_clearing_one_half_of_a_target_is_refused(self, db_path: str) -> None:
        """A whole target can't be turned into half of one."""
        item = _item(db_path, "coffee", None)
        items = HouseholdItemStore(db_path)
        items.update(
            item.id,
            HouseholdItemUpdate(target_quantity=2, target_unit=Unit.BAG),
            actor=ACTOR,
        )

        with pytest.raises(TargetIncompleteError):
            items.update(item.id, HouseholdItemUpdate(target_unit=None), actor=ACTOR)

    def test_a_target_can_be_cleared_whole(self, db_path: str) -> None:
        """Clearing both halves together is fine."""
        item = _item(db_path, "coffee", None)
        items = HouseholdItemStore(db_path)
        items.update(
            item.id,
            HouseholdItemUpdate(target_quantity=2, target_unit=Unit.BAG),
            actor=ACTOR,
        )

        cleared = items.update(
            item.id,
            HouseholdItemUpdate(target_quantity=None, target_unit=None),
            actor=ACTOR,
        )

        assert (cleared.target_quantity, cleared.target_unit) == (None, None)

    @pytest.mark.parametrize(
        "changes",
        [
            {"target_quantity": 0},
            {"target_quantity": -1},
            {"cadence_days": 0},
            {"cadence_days": 366},
            {"target_unit": "armful"},
        ],
    )
    def test_invalid_values_are_refused(self, changes: dict[str, object]) -> None:
        """A target must be positive, a cadence 1 to 365 days, a unit real."""
        with pytest.raises(ValidationError):
            HouseholdItemUpdate.model_validate(changes)

    def test_cadence_is_stored_as_a_hint(self, db_path: str) -> None:
        """How often an item is usually bought is kept, and changes no need."""
        item = _item(db_path, "coffee", StaplePolicy.ALWAYS_KEEP)

        updated = HouseholdItemStore(db_path).update(
            item.id, HouseholdItemUpdate(cadence_days=14), actor=ACTOR
        )

        assert updated.cadence_days == 14
        assert ReplenishmentNeeds(db_path).list_needs() == []


class TestStapleList:
    """The older flat staple list stays consistent with policies."""

    def test_adding_a_staple_sets_replenish_when_low(self, db_path: str) -> None:
        """A new staple gets the policy the flat list always meant."""
        RecipeStore(db_path).add_pantry_staple("nutmeg", "pantry_dry")

        item = HouseholdItemStore(db_path).resolve("nutmeg")

        assert item is not None
        assert item.staple_policy is StaplePolicy.REPLENISH_WHEN_LOW

    def test_adding_a_staple_keeps_a_policy_already_chosen(self, db_path: str) -> None:
        """Listing an item doesn't overwrite what the household chose for it."""
        _item(db_path, "coffee", StaplePolicy.ALWAYS_KEEP)

        RecipeStore(db_path).add_pantry_staple("coffee", "beverages")

        item = HouseholdItemStore(db_path).resolve("coffee")
        assert item is not None
        assert item.staple_policy is StaplePolicy.ALWAYS_KEEP

    def test_removing_a_staple_clears_replenish_when_low(self, db_path: str) -> None:
        """Off the list, the item is no longer a staple."""
        store = RecipeStore(db_path)
        staple_id = store.add_pantry_staple("nutmeg", "pantry_dry")

        store.remove_pantry_staple(staple_id)

        item = HouseholdItemStore(db_path).resolve("nutmeg")
        assert item is not None
        assert item.staple_policy is None

    def test_removing_a_staple_keeps_a_chosen_policy(self, db_path: str) -> None:
        """A policy set on the item itself survives leaving the list."""
        _item(db_path, "coffee", StaplePolicy.ALWAYS_KEEP)
        store = RecipeStore(db_path)
        staple_id = store.add_pantry_staple("coffee", "beverages")

        store.remove_pantry_staple(staple_id)

        item = HouseholdItemStore(db_path).resolve("coffee")
        assert item is not None
        assert item.staple_policy is StaplePolicy.ALWAYS_KEEP

    def test_removing_an_unknown_staple_is_harmless(self, db_path: str) -> None:
        """A missing id changes nothing."""
        RecipeStore(db_path).remove_pantry_staple(99999)


def _policies(db_path: str) -> dict[str, str | None]:
    """Read every canonical item's policy straight from the table."""
    with closing(get_connection(db_path)) as conn:
        rows = conn.execute(
            "SELECT name, staple_policy FROM household_items"
            " WHERE merged_into_id IS NULL"
        ).fetchall()
    return {row["name"]: row["staple_policy"] for row in rows}


def _prepare_pre_policy_database(db_path: str) -> None:
    """Build a version 19 database with staples, a chosen policy and a merge."""
    migrate(db_path, target=19)
    with closing(get_connection(db_path)) as conn:
        conn.execute(
            "UPDATE household_items SET staple_policy = ? WHERE name = ?",
            ("always_keep", "salt"),
        )
        conn.execute(
            "INSERT INTO household_items (name, display_name) VALUES (?, ?)",
            ("leeks", "Leeks"),
        )
        conn.execute(
            "INSERT INTO household_items (name, display_name) VALUES (?, ?)",
            ("unsalted butter", "Unsalted Butter"),
        )
        conn.commit()
        rows = conn.execute(
            "SELECT id FROM household_items WHERE name = ?", ("unsalted butter",)
        ).fetchall()
        target = rows[0]["id"]
        conn.execute(
            "UPDATE household_items SET merged_into_id = ?, active = ? WHERE name = ?",
            (target, False, "butter"),
        )
        conn.commit()


def _assert_migrated(db_path: str) -> None:
    """Check migration 020's outcome on a prepared database."""
    policies = _policies(db_path)
    with closing(get_connection(db_path)) as conn:
        staples = {
            row["ingredient"]
            for row in conn.execute("SELECT ingredient FROM pantry_staples").fetchall()
        }
    assert len(staples) >= 5
    for staple in staples - {"salt", "butter"}:
        assert policies[staple] == "replenish_when_low", staple
    assert policies["salt"] == "always_keep"
    assert policies["unsalted butter"] == "replenish_when_low"
    assert policies["leeks"] is None
    assert "butter" not in policies


class TestMigration:
    """Existing staples migrate to replenish when low; everything else is kept."""

    def test_every_staple_has_exactly_one_policy_after_migration(
        self, tmp_path: Path
    ) -> None:
        """Staples get the policy, a chosen policy survives, a merge is followed."""
        path = str(tmp_path / "migrating.db")
        _prepare_pre_policy_database(path)

        applied = migrate(path)

        assert applied >= 1
        _assert_migrated(path)

    def test_a_fresh_database_has_policies_on_its_seeded_staples(
        self, db_path: str
    ) -> None:
        """The staples a new install starts with are replenish when low."""
        names = RecipeStore(db_path).get_pantry_staple_names()
        policies = _policies(db_path)

        assert names
        assert {policies[name] for name in names} == {"replenish_when_low"}

    def test_the_new_columns_start_empty(self, db_path: str) -> None:
        """No item has a target or a cadence until someone sets one."""
        items = HouseholdItemStore(db_path).list_items()

        assert items
        assert all(
            (i.target_quantity, i.target_unit, i.cadence_days) == (None, None, None)
            for i in items
        )


_PG_URL = os.environ.get("TEST_DATABASE_URL", "")
_PG_SCHEMA = "staple_policy_test"


@pytest.mark.skipif(not _PG_URL, reason="TEST_DATABASE_URL not set")
class TestPostgres:
    """Migration 020 and the policy rules on Postgres, in a schema of their own."""

    @pytest.fixture()
    def pg_url(self) -> Iterator[str]:
        """Create an empty private schema and drop it afterwards."""
        self._reset_schema(create=True)
        yield f"{_PG_URL}?options={quote(f'-csearch_path={_PG_SCHEMA}')}"
        self._reset_schema(create=False)

    def _reset_schema(self, *, create: bool) -> None:
        """Drop the private schema and optionally create it again."""
        with closing(get_connection(_PG_URL)) as conn:
            conn.execute("DROP SCHEMA IF EXISTS staple_policy_test CASCADE")
            if create:
                conn.execute("CREATE SCHEMA staple_policy_test")
            conn.commit()

    def test_migration_sets_policies(self, pg_url: str) -> None:
        """The same migration outcome as SQLite."""
        _prepare_pre_policy_database(pg_url)

        migrate(pg_url)

        _assert_migrated(pg_url)

    def test_policy_rules_and_targets(self, pg_url: str) -> None:
        """Always keep approves itself and a target sets the plan amount."""
        init_db(pg_url)
        item = _item(pg_url, "pg coffee", StaplePolicy.ALWAYS_KEEP)
        HouseholdItemStore(pg_url).update(
            item.id,
            HouseholdItemUpdate(target_quantity=2, target_unit=Unit.BAG),
            actor=ACTOR,
        )

        need = _observe(pg_url, item.id, StockState.LOW)
        plan = ShoppingPlans(pg_url).preview([])

        assert need is not None
        assert need.status is NeedStatus.APPROVED
        assert [line.amounts for line in plan.lines] == [
            [PlanAmount(quantity=2.0, unit=Unit.BAG)]
        ]
