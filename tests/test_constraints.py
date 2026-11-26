"""Tests for hard constraints and soft preferences (ADR 0012, issue #145)."""

from __future__ import annotations

import datetime as dt
import os
from contextlib import closing
from typing import TYPE_CHECKING, Any

import pytest

from grocery_butler.constraints import (
    COMMON_ALLERGENS,
    ConstraintNotFoundError,
    HouseholdRules,
    import_dietary_preference,
)
from grocery_butler.db import get_connection, init_db
from grocery_butler.household_items import HouseholdItemStore, ItemNotFoundError
from grocery_butler.models import (
    BrandMatchType,
    BrandPreference,
    BrandPreferenceType,
    HardConstraint,
    HardConstraintKind,
    HouseholdItem,
    HouseholdItemUpdate,
    IngredientCategory,
    SoftPreference,
    SoftPreferenceKind,
)
from grocery_butler.recipe_store import RecipeStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

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
    path = str(tmp_path / "rules.db")
    init_db(path)
    return path


@pytest.fixture()
def rules(db_path: str) -> HouseholdRules:
    """Return a rules service with a fixed clock."""
    return HouseholdRules(db_path, clock=FakeClock())


@pytest.fixture()
def milk(db_path: str) -> HouseholdItem:
    """Return a dairy item."""
    return HouseholdItemStore(db_path).resolve_or_create(
        "milk", actor="t", category=IngredientCategory.DAIRY
    )


def _keys(constraints: list[HardConstraint]) -> list[tuple[str, str, str]]:
    """Return (kind, value, scope) for each constraint."""
    return [(c.kind.value, c.value, c.scope) for c in constraints]


class TestTypesStaySeparate:
    """A hard constraint can't become a preference by any route."""

    def test_the_two_types_share_no_fields_that_could_be_confused(self) -> None:
        """Different classes, different kinds, no common base but BaseModel."""
        hard_kinds = {kind.value for kind in HardConstraintKind}
        soft_kinds = {kind.value for kind in SoftPreferenceKind}

        assert hard_kinds.isdisjoint(soft_kinds)
        assert not issubclass(HardConstraint, SoftPreference)
        assert not issubclass(SoftPreference, HardConstraint)

    def test_no_method_converts_a_hard_constraint_into_a_preference(self) -> None:
        """There is nothing to call that would downgrade one."""
        suspicious = [
            name
            for name in dir(HouseholdRules)
            if not name.startswith("__")
            and any(
                word in name for word in ("downgrade", "soften", "to_soft", "relax")
            )
        ]

        assert suspicious == []

    def test_adding_a_soft_preference_with_a_hard_kind_is_impossible(
        self, rules: HouseholdRules
    ) -> None:
        """The soft API only accepts soft kinds."""
        # What a careless or hostile caller would pass at runtime.
        wrong_kind: Any = HardConstraintKind.ALLERGEN_EXCLUSION

        with pytest.raises(TypeError, match="not a soft preference"):
            rules.add_soft(wrong_kind, "peanut", actor=ACTOR)

        assert rules.soft_for_household() == []

    def test_stored_hard_constraints_never_appear_among_soft_preferences(
        self, rules: HouseholdRules, milk: HouseholdItem
    ) -> None:
        """Reading preferences for an item returns no hard constraint."""
        rules.add_hard(HardConstraintKind.ALLERGEN_EXCLUSION, "peanut", actor=ACTOR)
        rules.add_hard(
            HardConstraintKind.DO_NOT_SUBSTITUTE, "", actor=ACTOR, item_id=milk.id
        )
        rules.add_soft(SoftPreferenceKind.ORGANIC, "", actor=ACTOR, item_id=milk.id)

        soft = rules.soft_for_item(milk.id)

        assert [p.kind for p in soft] == [SoftPreferenceKind.ORGANIC]
        assert all(isinstance(p, SoftPreference) for p in soft)


