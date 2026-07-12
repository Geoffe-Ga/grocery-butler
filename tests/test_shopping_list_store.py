"""Tests for grocery_butler.shopping_list_store module.

Issue #65 (HIGH): POST /shopping-list/generate used to store the
generated list in the Flask session cookie, which caps out at ~4KB and
is scoped to a single browser. ShoppingListStore replaces that with
DB-backed persistence (imitating the RecipeStore/PantryManager
connection-per-operation pattern) so lists of any size are visible to
every household member.

These are unit tests for the store itself; app-level (route) behavior
is covered in tests/test_app.py.
"""

from __future__ import annotations

import json
from typing import TYPE_CHECKING

import pytest

from grocery_butler.models import IngredientCategory, ShoppingListItem
from grocery_butler.shopping_list_store import ShoppingListStore

if TYPE_CHECKING:
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
def store(db_path: str) -> ShoppingListStore:
    """Return a ShoppingListStore backed by a fresh temporary database.

    Args:
        db_path: Path to temporary database.

    Returns:
        Initialized ShoppingListStore instance.
    """
    return ShoppingListStore(db_path)


@pytest.fixture()
def sample_items() -> list[ShoppingListItem]:
    """Return a small multi-meal shopping list for round-trip tests.

    Returns:
        List of ShoppingListItem covering multiple provenance meals.
    """
    return [
        ShoppingListItem(
            ingredient="chicken thighs",
            quantity=2.0,
            unit="lbs",
            category=IngredientCategory.MEAT,
            search_term="boneless chicken thighs",
            from_meals=["Chicken Tacos", "Chicken Stir Fry"],
        ),
        ShoppingListItem(
            ingredient="milk",
            quantity=1.0,
            unit="gallon",
            category=IngredientCategory.DAIRY,
            search_term="whole milk",
            from_meals=["restock"],
        ),
    ]


def _make_items(count: int, *, prefix: str = "ingredient") -> list[ShoppingListItem]:
    """Build a list of distinct ShoppingListItem for bulk tests.

    Args:
        count: Number of items to generate.
        prefix: Prefix used for ingredient/search_term naming.

    Returns:
        List of distinct ShoppingListItem instances.
    """
    return [
        ShoppingListItem(
            ingredient=f"{prefix}-{i}",
            quantity=1.5,
            unit="each",
            category=IngredientCategory.OTHER,
            search_term=f"{prefix}-{i}",
            from_meals=[f"Meal {i}"],
        )
        for i in range(count)
    ]


# ---------------------------------------------------------------------------
# save_list / get_latest_list round-trip
# ---------------------------------------------------------------------------


