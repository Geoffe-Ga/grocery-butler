"""Tests for canonical household items (ADR 0006, backlog P0.4, issue #133)."""

from __future__ import annotations

import os
from contextlib import closing
from typing import TYPE_CHECKING
from unittest.mock import patch

import pytest

from grocery_butler.db import get_connection, init_db
from grocery_butler.household_items import (
    AliasConflictError,
    HouseholdItemStore,
    ItemNotFoundError,
    MergeError,
    backfill_item_links,
    normalize_item_name,
)
from grocery_butler.models import (
    HouseholdItem,
    HouseholdItemUpdate,
    Ingredient,
    IngredientCategory,
    InventoryItem,
    ItemEventType,
    ParsedMeal,
    StaplePolicy,
    Unit,
)
from grocery_butler.pantry_manager import PantryManager
from grocery_butler.recipe_store import RecipeStore

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from grocery_butler.db.adapter import DatabaseConnection

ACTOR = "geoff"


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a migrated temporary database path."""
    path = str(tmp_path / "items.db")
    init_db(path)
    return path


@pytest.fixture()
def store(db_path: str) -> HouseholdItemStore:
    """Return an item store on a fresh database."""
    return HouseholdItemStore(db_path)


def _names(store: HouseholdItemStore, *, include_inactive: bool = False) -> set[str]:
    """Return the names of every listed item.

    A migrated database already holds the seeded pantry staples as items,
    so tests compare names, not counts.
    """
    return {item.name for item in store.list_items(include_inactive=include_inactive)}


def _event_types(store: HouseholdItemStore, item_id: int) -> list[ItemEventType]:
    """Return an item's event types, oldest first."""
    return [event.event_type for event in store.history(item_id)]


class TestNormalizeItemName:
    """Normalization is weak on purpose: it never joins two real products."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [
            ("Peanut Butter", "peanut butter"),
            ("  peanut   butter  ", "peanut butter"),
            ("PEANUT\tBUTTER", "peanut butter"),
            ("milk", "milk"),
        ],
    )
    def test_lowercases_trims_and_collapses_whitespace(
        self, raw: str, expected: str
    ) -> None:
        """Case and spacing differences are not different items."""
        assert normalize_item_name(raw) == expected

    def test_does_not_fold_plurals(self) -> None:
        """Tomato and tomatoes stay distinct until a person says otherwise."""
        assert normalize_item_name("Tomatoes") != normalize_item_name("Tomato")

    def test_keeps_punctuation(self) -> None:
        """2% milk is not 2 milk."""
        assert normalize_item_name("2% Milk") == "2% milk"


class TestResolveOrCreate:
    """Unknown text creates an item; known text finds it."""

    def test_creates_an_item_with_its_name_as_an_alias(
        self, store: HouseholdItemStore
    ) -> None:
        """A new item is active, canonical, and resolvable by its own name."""
        item = store.resolve_or_create(
            "Peanut Butter", actor=ACTOR, category=IngredientCategory.PANTRY_DRY
        )

        assert item.name == "peanut butter"
        assert item.display_name == "Peanut Butter"
        assert item.category == IngredientCategory.PANTRY_DRY
        assert item.active is True
        assert item.merged_into_id is None
        assert item.staple_policy is None
        assert store.aliases(item.id) == ["peanut butter"]
        assert _event_types(store, item.id) == [ItemEventType.CREATED]

    def test_same_text_in_another_spelling_resolves_to_the_same_item(
        self, store: HouseholdItemStore
    ) -> None:
        """Case and spacing variants do not create a second item."""
        first = store.resolve_or_create("Peanut Butter", actor=ACTOR)
        second = store.resolve_or_create("  peanut   BUTTER ", actor=ACTOR)

        assert second.id == first.id
        assert sum(name.split() == ["peanut", "butter"] for name in _names(store)) == 1
        assert _event_types(store, first.id) == [ItemEventType.CREATED]

    def test_different_text_creates_a_different_item(
        self, store: HouseholdItemStore
    ) -> None:
        """Near names are not merged silently."""
        tomato = store.resolve_or_create("tomato", actor=ACTOR)
        tomatoes = store.resolve_or_create("tomatoes", actor=ACTOR)

        assert tomato.id != tomatoes.id

    def test_explicit_display_name_is_kept(self, store: HouseholdItemStore) -> None:
        """A caller's display name wins over the title-cased default."""
        item = store.resolve_or_create("pb", actor=ACTOR, display_name="PB (creamy)")

        assert item.display_name == "PB (creamy)"

    def test_blank_text_is_rejected(self, store: HouseholdItemStore) -> None:
        """An item needs a name."""
        with pytest.raises(ValueError, match="needs a name"):
            store.resolve_or_create("   ", actor=ACTOR)

    def test_losing_a_creation_race_returns_the_winners_item(
        self, store: HouseholdItemStore
    ) -> None:
        """If another writer inserts first, the caller gets that item."""
        winner = store.resolve_or_create("oat milk", actor="other-writer")
        real_resolve = HouseholdItemStore._resolve
        calls: list[str] = []

        def stale_then_real(
            self: HouseholdItemStore, conn: DatabaseConnection, key: str
        ) -> HouseholdItem | None:
            """Miss once, as a lookup made just before the other insert would."""
            calls.append(key)
            if len(calls) == 1:
                return None
            return real_resolve(self, conn, key)

        with patch.object(HouseholdItemStore, "_resolve", stale_then_real):
            item = store.resolve_or_create("oat milk", actor=ACTOR)

        assert item.id == winner.id
        assert calls == ["oat milk", "oat milk"]
        assert _event_types(store, item.id) == [ItemEventType.CREATED]

    def test_created_event_records_the_actor(self, store: HouseholdItemStore) -> None:
        """The audit trail says who made the item."""
        item = store.resolve_or_create("oat milk", actor="rubotpaul")

        event = store.history(item.id)[0]
        assert event.actor == "rubotpaul"
        assert event.detail == {"name": "oat milk"}