class TestHardConstraints:
    """Allergens, dietary rules and do-not-substitute."""

    def test_household_allergen_exclusion_is_stored_normalized(
        self, rules: HouseholdRules
    ) -> None:
        """Values are trimmed and lowercased; who and when are recorded."""
        constraint = rules.add_hard(
            HardConstraintKind.ALLERGEN_EXCLUSION,
            "  Peanut ",
            actor=ACTOR,
            note="anaphylaxis",
        )

        assert constraint.kind is HardConstraintKind.ALLERGEN_EXCLUSION
        assert constraint.value == "peanut"
        assert constraint.item_id is None
        assert constraint.scope == "household"
        assert constraint.note == "anaphylaxis"
        assert constraint.active is True
        assert (constraint.created_by, constraint.created_at) == (ACTOR, NOW)

    def test_adding_the_same_constraint_twice_returns_the_first(
        self, rules: HouseholdRules
    ) -> None:
        """Retries don't stack duplicates."""
        first = rules.add_hard(
            HardConstraintKind.ALLERGEN_EXCLUSION, "peanut", actor=ACTOR
        )

        again = rules.add_hard(
            HardConstraintKind.ALLERGEN_EXCLUSION, "PEANUT", actor="jess@rubotpaul"
        )

        assert again.id == first.id
        assert len(rules.list_hard()) == 1

    @pytest.mark.parametrize(
        "kind",
        [HardConstraintKind.ALLERGEN_EXCLUSION, HardConstraintKind.DIETARY_RULE],
    )
    def test_allergens_and_dietary_rules_need_a_value(
        self, rules: HouseholdRules, kind: HardConstraintKind
    ) -> None:
        """You have to say what is excluded."""
        with pytest.raises(ValueError, match="needs a value"):
            rules.add_hard(kind, "   ", actor=ACTOR)

    def test_do_not_substitute_is_about_one_item_and_has_no_value(
        self, rules: HouseholdRules, milk: HouseholdItem
    ) -> None:
        """It can't be household-wide, and it carries no text."""
        with pytest.raises(ValueError, match="one item"):
            rules.add_hard(HardConstraintKind.DO_NOT_SUBSTITUTE, "", actor=ACTOR)

        constraint = rules.add_hard(
            HardConstraintKind.DO_NOT_SUBSTITUTE,
            "ignored",
            actor=ACTOR,
            item_id=milk.id,
        )

        assert (constraint.value, constraint.item_id, constraint.scope) == (
            "",
            milk.id,
            "item",
        )

    def test_constraint_on_an_unknown_item_is_refused(
        self, rules: HouseholdRules
    ) -> None:
        """An item constraint needs an item."""
        with pytest.raises(ItemNotFoundError):
            rules.add_hard(
                HardConstraintKind.DO_NOT_SUBSTITUTE, "", actor=ACTOR, item_id=99999
            )

    def test_item_constraint_is_stored_on_the_canonical_item(
        self, rules: HouseholdRules, milk: HouseholdItem, db_path: str
    ) -> None:
        """A constraint set on a merged item belongs to its survivor."""
        items = HouseholdItemStore(db_path)
        whole = items.resolve_or_create("whole milk", actor="t")
        items.merge(whole.id, milk.id, actor="t")

        constraint = rules.add_hard(
            HardConstraintKind.DO_NOT_SUBSTITUTE, "", actor=ACTOR, item_id=whole.id
        )

        assert constraint.item_id == milk.id

    def test_hard_for_item_combines_household_and_item_constraints(
        self, rules: HouseholdRules, milk: HouseholdItem, db_path: str
    ) -> None:
        """Every line gets the household's constraints plus its own."""
        other = HouseholdItemStore(db_path).resolve_or_create("eggs", actor="t")
        rules.add_hard(HardConstraintKind.DIETARY_RULE, "vegetarian", actor=ACTOR)
        rules.add_hard(HardConstraintKind.ALLERGEN_EXCLUSION, "peanut", actor=ACTOR)
        rules.add_hard(
            HardConstraintKind.DO_NOT_SUBSTITUTE, "", actor=ACTOR, item_id=milk.id
        )

        assert _keys(rules.hard_for_item(milk.id)) == [
            ("allergen_exclusion", "peanut", "household"),
            ("dietary_rule", "vegetarian", "household"),
            ("do_not_substitute", "", "item"),
        ]
        assert _keys(rules.hard_for_item(other.id)) == [
            ("allergen_exclusion", "peanut", "household"),
            ("dietary_rule", "vegetarian", "household"),
        ]

    def test_constraint_set_on_a_merged_away_item_still_applies_to_the_survivor(
        self, rules: HouseholdRules, milk: HouseholdItem, db_path: str
    ) -> None:
        """Merging items never loses a hard constraint."""
        items = HouseholdItemStore(db_path)
        whole = items.resolve_or_create("whole milk", actor="t")
        rules.add_hard(
            HardConstraintKind.DO_NOT_SUBSTITUTE, "", actor=ACTOR, item_id=whole.id
        )

        items.merge(whole.id, milk.id, actor="t")

        assert _keys(rules.hard_for_item(milk.id)) == [
            ("do_not_substitute", "", "item")
        ]

    def test_deactivating_keeps_the_row_and_records_who(
        self, rules: HouseholdRules
    ) -> None:
        """A removed constraint stops applying but is never erased."""
        constraint = rules.add_hard(
            HardConstraintKind.ALLERGEN_EXCLUSION, "peanut", actor=ACTOR
        )

        gone = rules.deactivate_hard(constraint.id, actor="jess@rubotpaul")

        assert gone.active is False
        assert (gone.deactivated_by, gone.deactivated_at) == ("jess@rubotpaul", NOW)
        assert rules.list_hard() == []
        assert [c.id for c in rules.list_hard(include_inactive=True)] == [constraint.id]

    def test_re_adding_a_deactivated_constraint_makes_a_new_active_one(
        self, rules: HouseholdRules
    ) -> None:
        """History keeps both; only the new one applies."""
        first = rules.add_hard(
            HardConstraintKind.ALLERGEN_EXCLUSION, "peanut", actor=ACTOR
        )
        rules.deactivate_hard(first.id, actor=ACTOR)

        second = rules.add_hard(
            HardConstraintKind.ALLERGEN_EXCLUSION, "peanut", actor=ACTOR
        )

        assert second.id != first.id
        assert [c.id for c in rules.list_hard()] == [second.id]

    def test_deactivating_a_missing_constraint_raises(
        self, rules: HouseholdRules
    ) -> None:
        """You can only remove what exists."""
        with pytest.raises(ConstraintNotFoundError, match="99999"):
            rules.deactivate_hard(99999, actor=ACTOR)

    def test_common_allergens_are_offered_as_guidance(self) -> None:
        """The nine major allergens, lowercase, for a client to suggest."""
        assert len(COMMON_ALLERGENS) == 9
        assert "peanut" in COMMON_ALLERGENS
        assert all(name == name.lower() for name in COMMON_ALLERGENS)