class TestSaveAndGetLatestList:
    """Tests for the save_list -> get_latest_list round trip."""

    def test_get_latest_list_no_lists_returns_empty(
        self, store: ShoppingListStore
    ) -> None:
        """Test get_latest_list returns [] when nothing has been saved."""
        assert store.get_latest_list() == []

    def test_save_list_returns_int_id(
        self, store: ShoppingListStore, sample_items: list[ShoppingListItem]
    ) -> None:
        """Test save_list returns an integer list ID."""
        list_id = store.save_list(sample_items)
        assert isinstance(list_id, int)

    def test_round_trip_preserves_field_values(
        self, store: ShoppingListStore, sample_items: list[ShoppingListItem]
    ) -> None:
        """Test every field round-trips exactly through save/get."""
        store.save_list(sample_items)
        result = store.get_latest_list()

        assert len(result) == len(sample_items)
        by_ingredient = {item.ingredient: item for item in result}

        chicken = by_ingredient["chicken thighs"]
        assert chicken.quantity == 2.0
        # ShoppingListItem's Unit field_validator normalizes "lbs" -> Unit.LB
        # ("lb") on construction, before the store ever sees it, so the
        # fixture's raw input string is not what round-trips.
        assert chicken.unit == "lb"
        assert chicken.category == IngredientCategory.MEAT
        assert chicken.search_term == "boneless chicken thighs"
        assert chicken.from_meals == ["Chicken Tacos", "Chicken Stir Fry"]

        milk = by_ingredient["milk"]
        assert milk.quantity == 1.0
        # Same normalization: "gallon" -> Unit.GAL ("gal").
        assert milk.unit == "gal"
        assert milk.category == IngredientCategory.DAIRY
        assert milk.search_term == "whole milk"
        assert milk.from_meals == ["restock"]

    def test_round_trip_returns_shopping_list_item_instances(
        self, store: ShoppingListStore, sample_items: list[ShoppingListItem]
    ) -> None:
        """Test get_latest_list returns real ShoppingListItem models."""
        store.save_list(sample_items)
        result = store.get_latest_list()
        assert all(isinstance(item, ShoppingListItem) for item in result)

    def test_restock_from_meals_round_trips_as_list_for_template_check(
        self, store: ShoppingListStore
    ) -> None:
        """Test restock provenance round-trips as ``["restock"]`` exactly.

        The shopping_list.html template distinguishes restock items via
        ``item.from_meals == ['restock']``; from_meals must come back as
        a real list of strings, not a JSON string or tuple.
        """
        store.save_list(
            [
                ShoppingListItem(
                    ingredient="butter",
                    quantity=1.0,
                    unit="block",
                    category=IngredientCategory.DAIRY,
                    search_term="butter",
                    from_meals=["restock"],
                )
            ]
        )
        result = store.get_latest_list()
        assert result[0].from_meals == ["restock"]

    def test_latest_wins_second_save_replaces_first_in_get_latest(
        self, store: ShoppingListStore
    ) -> None:
        """Test get_latest_list returns only the most recently saved list."""
        store.save_list(_make_items(2, prefix="first"))
        store.save_list(_make_items(3, prefix="second"))

        result = store.get_latest_list()

        assert len(result) == 3
        assert all(item.ingredient.startswith("second-") for item in result)

    def test_large_list_round_trips_completely(self, store: ShoppingListStore) -> None:
        """Test a 45-item list with long provenance round-trips in full.

        Encodes the truncation acceptance criterion from issue #65: a
        list this size, with verbose from_meals provenance, serializes
        to well over 4KB of JSON -- comfortably past the session cookie
        cap that caused the original bug.
        """
        items = [
            ShoppingListItem(
                ingredient=f"large-list-ingredient-{i}",
                quantity=float(i + 1),
                unit="each",
                category=IngredientCategory.OTHER,
                search_term=f"large-list-ingredient-{i} search term",
                from_meals=[
                    f"A Very Long Descriptive Meal Name Number {i} "
                    "That Contributes This Ingredient To The List"
                ],
            )
            for i in range(45)
        ]
        serialized_size = len(
            json.dumps([item.model_dump(mode="json") for item in items])
        )
        assert serialized_size > 4096, "fixture must exceed the cookie cap"

        store.save_list(items)
        result = store.get_latest_list()

        assert len(result) == 45
        by_ingredient = {item.ingredient: item for item in result}
        for i in range(45):
            name = f"large-list-ingredient-{i}"
            assert by_ingredient[name].quantity == float(i + 1)
            assert by_ingredient[name].from_meals == [
                f"A Very Long Descriptive Meal Name Number {i} "
                "That Contributes This Ingredient To The List"
            ]

    def test_unicode_ingredient_round_trips(self, store: ShoppingListStore) -> None:
        """Test a unicode ingredient name round-trips without corruption."""
        store.save_list(
            [
                ShoppingListItem(
                    ingredient="jalapeño",
                    quantity=3.0,
                    unit="each",
                    category=IngredientCategory.PRODUCE,
                    search_term="jalapeño pepper",
                    from_meals=["Tacos al Pastor"],
                )
            ]
        )
        result = store.get_latest_list()
        assert result[0].ingredient == "jalapeño"
        assert result[0].search_term == "jalapeño pepper"

    def test_empty_from_meals_round_trips_as_empty_list(
        self, store: ShoppingListStore
    ) -> None:
        """Test an item with no provenance round-trips from_meals as []."""
        store.save_list(
            [
                ShoppingListItem(
                    ingredient="mystery item",
                    quantity=1.0,
                    unit="each",
                    category=IngredientCategory.OTHER,
                    search_term="mystery item",
                    from_meals=[],
                )
            ]
        )
        result = store.get_latest_list()
        assert result[0].from_meals == []

    def test_fractional_quantity_round_trips_exactly(
        self, store: ShoppingListStore
    ) -> None:
        """Test a fractional quantity round-trips without precision loss."""
        store.save_list(
            [
                ShoppingListItem(
                    ingredient="vanilla extract",
                    quantity=0.25,
                    unit="tsp",
                    category=IngredientCategory.PANTRY_DRY,
                    search_term="vanilla extract",
                    from_meals=["Cookies"],
                )
            ]
        )
        result = store.get_latest_list()
        assert result[0].quantity == 0.25


# ---------------------------------------------------------------------------
# get_list (historical lookup by id)
# ---------------------------------------------------------------------------


class TestGetList:
    """Tests for get_list(list_id)."""

    def test_get_list_unknown_id_returns_empty(self, store: ShoppingListStore) -> None:
        """Test get_list with an unknown id returns []."""
        assert store.get_list(999999) == []

    def test_get_list_returns_correct_historical_list(
        self, store: ShoppingListStore
    ) -> None:
        """Test get_list retrieves an older list, not the latest one."""
        first_id = store.save_list(_make_items(2, prefix="first"))
        store.save_list(_make_items(3, prefix="second"))

        result = store.get_list(first_id)

        assert len(result) == 2
        assert all(item.ingredient.startswith("first-") for item in result)

    def test_get_list_does_not_return_other_lists_items(
        self, store: ShoppingListStore
    ) -> None:
        """Test get_list does not leak items belonging to a different list."""
        store.save_list(_make_items(2, prefix="first"))
        second_id = store.save_list(_make_items(3, prefix="second"))

        result = store.get_list(second_id)

        assert all(item.ingredient.startswith("second-") for item in result)
        assert len(result) == 3