class TestResolveAndGet:
    """Lookups never create anything."""

    def test_resolve_unknown_text_returns_none(self, store: HouseholdItemStore) -> None:
        """Resolve is read-only."""
        before = _names(store)

        assert store.resolve("dragonfruit") is None
        assert _names(store) == before

    def test_get_unknown_id_returns_none(self, store: HouseholdItemStore) -> None:
        """A missing id is not an error for get."""
        assert store.get(99999) is None

    def test_canonical_unknown_id_raises(self, store: HouseholdItemStore) -> None:
        """Canonical resolution needs an item to start from."""
        with pytest.raises(ItemNotFoundError, match="99999"):
            store.canonical(99999)

    def test_list_items_is_sorted_by_name(self, store: HouseholdItemStore) -> None:
        """Listing is stable for display."""
        store.resolve_or_create("zucchini", actor=ACTOR)
        store.resolve_or_create("apples", actor=ACTOR)

        names = [item.name for item in store.list_items()]
        assert names == sorted(names)
        assert {"apples", "zucchini"} <= set(names)


class TestAliases:
    """Aliases are added by people, one item per alias."""

    def test_added_alias_resolves_to_the_item(self, store: HouseholdItemStore) -> None:
        """A new alias finds the same item."""
        item = store.resolve_or_create("peanut butter", actor=ACTOR)

        store.add_alias(item.id, "PB", actor=ACTOR)

        resolved = store.resolve("pb")
        assert resolved is not None
        assert resolved.id == item.id
        assert store.aliases(item.id) == ["pb", "peanut butter"]
        assert _event_types(store, item.id)[-1] == ItemEventType.ALIAS_ADDED

    def test_adding_the_same_alias_twice_is_a_no_op(
        self, store: HouseholdItemStore
    ) -> None:
        """Retries are safe and leave one event."""
        item = store.resolve_or_create("peanut butter", actor=ACTOR)
        store.add_alias(item.id, "pb", actor=ACTOR)

        store.add_alias(item.id, "PB", actor=ACTOR)

        assert _event_types(store, item.id).count(ItemEventType.ALIAS_ADDED) == 1

    def test_alias_owned_by_another_item_is_a_conflict(
        self, store: HouseholdItemStore
    ) -> None:
        """Adding an alias never merges two items."""
        butter = store.resolve_or_create("butter", actor=ACTOR)
        peanut = store.resolve_or_create("peanut butter", actor=ACTOR)

        with pytest.raises(AliasConflictError, match="already names"):
            store.add_alias(peanut.id, "butter", actor=ACTOR)

        resolved = store.resolve("butter")
        assert resolved is not None
        assert resolved.id == butter.id

    def test_blank_alias_is_rejected(self, store: HouseholdItemStore) -> None:
        """An alias needs text."""
        item = store.resolve_or_create("peanut butter", actor=ACTOR)

        with pytest.raises(ValueError, match="needs some text"):
            store.add_alias(item.id, "  ", actor=ACTOR)

    def test_alias_on_unknown_item_raises(self, store: HouseholdItemStore) -> None:
        """An alias needs an item."""
        with pytest.raises(ItemNotFoundError):
            store.add_alias(99999, "pb", actor=ACTOR)

    def test_removed_alias_no_longer_resolves(self, store: HouseholdItemStore) -> None:
        """Removing an alias is recorded and takes effect."""
        item = store.resolve_or_create("peanut butter", actor=ACTOR)
        store.add_alias(item.id, "pb", actor=ACTOR)

        store.remove_alias(item.id, "PB", actor=ACTOR)

        assert store.resolve("pb") is None
        assert _event_types(store, item.id)[-1] == ItemEventType.ALIAS_REMOVED

    def test_an_items_own_name_cannot_be_removed(
        self, store: HouseholdItemStore
    ) -> None:
        """Every item stays resolvable by its name."""
        item = store.resolve_or_create("peanut butter", actor=ACTOR)

        with pytest.raises(ValueError, match="own name"):
            store.remove_alias(item.id, "peanut butter", actor=ACTOR)

    def test_removing_an_alias_the_item_does_not_have_raises(
        self, store: HouseholdItemStore
    ) -> None:
        """A wrong alias is reported, not ignored."""
        item = store.resolve_or_create("peanut butter", actor=ACTOR)

        with pytest.raises(ValueError, match="not an alias"):
            store.remove_alias(item.id, "jam", actor=ACTOR)


