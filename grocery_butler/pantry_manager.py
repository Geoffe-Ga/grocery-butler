"""Pantry manager: inventory tracking, restock queue, and NL parsing.

Manages the household inventory lifecycle (on_hand -> low -> out) and
produces the restock queue that feeds into the shopping list. Also handles
Claude-powered natural language inventory updates.

Key business rule: pantry staples are excluded from shopping lists UNLESS
the inventory says the item is 'low' or 'out'.
"""

from __future__ import annotations

import json
import logging
from datetime import UTC, datetime
from typing import Any, Protocol

from grocery_butler.db import get_connection, init_db
from grocery_butler.models import InventoryItem, InventoryStatus, InventoryUpdate
from grocery_butler.prompt_loader import load_prompt

logger = logging.getLogger(__name__)

_RESTOCK_STATUSES = (InventoryStatus.LOW, InventoryStatus.OUT)


class RecipeStoreProtocol(Protocol):
    """Protocol for RecipeStore pantry staple lookup.

    Defines the minimal interface that PantryManager needs from RecipeStore
    to check whether an ingredient is a pantry staple.
    """

    def is_pantry_staple(self, ingredient: str) -> bool:
        """Check if an ingredient is a pantry staple.

        Args:
            ingredient: The ingredient name to check.

        Returns:
            True if the ingredient is a pantry staple.
        """
        ...  # pragma: no cover


