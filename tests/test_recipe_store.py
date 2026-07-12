"""Tests for grocery_butler.recipe_store module."""

from __future__ import annotations

import json
import os
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

from grocery_butler.db import get_connection
from grocery_butler.db.migrate import migrate
from grocery_butler.models import (
    BrandMatchType,
    BrandPreference,
    BrandPreferenceType,
    Ingredient,
    IngredientCategory,
    ParsedMeal,
)
from grocery_butler.recipe_store import RecipeStore, normalize_recipe_name

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a temporary database path.

    Args:
        tmp_path: Pytest temporary directory.

    Returns:
        Path string for a fresh database.
    """
    return str(tmp_path / "test.db")


@pytest.fixture()
def store(db_path: str) -> RecipeStore:
    """Return a RecipeStore backed by a fresh temporary database.

    Args:
        db_path: Path to temporary database.

    Returns:
        Initialized RecipeStore instance.
    """
    return RecipeStore(db_path)


@pytest.fixture()
def sample_meal() -> ParsedMeal:
    """Return a sample ParsedMeal for testing.

    Returns:
        A tacos meal with purchase and pantry items.
    """
    return ParsedMeal(
        name="Chicken Tacos",
        servings=4,
        known_recipe=True,
        needs_confirmation=False,
        purchase_items=[
            Ingredient(
                ingredient="chicken thighs",
                quantity=2.0,
                unit="lbs",
                category=IngredientCategory.MEAT,
            ),
            Ingredient(
                ingredient="corn tortillas",
                quantity=12.0,
                unit="each",
                category=IngredientCategory.BAKERY,
            ),
        ],
        pantry_items=[
            Ingredient(
                ingredient="olive oil",
                quantity=2.0,
                unit="tbsp",
                category=IngredientCategory.PANTRY_DRY,
                is_pantry_item=True,
            ),
        ],
    )


# ---------------------------------------------------------------------------
# normalize_recipe_name tests
# ---------------------------------------------------------------------------


class TestNormalizeRecipeName:
    """Tests for name normalization."""

    def test_lowercases(self) -> None:
        """Test names are lowercased."""
        assert normalize_recipe_name("TACOS") == "tacos"

    def test_strips_whitespace(self) -> None:
        """Test leading/trailing whitespace is stripped."""
        assert normalize_recipe_name("  tacos  ") == "tacos"

    def test_strips_leading_a(self) -> None:
        """Test leading article 'a' is stripped."""
        assert normalize_recipe_name("A Simple Pasta") == "simple pasta"

    def test_strips_leading_an(self) -> None:
        """Test leading article 'an' is stripped."""
        assert normalize_recipe_name("An Easy Soup") == "easy soup"

    def test_strips_leading_the(self) -> None:
        """Test leading article 'the' is stripped."""
        assert normalize_recipe_name("The Best Tacos") == "best tacos"

    def test_normalizes_possessives(self) -> None:
        """Test possessives are normalized."""
        assert normalize_recipe_name("Mom's Tacos") == "moms tacos"

    def test_removes_punctuation(self) -> None:
        """Test punctuation is removed."""
        assert normalize_recipe_name("mac & cheese!") == "mac cheese"

    def test_collapses_whitespace(self) -> None:
        """Test multiple spaces are collapsed."""
        assert normalize_recipe_name("mac  and   cheese") == "mac and cheese"

    def test_full_normalization(self) -> None:
        """Test full normalization pipeline."""
        result = normalize_recipe_name("Mom's Chicken Tikka Masala")
        assert result == "moms chicken tikka masala"

    def test_empty_string(self) -> None:
        """Test empty string normalizes to empty."""
        assert normalize_recipe_name("") == ""

    def test_article_only(self) -> None:
        """Test string that is just an article still normalizes."""
        assert normalize_recipe_name("the") == "the"

    def test_preserves_non_article_a(self) -> None:
        """Test 'a' inside a name is preserved."""
        assert normalize_recipe_name("pasta alla norma") == "pasta alla norma"


# ---------------------------------------------------------------------------
# Recipe CRUD tests
# ---------------------------------------------------------------------------


class TestRecipeCRUD:
    """Tests for recipe create/read/update/delete."""

    def test_save_and_get_recipe(
        self, store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test saving and retrieving a recipe."""
        recipe_id = store.save_recipe(sample_meal)
        assert recipe_id > 0

        result = store.get_recipe("Chicken Tacos")
        assert result is not None
        assert result.name == "Chicken Tacos"
        assert result.servings == 4
        assert len(result.purchase_items) == 2
        assert len(result.pantry_items) == 1

    def test_get_recipe_normalized_lookup(
        self, store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test recipes are found regardless of name casing."""
        store.save_recipe(sample_meal)
        result = store.get_recipe("CHICKEN TACOS")
        assert result is not None
        assert result.name == "Chicken Tacos"

    def test_get_recipe_not_found(self, store: RecipeStore) -> None:
        """Test get_recipe returns None for unknown recipes."""
        assert store.get_recipe("nonexistent") is None

    def test_get_recipe_by_id(
        self, store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test retrieving a recipe by database ID."""
        recipe_id = store.save_recipe(sample_meal)
        result = store.get_recipe_by_id(recipe_id)
        assert result is not None
        assert result.name == "Chicken Tacos"

    def test_get_recipe_by_id_not_found(self, store: RecipeStore) -> None:
        """Test get_recipe_by_id returns None for unknown ID."""
        assert store.get_recipe_by_id(9999) is None

    def test_update_recipe(self, store: RecipeStore, sample_meal: ParsedMeal) -> None:
        """Test updating a recipe replaces ingredients."""
        recipe_id = store.save_recipe(sample_meal)

        updated = ParsedMeal(
            name="Chicken Tacos",
            servings=6,
            known_recipe=True,
            needs_confirmation=False,
            purchase_items=[
                Ingredient(
                    ingredient="chicken breast",
                    quantity=3.0,
                    unit="lbs",
                    category=IngredientCategory.MEAT,
                ),
            ],
            pantry_items=[],
        )
        store.update_recipe(recipe_id, updated)

        result = store.get_recipe_by_id(recipe_id)
        assert result is not None
        assert result.servings == 6
        assert len(result.purchase_items) == 1
        assert result.purchase_items[0].ingredient == "chicken breast"
        assert len(result.pantry_items) == 0

    def test_delete_recipe(self, store: RecipeStore, sample_meal: ParsedMeal) -> None:
        """Test deleting a recipe removes it completely."""
        recipe_id = store.save_recipe(sample_meal)
        store.delete_recipe(recipe_id)
        assert store.get_recipe_by_id(recipe_id) is None

    def test_delete_recipe_cascades_ingredients(
        self, store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test deleting a recipe also removes its ingredients."""
        recipe_id = store.save_recipe(sample_meal)
        store.delete_recipe(recipe_id)

        from grocery_butler.db import get_connection

        conn = get_connection(store._db_path)
        try:
            rows = conn.execute(
                "SELECT * FROM recipe_ingredients WHERE recipe_id = ?",
                (recipe_id,),
            ).fetchall()
            assert len(rows) == 0
        finally:
            conn.close()

    def test_list_recipes_empty(self, store: RecipeStore) -> None:
        """Test list_recipes returns empty list when no recipes."""
        result = store.list_recipes()
        assert result == []

    def test_list_recipes(self, store: RecipeStore, sample_meal: ParsedMeal) -> None:
        """Test list_recipes returns summary dicts."""
        store.save_recipe(sample_meal)
        result = store.list_recipes()
        assert len(result) == 1
        assert result[0]["display_name"] == "Chicken Tacos"
        assert result[0]["times_ordered"] == 0

    def test_list_recipes_ordered_by_name(self, store: RecipeStore) -> None:
        """Test list_recipes returns recipes in alphabetical order."""
        for name in ["Pasta", "Burgers", "Tacos"]:
            store.save_recipe(
                ParsedMeal(
                    name=name,
                    servings=4,
                    known_recipe=True,
                    needs_confirmation=False,
                    purchase_items=[],
                    pantry_items=[],
                )
            )
        result = store.list_recipes()
        names = [r["display_name"] for r in result]
        assert names == ["Burgers", "Pasta", "Tacos"]

    def test_increment_times_ordered(
        self, store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test incrementing times_ordered counter."""
        recipe_id = store.save_recipe(sample_meal)
        store.increment_times_ordered(recipe_id)
        store.increment_times_ordered(recipe_id)

        result = store.list_recipes()
        assert result[0]["times_ordered"] == 2

    def test_export_recipe_json(
        self, store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test exporting a recipe as JSON."""
        recipe_id = store.save_recipe(sample_meal)
        result = store.export_recipe_json(recipe_id)
        assert result is not None

        data = json.loads(result)
        assert data["name"] == "Chicken Tacos"
        assert data["servings"] == 4
        assert len(data["purchase_items"]) == 2

    def test_export_recipe_json_not_found(self, store: RecipeStore) -> None:
        """Test export returns None for unknown recipe."""
        assert store.export_recipe_json(9999) is None

    def test_ingredient_quantities_preserved(
        self, store: RecipeStore, sample_meal: ParsedMeal
    ) -> None:
        """Test ingredient quantities survive round-trip."""
        store.save_recipe(sample_meal)
        result = store.get_recipe("Chicken Tacos")
        assert result is not None

        chicken = next(i for i in result.purchase_items if "chicken" in i.ingredient)
        assert chicken.quantity == 2.0
        assert chicken.unit == "lb"
        assert chicken.category == IngredientCategory.MEAT


# ---------------------------------------------------------------------------
# Fuzzy recipe lookup tests
# ---------------------------------------------------------------------------


class TestFindRecipe:
    """Tests for fuzzy recipe matching."""

    def test_exact_match(self, store: RecipeStore, sample_meal: ParsedMeal) -> None:
        """Test exact name match."""
        store.save_recipe(sample_meal)
        result = store.find_recipe("Chicken Tacos")
        assert result is not None
        assert result.name == "Chicken Tacos"

    def test_substring_match(self, store: RecipeStore, sample_meal: ParsedMeal) -> None:
        """Test substring matching when exact match fails."""
        store.save_recipe(sample_meal)
        result = store.find_recipe("chicken")
        assert result is not None
        assert result.name == "Chicken Tacos"

    def test_no_match(self, store: RecipeStore) -> None:
        """Test None returned when no match found."""
        result = store.find_recipe("nonexistent meal")
        assert result is None

    def test_prefers_exact_over_substring(self, store: RecipeStore) -> None:
        """Test exact match is preferred over substring match."""
        store.save_recipe(
            ParsedMeal(
                name="Chicken",
                servings=4,
                known_recipe=True,
                needs_confirmation=False,
                purchase_items=[],
                pantry_items=[],
            )
        )
        store.save_recipe(
            ParsedMeal(
                name="Chicken Tacos",
                servings=4,
                known_recipe=True,
                needs_confirmation=False,
                purchase_items=[],
                pantry_items=[],
            )
        )
        result = store.find_recipe("Chicken")
        assert result is not None
        assert result.name == "Chicken"


# ---------------------------------------------------------------------------
# Pantry staple tests
# ---------------------------------------------------------------------------


class TestPantryStaples:
    """Tests for pantry staple CRUD operations."""

    def test_get_pantry_staples_seeded(self, store: RecipeStore) -> None:
        """Test seeded pantry staples are returned."""
        result = store.get_pantry_staples()
        assert len(result) > 0
        names = [s["ingredient"] for s in result]
        assert "salt" in names
        assert "olive oil" in names

    def test_get_pantry_staple_names(self, store: RecipeStore) -> None:
        """Test getting just the names."""
        names = store.get_pantry_staple_names()
        assert isinstance(names, list)
        assert "salt" in names
        assert all(isinstance(n, str) for n in names)

    def test_add_pantry_staple(self, store: RecipeStore) -> None:
        """Test adding a new pantry staple."""
        staple_id = store.add_pantry_staple("cumin", "pantry_dry")
        assert staple_id > 0

        names = store.get_pantry_staple_names()
        assert "cumin" in names

    def test_add_pantry_staple_display_name(self, store: RecipeStore) -> None:
        """Test display name is auto-generated as title case."""
        store.add_pantry_staple("sesame oil", "pantry_dry")
        staples = store.get_pantry_staples()
        sesame = next(s for s in staples if s["ingredient"] == "sesame oil")
        assert sesame["display_name"] == "Sesame Oil"

    def test_remove_pantry_staple(self, store: RecipeStore) -> None:
        """Test removing a pantry staple."""
        staple_id = store.add_pantry_staple("cumin", "pantry_dry")
        store.remove_pantry_staple(staple_id)

        names = store.get_pantry_staple_names()
        assert "cumin" not in names

    def test_is_pantry_staple_true(self, store: RecipeStore) -> None:
        """Test is_pantry_staple returns True for known staples."""
        assert store.is_pantry_staple("salt") is True

    def test_is_pantry_staple_false(self, store: RecipeStore) -> None:
        """Test is_pantry_staple returns False for non-staples."""
        assert store.is_pantry_staple("chicken thighs") is False

    def test_is_pantry_staple_case_insensitive(self, store: RecipeStore) -> None:
        """Test is_pantry_staple is case insensitive."""
        assert store.is_pantry_staple("SALT") is True
        assert store.is_pantry_staple("Salt") is True


# ---------------------------------------------------------------------------
# Preferences tests
# ---------------------------------------------------------------------------


class TestPreferences:
    """Tests for preference CRUD operations."""

    def test_get_all_preferences_seeded(self, store: RecipeStore) -> None:
        """Test seeded preferences are returned."""
        prefs = store.get_all_preferences()
        assert "default_servings" in prefs
        assert prefs["default_servings"] == "4"

    def test_get_preference(self, store: RecipeStore) -> None:
        """Test getting a single preference."""
        value = store.get_preference("default_servings")
        assert value == "4"

    def test_get_preference_missing(self, store: RecipeStore) -> None:
        """Test getting a non-existent preference returns None."""
        assert store.get_preference("nonexistent_key") is None

    def test_set_preference_insert(self, store: RecipeStore) -> None:
        """Test inserting a new preference."""
        store.set_preference("theme", "dark")
        assert store.get_preference("theme") == "dark"

    def test_set_preference_upsert(self, store: RecipeStore) -> None:
        """Test upserting an existing preference."""
        store.set_preference("default_servings", "6")
        assert store.get_preference("default_servings") == "6"

    def test_set_preference_uses_returning_to_avoid_adapter_id_injection(
        self, store: RecipeStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test set_preference's SQL carries its own RETURNING clause.

        Without an explicit ``RETURNING`` clause, the PostgreSQL adapter's
        ``_inject_returning`` appends ``RETURNING id`` to any bare INSERT
        statement. The ``preferences`` table has no ``id`` column, so that
        blind injection raises ``UndefinedColumn`` on Postgres. Emitting
        ``RETURNING key`` ourselves means the SQL already contains
        "RETURNING", so the adapter's injection is skipped.

        Args:
            store: RecipeStore fixture backed by a temporary SQLite file.
            monkeypatch: Pytest fixture for patching attributes.
        """
        mock_conn = MagicMock()
        monkeypatch.setattr(store, "_connect", lambda: mock_conn)

        store.set_preference("theme", "dark")

        mock_conn.execute.assert_called_once_with(
            "INSERT INTO preferences (key, value) VALUES (?, ?)"
            " ON CONFLICT(key) DO UPDATE SET value = excluded.value"
            " RETURNING key",
            ("theme", "dark"),
        )

    def test_set_preference_drains_returning_before_commit(
        self, store: RecipeStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test the RETURNING result set is drained before commit.

        PostgreSQL requires the result set of a ``RETURNING`` clause to
        be consumed before the transaction commits; leaving it undrained
        can leave the connection in an unusable state. This mirrors the
        pattern already used by ``_record_migration`` and
        ``PendingActionsStore``.

        Args:
            store: RecipeStore fixture backed by a temporary SQLite file.
            monkeypatch: Pytest fixture for patching attributes.
        """
        mock_conn = MagicMock()
        monkeypatch.setattr(store, "_connect", lambda: mock_conn)

        store.set_preference("theme", "dark")

        mock_conn.execute.return_value.fetchall.assert_called_once()
        mock_conn.commit.assert_called_once()


# ---------------------------------------------------------------------------
# Brand preference tests
# ---------------------------------------------------------------------------


class TestBrandPreferences:
    """Tests for brand preference CRUD operations."""

    def test_get_brand_preferences_empty(self, store: RecipeStore) -> None:
        """Test empty brand preferences list."""
        result = store.get_brand_preferences()
        assert result == []

    def test_add_brand_preference(self, store: RecipeStore) -> None:
        """Test adding a brand preference."""
        pref = BrandPreference(
            match_target="dairy",
            match_type=BrandMatchType.CATEGORY,
            brand="Organic Valley",
            preference_type=BrandPreferenceType.PREFERRED,
        )
        pref_id = store.add_brand_preference(pref)
        assert pref_id > 0

        result = store.get_brand_preferences()
        assert len(result) == 1
        assert result[0].brand == "Organic Valley"

    def test_remove_brand_preference(self, store: RecipeStore) -> None:
        """Test removing a brand preference."""
        pref = BrandPreference(
            match_target="dairy",
            match_type=BrandMatchType.CATEGORY,
            brand="Store Brand",
            preference_type=BrandPreferenceType.AVOID,
        )
        pref_id = store.add_brand_preference(pref)
        store.remove_brand_preference(pref_id)

        assert store.get_brand_preferences() == []

    def test_get_brands_for_ingredient_level(self, store: RecipeStore) -> None:
        """Test ingredient-level brand preference lookup."""
        pref = BrandPreference(
            match_target="milk",
            match_type=BrandMatchType.INGREDIENT,
            brand="Organic Valley",
            preference_type=BrandPreferenceType.PREFERRED,
        )
        store.add_brand_preference(pref)

        result = store.get_brands_for_ingredient("milk")
        assert len(result) == 1
        assert result[0].brand == "Organic Valley"

    def test_get_brands_for_category_fallback(self, store: RecipeStore) -> None:
        """Test category-level fallback when no ingredient match."""
        pref = BrandPreference(
            match_target="dairy",
            match_type=BrandMatchType.CATEGORY,
            brand="Organic Valley",
            preference_type=BrandPreferenceType.PREFERRED,
        )
        store.add_brand_preference(pref)

        result = store.get_brands_for_ingredient("yogurt", category="dairy")
        assert len(result) == 1
        assert result[0].brand == "Organic Valley"

    def test_ingredient_overrides_category(self, store: RecipeStore) -> None:
        """Test ingredient-level prefs override category-level."""
        store.add_brand_preference(
            BrandPreference(
                match_target="dairy",
                match_type=BrandMatchType.CATEGORY,
                brand="Generic",
                preference_type=BrandPreferenceType.PREFERRED,
            )
        )
        store.add_brand_preference(
            BrandPreference(
                match_target="milk",
                match_type=BrandMatchType.INGREDIENT,
                brand="Organic Valley",
                preference_type=BrandPreferenceType.PREFERRED,
            )
        )

        result = store.get_brands_for_ingredient("milk", category="dairy")
        assert len(result) == 1
        assert result[0].brand == "Organic Valley"

    def test_get_brands_no_match(self, store: RecipeStore) -> None:
        """Test no brand preferences returns empty list."""
        result = store.get_brands_for_ingredient("chicken")
        assert result == []

    def test_get_brands_no_category_no_match(self, store: RecipeStore) -> None:
        """Test no match without category returns empty list."""
        store.add_brand_preference(
            BrandPreference(
                match_target="dairy",
                match_type=BrandMatchType.CATEGORY,
                brand="Generic",
                preference_type=BrandPreferenceType.PREFERRED,
            )
        )
        result = store.get_brands_for_ingredient("chicken")
        assert result == []

    def test_get_brand_preferences_populates_id(self, store: RecipeStore) -> None:
        """Test get_brand_preferences populates the real DB id on each pref."""
        pref_a = BrandPreference(
            match_target="dairy",
            match_type=BrandMatchType.CATEGORY,
            brand="Organic Valley",
            preference_type=BrandPreferenceType.PREFERRED,
        )
        pref_b = BrandPreference(
            match_target="soda",
            match_type=BrandMatchType.CATEGORY,
            brand="Generic Cola",
            preference_type=BrandPreferenceType.AVOID,
        )
        id_a = store.add_brand_preference(pref_a)
        id_b = store.add_brand_preference(pref_b)

        result = store.get_brand_preferences()
        by_brand = {pref.brand: pref for pref in result}
        assert by_brand["Organic Valley"].id == id_a
        assert by_brand["Generic Cola"].id == id_b

    def test_get_brands_for_ingredient_populates_id(self, store: RecipeStore) -> None:
        """Test get_brands_for_ingredient (via _rows_to_brand_prefs) sets id."""
        pref = BrandPreference(
            match_target="milk",
            match_type=BrandMatchType.INGREDIENT,
            brand="Organic Valley",
            preference_type=BrandPreferenceType.PREFERRED,
        )
        pref_id = store.add_brand_preference(pref)

        result = store.get_brands_for_ingredient("milk")
        assert len(result) == 1
        assert result[0].id == pref_id

    def test_remove_brand_preference_returns_true_when_deleted(
        self, store: RecipeStore
    ) -> None:
        """Test remove_brand_preference returns True for an existing id."""
        pref = BrandPreference(
            match_target="dairy",
            match_type=BrandMatchType.CATEGORY,
            brand="Store Brand",
            preference_type=BrandPreferenceType.AVOID,
        )
        pref_id = store.add_brand_preference(pref)

        result = store.remove_brand_preference(pref_id)

        assert result is True

    def test_remove_brand_preference_returns_false_when_missing(
        self, store: RecipeStore
    ) -> None:
        """Test remove_brand_preference returns False for a nonexistent id."""
        result = store.remove_brand_preference(9999)

        assert result is False


# ---------------------------------------------------------------------------
# PostgreSQL integration tests (require a running Postgres instance)
# ---------------------------------------------------------------------------

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "")
_skip_no_pg = pytest.mark.skipif(
    not _TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — no Postgres server available",
)


@_skip_no_pg
class TestRecipeStorePostgres:
    """Integration tests against PostgreSQL.

    Requires a running PostgreSQL instance. Set TEST_DATABASE_URL
    to run: ``TEST_DATABASE_URL=postgresql://user:pass@localhost/test``.
    """

    @pytest.fixture
    def pg_store(self) -> Iterator[RecipeStore]:
        """Yield a RecipeStore backed by PostgreSQL, cleaning up test rows.

        Runs migrations, deletes any leftover ``pg-test-%`` preference
        rows before yielding the store, then deletes them again on
        teardown so runs don't leak state between tests.

        Yields:
            A RecipeStore connected to TEST_DATABASE_URL.
        """
        migrate(_TEST_DB_URL)
        conn = get_connection(_TEST_DB_URL)
        try:
            conn.execute("DELETE FROM preferences WHERE key LIKE ?", ("pg-test-%",))
            conn.commit()
        finally:
            conn.close()

        yield RecipeStore(_TEST_DB_URL)

        conn = get_connection(_TEST_DB_URL)
        try:
            conn.execute("DELETE FROM preferences WHERE key LIKE ?", ("pg-test-%",))
            conn.commit()
        finally:
            conn.close()

    def test_set_preference_persists_on_postgres(self, pg_store: RecipeStore) -> None:
        """Test a preference set via set_preference is durably stored."""
        pg_store.set_preference("pg-test-theme", "dark")

        assert pg_store.get_preference("pg-test-theme") == "dark"

    def test_set_preference_upsert_overwrites_on_postgres(
        self, pg_store: RecipeStore
    ) -> None:
        """Test setting the same key twice overwrites the value (upsert)."""
        pg_store.set_preference("pg-test-theme", "light")
        pg_store.set_preference("pg-test-theme", "dark")

        assert pg_store.get_preference("pg-test-theme") == "dark"
        all_prefs = pg_store.get_all_preferences()
        pg_test_prefs = {
            key: value for key, value in all_prefs.items() if key.startswith("pg-test-")
        }
        assert pg_test_prefs == {"pg-test-theme": "dark"}