class TestUpdate:
    """Policy and preferences live on the canonical item."""

    def test_sets_policy_and_preferences(self, store: HouseholdItemStore) -> None:
        """Only the fields given change, and the change is audited."""
        item = store.resolve_or_create("peanut butter", actor=ACTOR)

        updated = store.update(
            item.id,
            HouseholdItemUpdate(
                staple_policy=StaplePolicy.ALWAYS_KEEP,
                preferred_brand="Adams",
                preferred_size="16 oz",
            ),
            actor=ACTOR,
        )

        assert updated.staple_policy == StaplePolicy.ALWAYS_KEEP
        assert updated.preferred_brand == "Adams"
        assert updated.preferred_size == "16 oz"
        assert updated.display_name == "Peanut Butter"
        event = store.history(item.id)[-1]
        assert event.event_type == ItemEventType.UPDATED
        assert event.detail == {
            "staple_policy": "always_keep",
            "preferred_brand": "Adams",
            "preferred_size": "16 oz",
        }

    def test_a_field_can_be_cleared(self, store: HouseholdItemStore) -> None:
        """Passing None explicitly clears a preference."""
        item = store.resolve_or_create("peanut butter", actor=ACTOR)
        store.update(item.id, HouseholdItemUpdate(preferred_brand="Adams"), actor=ACTOR)

        updated = store.update(
            item.id, HouseholdItemUpdate(preferred_brand=None), actor=ACTOR
        )

        assert updated.preferred_brand is None

    def test_empty_update_changes_nothing(self, store: HouseholdItemStore) -> None:
        """No fields, no event."""
        item = store.resolve_or_create("peanut butter", actor=ACTOR)

        store.update(item.id, HouseholdItemUpdate(), actor=ACTOR)

        assert _event_types(store, item.id) == [ItemEventType.CREATED]

    def test_update_unknown_item_raises(self, store: HouseholdItemStore) -> None:
        """Updating needs an item."""
        with pytest.raises(ItemNotFoundError):
            store.update(99999, HouseholdItemUpdate(preferred_brand="x"), actor=ACTOR)