class TestSoftPreferences:
    """Existing storage is read as typed preferences; nothing is moved."""

    def test_item_brand_and_size_are_read_from_the_item(
        self, rules: HouseholdRules, milk: HouseholdItem, db_path: str
    ) -> None:
        """Preferences already on the item need no migration."""
        HouseholdItemStore(db_path).update(
            milk.id,
            HouseholdItemUpdate(preferred_brand="Clover", preferred_size="1 gal"),
            actor=ACTOR,
        )

        soft = rules.soft_for_item(milk.id)

        assert [(p.kind, p.value, p.source) for p in soft] == [
            (SoftPreferenceKind.BRAND, "Clover", "item"),
            (SoftPreferenceKind.SIZE, "1 gal", "item"),
        ]

    def test_brand_rules_apply_by_ingredient_name_and_by_category(
        self, rules: HouseholdRules, milk: HouseholdItem, db_path: str
    ) -> None:
        """The older brand rules table is read without being rewritten."""
        store = RecipeStore(db_path)
        store.add_brand_preference(
            BrandPreference(
                match_target="milk",
                match_type=BrandMatchType.INGREDIENT,
                brand="Straus",
                preference_type=BrandPreferenceType.PREFERRED,
            )
        )
        store.add_brand_preference(
            BrandPreference(
                match_target="dairy",
                match_type=BrandMatchType.CATEGORY,
                brand="Generic Co",
                preference_type=BrandPreferenceType.AVOID,
            )
        )
        store.add_brand_preference(
            BrandPreference(
                match_target="produce",
                match_type=BrandMatchType.CATEGORY,
                brand="Other",
                preference_type=BrandPreferenceType.PREFERRED,
            )
        )

        soft = rules.soft_for_item(milk.id)

        assert [(p.kind, p.value, p.source) for p in soft] == [
            (SoftPreferenceKind.AVOID_BRAND, "Generic Co", "brand_rule"),
            (SoftPreferenceKind.BRAND, "Straus", "brand_rule"),
        ]
        assert len(store.get_brand_preferences()) == 3

    def test_organic_and_generic_are_stored_for_an_item_or_the_household(
        self, rules: HouseholdRules, milk: HouseholdItem, db_path: str
    ) -> None:
        """The new kinds live in the preferences table."""
        eggs = HouseholdItemStore(db_path).resolve_or_create("eggs", actor="t")
        rules.add_soft(SoftPreferenceKind.ORGANIC, "", actor=ACTOR)
        rules.add_soft(SoftPreferenceKind.GENERIC, "", actor=ACTOR, item_id=eggs.id)

        assert [p.kind for p in rules.soft_for_item(milk.id)] == [
            SoftPreferenceKind.ORGANIC
        ]
        assert [(p.kind, p.item_id) for p in rules.soft_for_item(eggs.id)] == [
            (SoftPreferenceKind.GENERIC, eggs.id),
            (SoftPreferenceKind.ORGANIC, None),
        ]

    def test_adding_the_same_preference_twice_stores_it_once(
        self, rules: HouseholdRules
    ) -> None:
        """Retries don't stack duplicates."""
        rules.add_soft(SoftPreferenceKind.ORGANIC, "", actor=ACTOR)
        rules.add_soft(SoftPreferenceKind.ORGANIC, "", actor=ACTOR)

        assert len(rules.soft_for_household()) == 1