class PantryManager:
    """Manages household inventory and restock queue.

    Provides CRUD for inventory items, tracks status lifecycle
    (on_hand -> low -> out -> on_hand via restock), and integrates
    with Claude for natural language inventory updates.

    Args:
        db_path: Path to the SQLite database file.
        anthropic_client: Optional Anthropic client for NL parsing.
    """

    def __init__(self, db_path: str, anthropic_client: Any = None) -> None:
        """Initialize the pantry manager.

        Args:
            db_path: Path to the SQLite database file.
            anthropic_client: Optional Anthropic client for NL parsing.
                If None, parse_inventory_intent returns empty list.
        """
        self._db_path = db_path
        self._client = anthropic_client
        init_db(db_path)

    def get_inventory(self) -> list[InventoryItem]:
        """Return all tracked inventory items.

        Returns:
            List of all InventoryItem records from the household_inventory table.
        """
        conn = get_connection(self._db_path)
        try:
            cursor = conn.execute(
                "SELECT ingredient, display_name, category, status, "
                "default_quantity, default_unit, default_search_term, notes "
                "FROM household_inventory ORDER BY ingredient"
            )
            return [_row_to_item(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def get_item(self, ingredient: str) -> InventoryItem | None:
        """Look up a single inventory item by normalized ingredient name.

        Args:
            ingredient: Ingredient name (matched case-insensitively).

        Returns:
            The matching InventoryItem, or None if not found.
        """
        conn = get_connection(self._db_path)
        try:
            cursor = conn.execute(
                "SELECT ingredient, display_name, category, status, "
                "default_quantity, default_unit, default_search_term, notes "
                "FROM household_inventory WHERE LOWER(ingredient) = ?",
                (ingredient.lower(),),
            )
            row = cursor.fetchone()
            return _row_to_item(row) if row else None
        finally:
            conn.close()

    def add_item(self, item: InventoryItem) -> int:
        """Add a new tracked inventory item.

        Args:
            item: The InventoryItem to add.

        Returns:
            The database row id of the inserted item.

        Raises:
            RuntimeError: If the insert fails (e.g. duplicate ingredient).
        """
        conn = get_connection(self._db_path)
        try:
            cursor = conn.execute(
                "INSERT INTO household_inventory "
                "(ingredient, display_name, category, status, "
                "default_quantity, default_unit, default_search_term, notes) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.ingredient.lower(),
                    item.display_name,
                    item.category.value if item.category else None,
                    item.status.value,
                    item.default_quantity,
                    item.default_unit,
                    item.default_search_term,
                    item.notes,
                ),
            )
            conn.commit()
            row_id = cursor.lastrowid
            if row_id is None:  # pragma: no cover
                raise RuntimeError("Insert did not return a row id")
            return row_id
        finally:
            conn.close()

    def remove_item(self, ingredient: str) -> None:
        """Remove a tracked inventory item.

        Args:
            ingredient: Ingredient name to remove (case-insensitive).
        """
        conn = get_connection(self._db_path)
        try:
            conn.execute(
                "DELETE FROM household_inventory WHERE LOWER(ingredient) = ?",
                (ingredient.lower(),),
            )
            conn.commit()
        finally:
            conn.close()

    def update_status(self, ingredient: str, new_status: InventoryStatus) -> None:
        """Update an item's status and record the status change timestamp.

        Args:
            ingredient: Ingredient name (case-insensitive).
            new_status: The new InventoryStatus value.
        """
        now = datetime.now(tz=UTC).isoformat()
        conn = get_connection(self._db_path)
        try:
            conn.execute(
                "UPDATE household_inventory "
                "SET status = ?, last_status_change = ? "
                "WHERE LOWER(ingredient) = ?",
                (new_status.value, now, ingredient.lower()),
            )
            conn.commit()
        finally:
            conn.close()

    def mark_restocked(self, ingredients: list[str]) -> int:
        """Move matching items back to on_hand and set last_restocked.

        Uses case-insensitive matching against ingredient names.

        Args:
            ingredients: List of ingredient names to mark as restocked.

        Returns:
            Number of items actually updated.
        """
        if not ingredients:
            return 0

        now = datetime.now(tz=UTC).isoformat()
        lowered = [ing.lower() for ing in ingredients]
        conn = get_connection(self._db_path)
        try:
            total_updated = 0
            for name in lowered:
                cursor = conn.execute(
                    "UPDATE household_inventory "
                    "SET status = ?, last_restocked = ?, last_status_change = ? "
                    "WHERE LOWER(ingredient) = ?",
                    (InventoryStatus.ON_HAND.value, now, now, name),
                )
                total_updated += cursor.rowcount
            conn.commit()
            return total_updated
        finally:
            conn.close()

    def get_restock_queue(self) -> list[InventoryItem]:
        """Return all items with status 'low' or 'out'.

        Returns:
            List of InventoryItem records needing restocking.
        """
        conn = get_connection(self._db_path)
        try:
            cursor = conn.execute(
                "SELECT ingredient, display_name, category, status, "
                "default_quantity, default_unit, default_search_term, notes "
                "FROM household_inventory WHERE status IN (?, ?) "
                "ORDER BY ingredient",
                (InventoryStatus.LOW.value, InventoryStatus.OUT.value),
            )
            return [_row_to_item(row) for row in cursor.fetchall()]
        finally:
            conn.close()

    def clear_restock_queue(self) -> int:
        """Set all low/out items to on_hand.

        Returns:
            Number of items updated.
        """
        now = datetime.now(tz=UTC).isoformat()
        conn = get_connection(self._db_path)
        try:
            cursor = conn.execute(
                "UPDATE household_inventory "
                "SET status = ?, last_status_change = ? "
                "WHERE status IN (?, ?)",
                (
                    InventoryStatus.ON_HAND.value,
                    now,
                    InventoryStatus.LOW.value,
                    InventoryStatus.OUT.value,
                ),
            )
            conn.commit()
            return cursor.rowcount
        finally:
            conn.close()

    def should_include_in_order(
        self,
        ingredient: str,
        recipe_store: RecipeStoreProtocol,
    ) -> bool:
        """Determine whether an ingredient should be included in a shopping order.

        Implements the pantry-inventory override rule:
        1. If ingredient is NOT a pantry staple -> True (always include)
        2. If IS a pantry staple AND inventory says 'low' or 'out' -> True
        3. If IS a pantry staple AND inventory says 'on_hand' -> False
        4. If IS a pantry staple AND NOT tracked in inventory -> False

        Args:
            ingredient: The ingredient name to check.
            recipe_store: RecipeStore instance for pantry staple lookup.

        Returns:
            True if the ingredient should be included in the order.
        """
        if not recipe_store.is_pantry_staple(ingredient):
            return True

        item = self.get_item(ingredient)
        if item is None:
            return False

        return item.status in _RESTOCK_STATUSES

    def parse_inventory_intent(self, message: str) -> list[InventoryUpdate]:
        """Parse natural language into inventory status updates via Claude.

        Args:
            message: The user's natural language message.

        Returns:
            List of InventoryUpdate objects with confidence >= 0.8.
            Returns empty list if no client is configured or on API errors.
        """
        if self._client is None:
            return []

        inventory_context = _format_inventory_context(self.get_inventory())
        prompt = load_prompt(
            "inventory_intent",
            user_message=message,
            current_inventory=inventory_context,
        )

        return _call_claude_with_retry(self._client, prompt)


def _row_to_item(row: Any) -> InventoryItem:
    """Convert a sqlite3.Row to an InventoryItem model.

    Args:
        row: A sqlite3.Row from the household_inventory table.

    Returns:
        Populated InventoryItem instance.
    """
    return InventoryItem(
        ingredient=row["ingredient"],
        display_name=row["display_name"],
        category=row["category"],
        status=row["status"],
        default_quantity=row["default_quantity"],
        default_unit=row["default_unit"],
        default_search_term=row["default_search_term"],
        notes=row["notes"] or "",
    )


def _format_inventory_context(items: list[InventoryItem]) -> str:
    """Format current inventory as a string for Claude prompt context.

    Args:
        items: List of inventory items.

    Returns:
        Human-readable inventory summary string.
    """
    if not items:
        return "No items currently tracked."

    lines = []
    for item in items:
        lines.append(f"- {item.display_name}: {item.status.value}")
    return "\n".join(lines)


def _parse_claude_response(text: str) -> list[InventoryUpdate]:
    """Parse Claude's JSON response into InventoryUpdate objects.

    Filters out low-confidence matches (< 0.8).

    Args:
        text: Raw text response from Claude API.

    Returns:
        List of validated InventoryUpdate objects.

    Raises:
        json.JSONDecodeError: If the response is not valid JSON.
        ValueError: If the JSON structure is invalid.
    """
    data = json.loads(text)
    if not isinstance(data, list):
        raise ValueError("Expected a JSON array")

    updates = []
    for entry in data:
        update = InventoryUpdate(**entry)
        if update.confidence >= 0.8:
            updates.append(update)
    return updates


def _call_claude_with_retry(
    client: Any, prompt: str, max_retries: int = 1
) -> list[InventoryUpdate]:
    """Call Claude API and parse response, retrying once on invalid JSON.

    Args:
        client: Anthropic API client.
        prompt: The formatted prompt string.
        max_retries: Number of retries on parse failure (default 1).

    Returns:
        List of parsed InventoryUpdate objects.
        Returns empty list on API errors.
    """
    attempts = 0
    while attempts <= max_retries:
        try:
            response = client.messages.create(
                model="claude-sonnet-4-20250514",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text
            return _parse_claude_response(text)
        except (json.JSONDecodeError, ValueError):
            attempts += 1
            if attempts > max_retries:
                logger.warning("Failed to parse Claude response after retries")
                return []
        except Exception:
            logger.exception("Claude API call failed")
            return []
    return []  # pragma: no cover