class TestMergeAndUnmerge:
    """A merge is a reversible pointer."""

    def test_merged_item_resolves_to_its_target(
        self, store: HouseholdItemStore
    ) -> None:
        """Both names now find the surviving item."""
        tomato = store.resolve_or_create("tomato", actor=ACTOR)
        tomatoes = store.resolve_or_create("tomatoes", actor=ACTOR)

        store.merge(tomatoes.id, tomato.id, actor=ACTOR)

        resolved = store.resolve("tomatoes")
        assert resolved is not None
        assert resolved.id == tomato.id
        assert store.canonical(tomatoes.id).id == tomato.id

    def test_merged_item_is_inactive_and_hidden_from_the_default_list(
        self, store: HouseholdItemStore
    ) -> None:
        """The source stays in the table but out of the way."""
        tomato = store.resolve_or_create("tomato", actor=ACTOR)
        tomatoes = store.resolve_or_create("tomatoes", actor=ACTOR)

        store.merge(tomatoes.id, tomato.id, actor=ACTOR)

        source = store.get(tomatoes.id)
        assert source is not None
        assert source.active is False
        assert source.merged_into_id == tomato.id
        assert "tomato" in _names(store)
        assert "tomatoes" not in _names(store)
        assert "tomatoes" in _names(store, include_inactive=True)

    def test_merge_is_audited_on_both_items(self, store: HouseholdItemStore) -> None:
        """Each side's history shows the merge and the actor."""
        tomato = store.resolve_or_create("tomato", actor=ACTOR)
        tomatoes = store.resolve_or_create("tomatoes", actor=ACTOR)

        store.merge(tomatoes.id, tomato.id, actor="jess")

        source_event = store.history(tomatoes.id)[-1]
        target_event = store.history(tomato.id)[-1]
        assert source_event.event_type == ItemEventType.MERGED
        assert source_event.detail == {"into": tomato.id}
        assert source_event.actor == "jess"
        assert target_event.event_type == ItemEventType.MERGED
        assert target_event.detail == {"absorbed": tomatoes.id}

    def test_unmerge_restores_the_earlier_state(
        self, store: HouseholdItemStore
    ) -> None:
        """Nothing was rewritten, so nothing is lost."""
        tomato = store.resolve_or_create("tomato", actor=ACTOR)
        tomatoes = store.resolve_or_create("tomatoes", actor=ACTOR)
        store.merge(tomatoes.id, tomato.id, actor=ACTOR)

        store.unmerge(tomatoes.id, actor=ACTOR)

        resolved = store.resolve("tomatoes")
        assert resolved is not None
        assert resolved.id == tomatoes.id
        assert resolved.active is True
        assert resolved.merged_into_id is None
        assert store.aliases(tomatoes.id) == ["tomatoes"]
        assert _event_types(store, tomatoes.id)[-1] == ItemEventType.UNMERGED
        assert store.history(tomato.id)[-1].detail == {"released": tomatoes.id}

    def test_item_cannot_merge_into_itself(self, store: HouseholdItemStore) -> None:
        """A self-merge would hide the item from every reader."""
        tomato = store.resolve_or_create("tomato", actor=ACTOR)

        with pytest.raises(MergeError, match="itself"):
            store.merge(tomato.id, tomato.id, actor=ACTOR)

    def test_target_must_be_canonical(self, store: HouseholdItemStore) -> None:
        """Merging into a merged item would start a chain."""
        a = store.resolve_or_create("a", actor=ACTOR)
        b = store.resolve_or_create("b", actor=ACTOR)
        c = store.resolve_or_create("c", actor=ACTOR)
        store.merge(b.id, a.id, actor=ACTOR)

        with pytest.raises(MergeError, match="already merged"):
            store.merge(c.id, b.id, actor=ACTOR)

    def test_source_must_not_already_be_merged(self, store: HouseholdItemStore) -> None:
        """Unmerge first, then merge elsewhere."""
        a = store.resolve_or_create("a", actor=ACTOR)
        b = store.resolve_or_create("b", actor=ACTOR)
        c = store.resolve_or_create("c", actor=ACTOR)
        store.merge(b.id, a.id, actor=ACTOR)

        with pytest.raises(MergeError, match="already merged"):
            store.merge(b.id, c.id, actor=ACTOR)

    def test_source_with_absorbed_items_cannot_be_merged(
        self, store: HouseholdItemStore
    ) -> None:
        """An item others point at can't become a pointer itself."""
        a = store.resolve_or_create("a", actor=ACTOR)
        b = store.resolve_or_create("b", actor=ACTOR)
        c = store.resolve_or_create("c", actor=ACTOR)
        store.merge(b.id, a.id, actor=ACTOR)

        with pytest.raises(MergeError, match="absorbed"):
            store.merge(a.id, c.id, actor=ACTOR)

    def test_merge_with_unknown_item_raises(self, store: HouseholdItemStore) -> None:
        """Both sides must exist."""
        a = store.resolve_or_create("a", actor=ACTOR)

        with pytest.raises(ItemNotFoundError):
            store.merge(a.id, 99999, actor=ACTOR)
        with pytest.raises(ItemNotFoundError):
            store.merge(99999, a.id, actor=ACTOR)

    def test_unmerge_of_an_unmerged_item_raises(
        self, store: HouseholdItemStore
    ) -> None:
        """There is nothing to undo."""
        a = store.resolve_or_create("a", actor=ACTOR)

        with pytest.raises(MergeError, match="not merged"):
            store.unmerge(a.id, actor=ACTOR)

    def test_new_text_matching_a_merged_name_does_not_recreate_it(
        self, store: HouseholdItemStore
    ) -> None:
        """resolve_or_create follows the merge instead of making a twin."""
        tomato = store.resolve_or_create("tomato", actor=ACTOR)
        tomatoes = store.resolve_or_create("tomatoes", actor=ACTOR)
        store.merge(tomatoes.id, tomato.id, actor=ACTOR)

        before = _names(store, include_inactive=True)
        again = store.resolve_or_create("Tomatoes", actor=ACTOR)

        assert again.id == tomato.id
        assert _names(store, include_inactive=True) == before