class TestDietaryMigration:
    """The old free-text dietary preference is carried over, not parsed."""

    def test_text_becomes_one_household_dietary_rule_verbatim(
        self, db_path: str
    ) -> None:
        """Nothing is guessed from the text, and the original stays."""
        store = RecipeStore(db_path)
        store.set_preference("dietary_restrictions", "No nuts except almonds; Kosher")

        import_dietary_preference(db_path)
        import_dietary_preference(db_path)

        hard = HouseholdRules(db_path).list_hard()
        assert _keys(hard) == [
            ("dietary_rule", "no nuts except almonds; kosher", "household")
        ]
        assert hard[0].created_by == "migration"
        assert "unverified" in hard[0].note
        assert (
            store.get_preference("dietary_restrictions")
            == "No nuts except almonds; Kosher"
        )

    @pytest.mark.parametrize("text", ["", "   ", "none", "None", "n/a"])
    def test_blank_or_none_text_creates_nothing(self, db_path: str, text: str) -> None:
        """An empty preference is not a rule."""
        RecipeStore(db_path).set_preference("dietary_restrictions", text)

        import_dietary_preference(db_path)

        assert HouseholdRules(db_path).list_hard() == []

    def test_no_preference_row_creates_nothing(self, db_path: str) -> None:
        """A household that never set it gets no rule."""
        import_dietary_preference(db_path)

        assert HouseholdRules(db_path).list_hard() == []


_PG_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.mark.skipif(not _PG_URL, reason="TEST_DATABASE_URL not set")
class TestPostgres:
    """Rules behave the same on Postgres."""

    @pytest.fixture()
    def clean(self) -> Iterator[None]:
        """Remove this suite's rows before and after."""
        init_db(_PG_URL)
        self._clean()
        yield
        self._clean()

    def _clean(self) -> None:
        """Delete this suite's constraints and preferences."""
        with closing(get_connection(_PG_URL)) as conn:
            conn.execute(
                "DELETE FROM hard_constraints WHERE created_by = ?", ("pg-rules-test",)
            )
            conn.execute(
                "DELETE FROM soft_preferences WHERE created_by = ?", ("pg-rules-test",)
            )
            conn.commit()

    @pytest.mark.usefixtures("clean")
    def test_add_list_and_deactivate(self) -> None:
        """The lifecycle works through the Postgres dialect."""
        rules = HouseholdRules(_PG_URL)

        constraint = rules.add_hard(
            HardConstraintKind.ALLERGEN_EXCLUSION, "pg-sesame", actor="pg-rules-test"
        )
        again = rules.add_hard(
            HardConstraintKind.ALLERGEN_EXCLUSION, "pg-sesame", actor="pg-rules-test"
        )
        rules.add_soft(SoftPreferenceKind.ORGANIC, "pg", actor="pg-rules-test")
        gone = rules.deactivate_hard(constraint.id, actor="pg-rules-test")

        assert again.id == constraint.id
        assert gone.active is False
        assert constraint.id not in [c.id for c in rules.list_hard()]
        assert "pg" in [p.value for p in rules.soft_for_household()]
