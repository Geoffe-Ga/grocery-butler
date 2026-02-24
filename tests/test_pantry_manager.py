"""Tests for grocery_butler.pantry_manager module."""

from __future__ import annotations

import json
from typing import TYPE_CHECKING
from unittest.mock import MagicMock

import pytest

if TYPE_CHECKING:
    from pathlib import Path

from grocery_butler.models import (
    IngredientCategory,
    InventoryItem,
    InventoryStatus,
)
from grocery_butler.pantry_manager import (
    PantryManager,
    _format_inventory_context,
    _parse_claude_response,
)

# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a temporary database path for test isolation."""
    return str(tmp_path / "test_pantry.db")


@pytest.fixture()
def manager(db_path: str) -> PantryManager:
    """Return a PantryManager with no anthropic client."""
    return PantryManager(db_path)


@pytest.fixture()
def sample_item() -> InventoryItem:
    """Return a sample InventoryItem for testing."""
    return InventoryItem(
        ingredient="milk",
        display_name="Milk",
        category=IngredientCategory.DAIRY,
        status=InventoryStatus.ON_HAND,
        default_quantity=1.0,
        default_unit="gallon",
        default_search_term="whole milk",
        notes="2% preferred",
    )


@pytest.fixture()
def recipe_store_mock() -> MagicMock:
    """Return a mock RecipeStore with is_pantry_staple method."""
    mock = MagicMock()
    mock.is_pantry_staple = MagicMock(return_value=False)
    return mock


# ---------------------------------------------------------------------------
# TestPantryManagerInit
# ---------------------------------------------------------------------------


class TestPantryManagerInit:
    """Tests for PantryManager constructor."""

    def test_init_creates_db(self, db_path: str) -> None:
        """Test constructor initializes the database."""
        pm = PantryManager(db_path)
        # Should be able to query without error
        items = pm.get_inventory()
        assert items == []

    def test_init_with_anthropic_client(self, db_path: str) -> None:
        """Test constructor accepts optional anthropic client."""
        mock_client = MagicMock()
        pm = PantryManager(db_path, anthropic_client=mock_client)
        assert pm._client is mock_client

    def test_init_without_client(self, db_path: str) -> None:
        """Test constructor defaults to None client."""
        pm = PantryManager(db_path)
        assert pm._client is None


# ---------------------------------------------------------------------------
# TestInventoryCRUD
# ---------------------------------------------------------------------------