class TestWritePathsLinkItems:
    """Inventory, pantry staples and recipe ingredients carry an item id."""

    def test_inventory_item_is_linked(self, db_path: str) -> None:
        """Adding an inventory row creates or finds its item."""
        manager = PantryManager(db_path)

        manager.add_item(
            InventoryItem(
                ingredient="Oat Milk",
                display_name="Oat Milk",
                category=IngredientCategory.DAIRY,
            )
        )

        item = HouseholdItemStore(db_path).resolve("oat milk")
        assert item is not None
        assert item.category == IngredientCategory.DAIRY
        stored = manager.get_item("oat milk")
        assert stored is not None
        assert stored.item_id == item.id

    def test_pantry_staple_is_linked(self, db_path: str) -> None:
        """A staple shares the item of the same name."""
        recipes = RecipeStore(db_path)

        recipes.add_pantry_staple("Saffron", "pantry_dry")

        item = HouseholdItemStore(db_path).resolve("saffron")
        assert item is not None
        staple = next(
            s for s in recipes.get_pantry_staples() if s["ingredient"] == "saffron"
        )
        assert staple["item_id"] == item.id

    def test_recipe_ingredients_are_linked_and_shared(self, db_path: str) -> None:
        """A recipe ingredient and an inventory row of one name share one item."""
        PantryManager(db_path).add_item(
            InventoryItem(ingredient="rice", display_name="Rice")
        )
        meal = ParsedMeal(
            name="Fried Rice",
            servings=2,
            known_recipe=False,
            needs_confirmation=False,
            purchase_items=[
                Ingredient(
                    ingredient="Rice",
                    quantity=1,
                    unit=Unit.CUP,
                    category=IngredientCategory.PANTRY_DRY,
                )
            ],
            pantry_items=[
                Ingredient(
                    ingredient="soy sauce",
                    quantity=1,
                    unit=Unit.TBSP,
                    category=IngredientCategory.PANTRY_DRY,
                    is_pantry_item=True,
                )
            ],
        )

        recipe_id = RecipeStore(db_path).save_recipe(meal)

        items = HouseholdItemStore(db_path)
        rice = items.resolve("rice")
        assert rice is not None
        with closing(get_connection(db_path)) as conn:
            rows = conn.execute(
                "SELECT ingredient, item_id FROM recipe_ingredients"
                " WHERE recipe_id = ? ORDER BY ingredient",
                (recipe_id,),
            ).fetchall()
        linked = {row["ingredient"]: row["item_id"] for row in rows}
        assert linked["Rice"] == rice.id
        assert linked["soy sauce"] is not None
        assert len([i for i in items.list_items() if i.name == "rice"]) == 1


class TestBackfill:
    """Migration 010's hook links rows written before items existed."""

    def test_links_unlinked_rows_and_is_idempotent(self, db_path: str) -> None:
        """Rows without an item id get one; a second run changes nothing."""
        with closing(get_connection(db_path)) as conn:
            conn.execute(
                "INSERT INTO household_inventory (ingredient, display_name, category)"
                " VALUES (?, ?, ?)",
                ("legacy milk", "Legacy Milk", "dairy"),
            )
            conn.execute(
                "INSERT INTO pantry_staples (ingredient, display_name, category)"
                " VALUES (?, ?, ?)",
                ("legacy milk", "Legacy Milk", "dairy"),
            )
            conn.commit()

        backfill_item_links(db_path)
        backfill_item_links(db_path)

        store = HouseholdItemStore(db_path)
        item = store.resolve("legacy milk")
        assert item is not None
        assert item.display_name == "Legacy Milk"
        assert item.category == IngredientCategory.DAIRY
        assert _event_types(store, item.id) == [ItemEventType.CREATED]
        assert store.history(item.id)[0].actor == "migration"
        with closing(get_connection(db_path)) as conn:
            for query in (
                "SELECT item_id FROM household_inventory WHERE ingredient = ?",
                "SELECT item_id FROM pantry_staples WHERE ingredient = ?",
            ):
                rows = conn.execute(query, ("legacy milk",)).fetchall()
                assert [row["item_id"] for row in rows] == [item.id]

    def test_seeded_pantry_staples_are_linked_by_the_migration(
        self, db_path: str
    ) -> None:
        """A freshly migrated database has no unlinked staple."""
        with closing(get_connection(db_path)) as conn:
            links = [
                row["item_id"]
                for row in conn.execute("SELECT item_id FROM pantry_staples").fetchall()
            ]

        assert links
        assert None not in links

    def test_unknown_category_text_falls_back_to_other(self, db_path: str) -> None:
        """Old rows can hold a category the enum no longer has."""
        with closing(get_connection(db_path)) as conn:
            conn.execute(
                "INSERT INTO pantry_staples (ingredient, display_name, category)"
                " VALUES (?, ?, ?)",
                ("mystery spice", "Mystery Spice", "not-a-category"),
            )
            conn.commit()

        backfill_item_links(db_path)

        item = HouseholdItemStore(db_path).resolve("mystery spice")
        assert item is not None
        assert item.category == IngredientCategory.OTHER


_PG_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.mark.skipif(not _PG_URL, reason="TEST_DATABASE_URL not set")
class TestPostgres:
    """The store behaves the same on Postgres."""

    @pytest.fixture()
    def pg_store(self) -> Iterator[HouseholdItemStore]:
        """Yield a store on the Postgres test database, cleaning its rows."""
        init_db(_PG_URL)
        self._clean()
        yield HouseholdItemStore(_PG_URL)
        self._clean()

    def _clean(self) -> None:
        """Delete this suite's items from the shared test database."""
        with closing(get_connection(_PG_URL)) as conn:
            ids = [
                row["id"]
                for row in conn.execute(
                    "SELECT id FROM household_items WHERE name LIKE ?", ("pgt %",)
                ).fetchall()
            ]
            for item_id in ids:
                conn.execute(
                    "UPDATE household_items SET merged_into_id = NULL"
                    " WHERE merged_into_id = ?",
                    (item_id,),
                )
            for item_id in ids:
                for statement in (
                    "DELETE FROM household_item_aliases WHERE item_id = ?",
                    "DELETE FROM household_item_events WHERE item_id = ?",
                    "DELETE FROM household_items WHERE id = ?",
                ):
                    conn.execute(statement, (item_id,))
            conn.commit()

    def test_create_alias_merge_unmerge_round_trip(
        self, pg_store: HouseholdItemStore
    ) -> None:
        """The whole lifecycle works through the Postgres dialect."""
        tomato = pg_store.resolve_or_create("pgt tomato", actor=ACTOR)
        tomatoes = pg_store.resolve_or_create("PGT  Tomatoes", actor=ACTOR)
        pg_store.add_alias(tomato.id, "pgt roma", actor=ACTOR)

        pg_store.merge(tomatoes.id, tomato.id, actor=ACTOR)
        merged = pg_store.resolve("pgt tomatoes")
        pg_store.unmerge(tomatoes.id, actor=ACTOR)
        released = pg_store.resolve("pgt tomatoes")

        assert merged is not None
        assert merged.id == tomato.id
        assert released is not None
        assert released.id == tomatoes.id
        assert released.active is True
        assert pg_store.aliases(tomato.id) == ["pgt roma", "pgt tomato"]

    def test_alias_conflict_is_reported(self, pg_store: HouseholdItemStore) -> None:
        """The unique alias rule holds on Postgres."""
        a = pg_store.resolve_or_create("pgt a", actor=ACTOR)
        pg_store.resolve_or_create("pgt b", actor=ACTOR)

        with pytest.raises(AliasConflictError):
            pg_store.add_alias(a.id, "pgt b", actor=ACTOR)