class TestInventoryCRUD:
    """Tests for inventory CRUD operations."""

    def test_add_item_returns_id(
        self, manager: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test add_item returns a positive integer id."""
        row_id = manager.add_item(sample_item)
        assert isinstance(row_id, int)
        assert row_id > 0

    def test_get_inventory_empty(self, manager: PantryManager) -> None:
        """Test get_inventory returns empty list for empty db."""
        assert manager.get_inventory() == []

    def test_get_inventory_returns_added_items(
        self, manager: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test get_inventory returns items that were added."""
        manager.add_item(sample_item)
        items = manager.get_inventory()
        assert len(items) == 1
        assert items[0].ingredient == "milk"
        assert items[0].display_name == "Milk"
        assert items[0].category == IngredientCategory.DAIRY

    def test_get_item_found(
        self, manager: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test get_item returns item when found."""
        manager.add_item(sample_item)
        item = manager.get_item("milk")
        assert item is not None
        assert item.ingredient == "milk"
        assert item.status == InventoryStatus.ON_HAND

    def test_get_item_not_found(self, manager: PantryManager) -> None:
        """Test get_item returns None when ingredient not tracked."""
        item = manager.get_item("nonexistent")
        assert item is None

    def test_get_item_case_insensitive(
        self, manager: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test get_item matches case-insensitively."""
        manager.add_item(sample_item)
        item = manager.get_item("MILK")
        assert item is not None
        assert item.ingredient == "milk"

    def test_remove_item(
        self, manager: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test remove_item deletes the item."""
        manager.add_item(sample_item)
        manager.remove_item("milk")
        assert manager.get_item("milk") is None

    def test_remove_item_case_insensitive(
        self, manager: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test remove_item uses case-insensitive matching."""
        manager.add_item(sample_item)
        manager.remove_item("MILK")
        assert manager.get_item("milk") is None

    def test_remove_nonexistent_item_no_error(self, manager: PantryManager) -> None:
        """Test remove_item does not raise for missing items."""
        manager.remove_item("nonexistent")  # Should not raise

    def test_add_item_with_no_category(self, manager: PantryManager) -> None:
        """Test add_item handles None category."""
        item = InventoryItem(
            ingredient="mystery spice",
            display_name="Mystery Spice",
            category=None,
            status=InventoryStatus.ON_HAND,
        )
        row_id = manager.add_item(item)
        assert row_id > 0
        retrieved = manager.get_item("mystery spice")
        assert retrieved is not None
        assert retrieved.category is None

    def test_add_multiple_items(self, manager: PantryManager) -> None:
        """Test adding multiple items and retrieving them."""
        items = [
            InventoryItem(
                ingredient="eggs",
                display_name="Eggs",
                category=IngredientCategory.DAIRY,
            ),
            InventoryItem(
                ingredient="bread",
                display_name="Bread",
                category=IngredientCategory.BAKERY,
            ),
            InventoryItem(
                ingredient="apples",
                display_name="Apples",
                category=IngredientCategory.PRODUCE,
            ),
        ]
        for item in items:
            manager.add_item(item)

        inventory = manager.get_inventory()
        assert len(inventory) == 3
        names = [i.ingredient for i in inventory]
        assert "apples" in names
        assert "bread" in names
        assert "eggs" in names


# ---------------------------------------------------------------------------
# TestStatusLifecycle
# ---------------------------------------------------------------------------


class TestStatusLifecycle:
    """Tests for status transitions: on_hand -> low -> out -> on_hand."""

    def test_initial_status_on_hand(
        self, manager: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test items start with on_hand status."""
        manager.add_item(sample_item)
        item = manager.get_item("milk")
        assert item is not None
        assert item.status == InventoryStatus.ON_HAND

    def test_update_status_to_low(
        self, manager: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test updating status from on_hand to low."""
        manager.add_item(sample_item)
        manager.update_status("milk", InventoryStatus.LOW)
        item = manager.get_item("milk")
        assert item is not None
        assert item.status == InventoryStatus.LOW

    def test_update_status_to_out(
        self, manager: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test updating status from low to out."""
        manager.add_item(sample_item)
        manager.update_status("milk", InventoryStatus.LOW)
        manager.update_status("milk", InventoryStatus.OUT)
        item = manager.get_item("milk")
        assert item is not None
        assert item.status == InventoryStatus.OUT

    def test_full_lifecycle_on_hand_low_out_restocked(
        self, manager: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test full status lifecycle: on_hand -> low -> out -> on_hand."""
        manager.add_item(sample_item)

        # on_hand -> low
        manager.update_status("milk", InventoryStatus.LOW)
        assert manager.get_item("milk").status == InventoryStatus.LOW  # type: ignore[union-attr]

        # low -> out
        manager.update_status("milk", InventoryStatus.OUT)
        assert manager.get_item("milk").status == InventoryStatus.OUT  # type: ignore[union-attr]

        # out -> on_hand (via mark_restocked)
        count = manager.mark_restocked(["milk"])
        assert count == 1
        assert manager.get_item("milk").status == InventoryStatus.ON_HAND  # type: ignore[union-attr]

    def test_update_status_case_insensitive(
        self, manager: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test update_status uses case-insensitive matching."""
        manager.add_item(sample_item)
        manager.update_status("MILK", InventoryStatus.LOW)
        item = manager.get_item("milk")
        assert item is not None
        assert item.status == InventoryStatus.LOW


# ---------------------------------------------------------------------------
# TestUpdateQuantity
# ---------------------------------------------------------------------------


class TestUpdateQuantity:
    """Tests for update_quantity method."""

    def test_update_quantity_basic(
        self, manager: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test updating an item's current quantity."""
        manager.add_item(sample_item)
        manager.update_quantity("milk", 0.5, "gal")

        item = manager.get_item("milk")
        assert item is not None
        assert item.current_quantity == 0.5
        assert item.current_unit == "gal"

    def test_update_quantity_case_insensitive(
        self, manager: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test update_quantity matches case-insensitively."""
        manager.add_item(sample_item)
        manager.update_quantity("MILK", 2.0, "L")

        item = manager.get_item("milk")
        assert item is not None
        assert item.current_quantity == 2.0
        assert item.current_unit == "L"

    def test_update_quantity_overwrites(
        self, manager: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test updating quantity overwrites previous value."""
        manager.add_item(sample_item)
        manager.update_quantity("milk", 1.0, "gal")
        manager.update_quantity("milk", 0.25, "gal")

        item = manager.get_item("milk")
        assert item is not None
        assert item.current_quantity == 0.25

    def test_item_added_without_quantity(
        self, manager: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test new items have None quantity by default."""
        manager.add_item(sample_item)
        item = manager.get_item("milk")
        assert item is not None
        assert item.current_quantity is None
        assert item.current_unit is None


# ---------------------------------------------------------------------------
# TestMarkRestocked
# ---------------------------------------------------------------------------


class TestMarkRestocked:
    """Tests for mark_restocked method."""

    def test_mark_restocked_single(
        self, manager: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test restocking a single item."""
        manager.add_item(sample_item)
        manager.update_status("milk", InventoryStatus.OUT)

        count = manager.mark_restocked(["milk"])
        assert count == 1
        item = manager.get_item("milk")
        assert item is not None
        assert item.status == InventoryStatus.ON_HAND

    def test_mark_restocked_multiple(self, manager: PantryManager) -> None:
        """Test restocking multiple items at once."""
        items = [
            InventoryItem(ingredient="milk", display_name="Milk"),
            InventoryItem(ingredient="eggs", display_name="Eggs"),
            InventoryItem(ingredient="bread", display_name="Bread"),
        ]
        for item in items:
            manager.add_item(item)
            manager.update_status(item.ingredient, InventoryStatus.OUT)

        count = manager.mark_restocked(["milk", "eggs", "bread"])
        assert count == 3

    def test_mark_restocked_case_insensitive(
        self, manager: PantryManager, sample_item: InventoryItem
    ) -> None:
        """Test mark_restocked matches case-insensitively."""
        manager.add_item(sample_item)
        manager.update_status("milk", InventoryStatus.LOW)

        count = manager.mark_restocked(["MILK"])
        assert count == 1
        item = manager.get_item("milk")
        assert item is not None
        assert item.status == InventoryStatus.ON_HAND

    def test_mark_restocked_mixed_case(self, manager: PantryManager) -> None:
        """Test mark_restocked with Olive Oil style casing."""
        item = InventoryItem(
            ingredient="olive oil",
            display_name="Olive Oil",
            category=IngredientCategory.PANTRY_DRY,
        )
        manager.add_item(item)
        manager.update_status("olive oil", InventoryStatus.OUT)

        count = manager.mark_restocked(["Olive Oil"])
        assert count == 1

    def test_mark_restocked_empty_list(self, manager: PantryManager) -> None:
        """Test mark_restocked with empty list returns 0."""
        count = manager.mark_restocked([])
        assert count == 0

    def test_mark_restocked_nonexistent(self, manager: PantryManager) -> None:
        """Test mark_restocked with unknown ingredients returns 0."""
        count = manager.mark_restocked(["nonexistent"])
        assert count == 0

    def test_mark_restocked_partial_match(self, manager: PantryManager) -> None:
        """Test mark_restocked when only some items exist."""
        item = InventoryItem(ingredient="milk", display_name="Milk")
        manager.add_item(item)
        manager.update_status("milk", InventoryStatus.OUT)

        count = manager.mark_restocked(["milk", "nonexistent"])
        assert count == 1

    def test_mark_restocked_resets_quantity(
        self,
        manager: PantryManager,
    ) -> None:
        """Test mark_restocked resets current_quantity to default_quantity."""
        item = InventoryItem(
            ingredient="milk",
            display_name="Milk",
            default_quantity=1.0,
            default_unit="gal",
            current_quantity=0.1,
            current_unit="gal",
        )
        manager.add_item(item)
        manager.update_status("milk", InventoryStatus.OUT)

        manager.mark_restocked(["milk"])
        updated = manager.get_item("milk")
        assert updated is not None
        assert updated.current_quantity == 1.0


# ---------------------------------------------------------------------------
# TestRestockQueue
# ---------------------------------------------------------------------------


class TestRestockQueue:
    """Tests for restock queue operations."""

    def test_get_restock_queue_empty(self, manager: PantryManager) -> None:
        """Test restock queue is empty when no items need restocking."""
        item = InventoryItem(
            ingredient="milk",
            display_name="Milk",
            status=InventoryStatus.ON_HAND,
        )
        manager.add_item(item)
        assert manager.get_restock_queue() == []

    def test_get_restock_queue_low_items(self, manager: PantryManager) -> None:
        """Test restock queue includes items with 'low' status."""
        item = InventoryItem(ingredient="milk", display_name="Milk")
        manager.add_item(item)
        manager.update_status("milk", InventoryStatus.LOW)

        queue = manager.get_restock_queue()
        assert len(queue) == 1
        assert queue[0].ingredient == "milk"
        assert queue[0].status == InventoryStatus.LOW

    def test_get_restock_queue_out_items(self, manager: PantryManager) -> None:
        """Test restock queue includes items with 'out' status."""
        item = InventoryItem(ingredient="eggs", display_name="Eggs")
        manager.add_item(item)
        manager.update_status("eggs", InventoryStatus.OUT)

        queue = manager.get_restock_queue()
        assert len(queue) == 1
        assert queue[0].status == InventoryStatus.OUT

    def test_get_restock_queue_excludes_on_hand(self, manager: PantryManager) -> None:
        """Test restock queue excludes on_hand items."""
        items_data = [
            ("milk", InventoryStatus.LOW),
            ("eggs", InventoryStatus.ON_HAND),
            ("bread", InventoryStatus.OUT),
        ]
        for ingredient, status in items_data:
            item = InventoryItem(
                ingredient=ingredient,
                display_name=ingredient.title(),
            )
            manager.add_item(item)
            if status != InventoryStatus.ON_HAND:
                manager.update_status(ingredient, status)

        queue = manager.get_restock_queue()
        assert len(queue) == 2
        names = [i.ingredient for i in queue]
        assert "milk" in names
        assert "bread" in names
        assert "eggs" not in names

    def test_clear_restock_queue(self, manager: PantryManager) -> None:
        """Test clear_restock_queue resets all low/out items to on_hand."""
        items = [
            InventoryItem(ingredient="milk", display_name="Milk"),
            InventoryItem(ingredient="eggs", display_name="Eggs"),
            InventoryItem(ingredient="bread", display_name="Bread"),
        ]
        for item in items:
            manager.add_item(item)

        manager.update_status("milk", InventoryStatus.LOW)
        manager.update_status("eggs", InventoryStatus.OUT)
        # bread stays on_hand

        count = manager.clear_restock_queue()
        assert count == 2

        # All items should now be on_hand
        for item in items:
            retrieved = manager.get_item(item.ingredient)
            assert retrieved is not None
            assert retrieved.status == InventoryStatus.ON_HAND

    def test_clear_restock_queue_empty(self, manager: PantryManager) -> None:
        """Test clear_restock_queue returns 0 when nothing to clear."""
        count = manager.clear_restock_queue()
        assert count == 0

    def test_clear_restock_queue_all_on_hand(self, manager: PantryManager) -> None:
        """Test clear_restock_queue returns 0 when all items are on_hand."""
        item = InventoryItem(
            ingredient="milk",
            display_name="Milk",
            status=InventoryStatus.ON_HAND,
        )
        manager.add_item(item)
        count = manager.clear_restock_queue()
        assert count == 0


# ---------------------------------------------------------------------------
# TestShouldIncludeInOrder
# ---------------------------------------------------------------------------


class TestShouldIncludeInOrder:
    """Tests for the pantry-inventory override rule."""

    def test_non_pantry_staple_always_included(
        self,
        manager: PantryManager,
        recipe_store_mock: MagicMock,
    ) -> None:
        """Test case 1: non-pantry items are always included."""
        recipe_store_mock.is_pantry_staple.return_value = False
        result = manager.should_include_in_order("chicken", recipe_store_mock)
        assert result is True

    def test_pantry_staple_low_included(
        self,
        manager: PantryManager,
        recipe_store_mock: MagicMock,
    ) -> None:
        """Test case 2a: pantry staple with 'low' status is included."""
        recipe_store_mock.is_pantry_staple.return_value = True
        item = InventoryItem(ingredient="salt", display_name="Salt")
        manager.add_item(item)
        manager.update_status("salt", InventoryStatus.LOW)

        result = manager.should_include_in_order("salt", recipe_store_mock)
        assert result is True

    def test_pantry_staple_out_included(
        self,
        manager: PantryManager,
        recipe_store_mock: MagicMock,
    ) -> None:
        """Test case 2b: pantry staple with 'out' status is included."""
        recipe_store_mock.is_pantry_staple.return_value = True
        item = InventoryItem(ingredient="salt", display_name="Salt")
        manager.add_item(item)
        manager.update_status("salt", InventoryStatus.OUT)

        result = manager.should_include_in_order("salt", recipe_store_mock)
        assert result is True

    def test_pantry_staple_on_hand_excluded(
        self,
        manager: PantryManager,
        recipe_store_mock: MagicMock,
    ) -> None:
        """Test case 3: pantry staple with 'on_hand' status is excluded."""
        recipe_store_mock.is_pantry_staple.return_value = True
        item = InventoryItem(ingredient="salt", display_name="Salt")
        manager.add_item(item)

        result = manager.should_include_in_order("salt", recipe_store_mock)
        assert result is False

    def test_pantry_staple_not_tracked_excluded(
        self,
        manager: PantryManager,
        recipe_store_mock: MagicMock,
    ) -> None:
        """Test case 4: pantry staple not in inventory assumed on_hand."""
        recipe_store_mock.is_pantry_staple.return_value = True
        # Do not add salt to inventory
        result = manager.should_include_in_order("salt", recipe_store_mock)
        assert result is False


# ---------------------------------------------------------------------------
# TestParseInventoryIntent
# ---------------------------------------------------------------------------


class TestParseInventoryIntent:
    """Tests for NL inventory parsing via Claude."""

    def test_returns_empty_without_client(self, manager: PantryManager) -> None:
        """Test parse_inventory_intent returns empty when no client."""
        result = manager.parse_inventory_intent("we're out of milk")
        assert result == []

    def test_valid_json_response(self, db_path: str) -> None:
        """Test parse with mocked Claude returning valid JSON."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_content = MagicMock()
        mock_content.text = json.dumps(
            [
                {
                    "ingredient": "milk",
                    "new_status": "out",
                    "confidence": 0.95,
                },
                {
                    "ingredient": "eggs",
                    "new_status": "low",
                    "confidence": 0.90,
                },
            ]
        )
        mock_response.content = [mock_content]
        mock_client.messages.create.return_value = mock_response

        pm = PantryManager(db_path, anthropic_client=mock_client)
        result = pm.parse_inventory_intent("we're out of milk and low on eggs")

        assert len(result) == 2
        assert result[0].ingredient == "milk"
        assert result[0].new_status == InventoryStatus.OUT
        assert result[1].ingredient == "eggs"
        assert result[1].new_status == InventoryStatus.LOW

    def test_filters_low_confidence(self, db_path: str) -> None:
        """Test parse filters out results with confidence < 0.8."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_content = MagicMock()
        mock_content.text = json.dumps(
            [
                {
                    "ingredient": "milk",
                    "new_status": "out",
                    "confidence": 0.95,
                },
                {
                    "ingredient": "maybe_flour",
                    "new_status": "low",
                    "confidence": 0.5,
                },
            ]
        )
        mock_response.content = [mock_content]
        mock_client.messages.create.return_value = mock_response

        pm = PantryManager(db_path, anthropic_client=mock_client)
        result = pm.parse_inventory_intent("we're out of milk, maybe flour?")

        assert len(result) == 1
        assert result[0].ingredient == "milk"

    def test_retry_on_invalid_json_then_success(self, db_path: str) -> None:
        """Test parse retries once on invalid JSON then succeeds."""
        mock_client = MagicMock()

        # First call returns invalid JSON, second returns valid
        bad_response = MagicMock()
        bad_content = MagicMock()
        bad_content.text = "not valid json at all"
        bad_response.content = [bad_content]

        good_response = MagicMock()
        good_content = MagicMock()
        good_content.text = json.dumps(
            [
                {
                    "ingredient": "milk",
                    "new_status": "out",
                    "confidence": 0.95,
                }
            ]
        )
        good_response.content = [good_content]

        mock_client.messages.create.side_effect = [bad_response, good_response]

        pm = PantryManager(db_path, anthropic_client=mock_client)
        result = pm.parse_inventory_intent("we're out of milk")

        assert len(result) == 1
        assert result[0].ingredient == "milk"
        assert mock_client.messages.create.call_count == 2

    def test_retry_on_invalid_json_both_fail(self, db_path: str) -> None:
        """Test parse returns empty after both attempts fail."""
        mock_client = MagicMock()
        bad_response = MagicMock()
        bad_content = MagicMock()
        bad_content.text = "not valid json"
        bad_response.content = [bad_content]

        mock_client.messages.create.return_value = bad_response

        pm = PantryManager(db_path, anthropic_client=mock_client)
        result = pm.parse_inventory_intent("we're out of milk")

        assert result == []
        assert mock_client.messages.create.call_count == 2

    def test_api_error_returns_empty(self, db_path: str) -> None:
        """Test parse returns empty list on API exception."""
        mock_client = MagicMock()
        mock_client.messages.create.side_effect = RuntimeError("API down")

        pm = PantryManager(db_path, anthropic_client=mock_client)
        result = pm.parse_inventory_intent("we're out of milk")

        assert result == []

    def test_empty_message_with_client(self, db_path: str) -> None:
        """Test parse with empty array response."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_content = MagicMock()
        mock_content.text = "[]"
        mock_response.content = [mock_content]
        mock_client.messages.create.return_value = mock_response

        pm = PantryManager(db_path, anthropic_client=mock_client)
        result = pm.parse_inventory_intent("hello how are you")

        assert result == []

    def test_passes_inventory_context(self, db_path: str) -> None:
        """Test that current inventory is passed as context to Claude."""
        mock_client = MagicMock()
        mock_response = MagicMock()
        mock_content = MagicMock()
        mock_content.text = "[]"
        mock_response.content = [mock_content]
        mock_client.messages.create.return_value = mock_response

        pm = PantryManager(db_path, anthropic_client=mock_client)

        # Add an item to inventory so context is non-empty
        item = InventoryItem(ingredient="milk", display_name="Milk")
        pm.add_item(item)

        pm.parse_inventory_intent("check milk status")

        # Verify the prompt was called with inventory context
        call_args = mock_client.messages.create.call_args
        messages = call_args.kwargs["messages"]
        prompt_text = messages[0]["content"]
        assert "Milk" in prompt_text


# ---------------------------------------------------------------------------
# TestFormatInventoryContext
# ---------------------------------------------------------------------------


class TestFormatInventoryContext:
    """Tests for _format_inventory_context helper."""

    def test_empty_inventory(self) -> None:
        """Test formatting with no items."""
        result = _format_inventory_context([])
        assert result == "No items currently tracked."

    def test_single_item(self) -> None:
        """Test formatting with one item."""
        items = [
            InventoryItem(
                ingredient="milk",
                display_name="Milk",
                status=InventoryStatus.ON_HAND,
            )
        ]
        result = _format_inventory_context(items)
        assert result == "- Milk: on_hand"

    def test_multiple_items(self) -> None:
        """Test formatting with multiple items."""
        items = [
            InventoryItem(
                ingredient="milk",
                display_name="Milk",
                status=InventoryStatus.ON_HAND,
            ),
            InventoryItem(
                ingredient="eggs",
                display_name="Eggs",
                status=InventoryStatus.LOW,
            ),
        ]
        result = _format_inventory_context(items)
        assert "- Milk: on_hand" in result
        assert "- Eggs: low" in result


# ---------------------------------------------------------------------------
# TestParseClaudeResponse
# ---------------------------------------------------------------------------


class TestParseClaudeResponse:
    """Tests for _parse_claude_response helper."""

    def test_valid_array(self) -> None:
        """Test parsing a valid JSON array."""
        text = json.dumps(
            [{"ingredient": "milk", "new_status": "out", "confidence": 0.95}]
        )
        result = _parse_claude_response(text)
        assert len(result) == 1
        assert result[0].ingredient == "milk"

    def test_filters_low_confidence(self) -> None:
        """Test low-confidence entries are filtered out."""
        text = json.dumps(
            [
                {"ingredient": "milk", "new_status": "out", "confidence": 0.95},
                {"ingredient": "flour", "new_status": "low", "confidence": 0.3},
            ]
        )
        result = _parse_claude_response(text)
        assert len(result) == 1

    def test_boundary_confidence_included(self) -> None:
        """Test that confidence exactly 0.8 is included."""
        text = json.dumps(
            [{"ingredient": "milk", "new_status": "out", "confidence": 0.8}]
        )
        result = _parse_claude_response(text)
        assert len(result) == 1

    def test_boundary_confidence_excluded(self) -> None:
        """Test that confidence just below 0.8 is excluded."""
        text = json.dumps(
            [{"ingredient": "milk", "new_status": "out", "confidence": 0.79}]
        )
        result = _parse_claude_response(text)
        assert len(result) == 0

    def test_invalid_json_raises(self) -> None:
        """Test invalid JSON raises JSONDecodeError."""
        with pytest.raises(json.JSONDecodeError):
            _parse_claude_response("not json")

    def test_non_array_raises(self) -> None:
        """Test non-array JSON raises ValueError."""
        with pytest.raises(ValueError, match="Expected a JSON array"):
            _parse_claude_response('{"key": "value"}')

    def test_empty_array(self) -> None:
        """Test empty array returns empty list."""
        result = _parse_claude_response("[]")
        assert result == []
