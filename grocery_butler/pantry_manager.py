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

from grocery_butler.claude_utils import (
    create_message,
    extract_json_text,
    extraction_failed,
)
from grocery_butler.db import get_connection
from grocery_butler.household_items import SYSTEM_ACTOR, HouseholdItemStore
from grocery_butler.inventory_ledger import InventoryLedger
from grocery_butler.models import (
    InventoryItem,
    InventoryStatus,
    InventoryUpdate,
    NewObservation,
    ObservationSource,
    StockState,
    coerce_category_optional,
)
from grocery_butler.prompt_loader import load_prompt
from grocery_butler.replenishment import ReplenishmentNeeds

logger = logging.getLogger(__name__)

_EXTRACTION_STAGE = "inventory_update"

_RESTOCK_STATUSES = (InventoryStatus.LOW, InventoryStatus.OUT)


class MissingRowIdError(RuntimeError):
    """Raised when an INSERT returns no row id."""

    def __init__(self) -> None:
        """Use the one message this error carries."""
        super().__init__("Insert did not return a row id")


class NotAJsonArrayError(ValueError):
    """Raised when Claude's inventory reply parses as JSON but isn't an array.

    It stays a ``ValueError``, because callers treat any malformed reply alike.
    """

    def __init__(self) -> None:
        """Use the one message this error carries."""
        super().__init__("Expected a JSON array")


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

    def get_inventory(self) -> list[InventoryItem]:
        """Return all tracked inventory items.

        Returns:
            List of all InventoryItem records from the household_inventory table.
        """
        conn = get_connection(self._db_path)
        try:
            cursor = conn.execute(
                "SELECT ingredient, display_name, category, status, "
                "current_quantity, current_unit, "
                "default_quantity, default_unit, default_search_term, notes, item_id "
                "FROM household_inventory ORDER BY ingredient"
            )
            return _rows_to_items(cursor.fetchall())
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
                "current_quantity, current_unit, "
                "default_quantity, default_unit, default_search_term, notes, item_id "
                "FROM household_inventory WHERE LOWER(ingredient) = ?",
                (ingredient.lower(),),
            )
            row = cursor.fetchone()
            items = _rows_to_items([row] if row else [])
            return items[0] if items else None
        finally:
            conn.close()

    def add_item(self, item: InventoryItem) -> int:
        """Add a new tracked inventory item.

        Args:
            item: The InventoryItem to add.

        Returns:
            The database row id of the inserted item.

        Raises:
            MissingRowIdError: If the insert returns no row id (a RuntimeError).
        """
        household_item = HouseholdItemStore(self._db_path).resolve_or_create(
            item.ingredient,
            actor=SYSTEM_ACTOR,
            display_name=item.display_name,
            category=item.category,
        )
        conn = get_connection(self._db_path)
        try:
            cursor = conn.execute(
                "INSERT INTO household_inventory "
                "(ingredient, display_name, category, status, "
                "current_quantity, current_unit, "
                "default_quantity, default_unit, default_search_term, notes, "
                "item_id) "
                "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
                (
                    item.ingredient.lower(),
                    item.display_name,
                    item.category.value if item.category else None,
                    item.status.value,
                    item.current_quantity,
                    item.current_unit,
                    item.default_quantity,
                    item.default_unit,
                    item.default_search_term,
                    item.notes,
                    household_item.id,
                ),
            )
            conn.commit()
            row_id = cursor.lastrowid
            if row_id is None:  # pragma: no cover
                raise MissingRowIdError
        finally:
            conn.close()
        self._observe(household_item.id, item.status)
        return row_id

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
        tracked = self.get_item(ingredient)
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
        if tracked is not None:
            self._observe(tracked.item_id, new_status)

    def update_quantity(self, ingredient: str, quantity: float, unit: str) -> None:
        """Update an item's current quantity and unit.

        Args:
            ingredient: Ingredient name (case-insensitive).
            quantity: The new quantity value.
            unit: The unit for the quantity (e.g. 'gal', 'lb').
        """
        tracked = self.get_item(ingredient)
        conn = get_connection(self._db_path)
        try:
            conn.execute(
                "UPDATE household_inventory "
                "SET current_quantity = ?, current_unit = ? "
                "WHERE LOWER(ingredient) = ?",
                (quantity, unit, ingredient.lower()),
            )
            conn.commit()
        finally:
            conn.close()
        if tracked is not None:
            self._observe(tracked.item_id, tracked.status, quantity, unit)

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
        tracked = [item for name in lowered if (item := self.get_item(name))]
        conn = get_connection(self._db_path)
        try:
            total_updated = 0
            for name in lowered:
                cursor = conn.execute(
                    "UPDATE household_inventory "
                    "SET status = ?, last_restocked = ?, last_status_change = ?, "
                    "current_quantity = default_quantity, "
                    "current_unit = default_unit "
                    "WHERE LOWER(ingredient) = ?",
                    (InventoryStatus.ON_HAND.value, now, now, name),
                )
                total_updated += cursor.rowcount
            conn.commit()
        finally:
            conn.close()
        for item in tracked:
            self._observe(item.item_id, InventoryStatus.ON_HAND)
        return total_updated

    def get_restock_queue(self) -> list[InventoryItem]:
        """Return all items with status 'low' or 'out'.

        Returns:
            List of InventoryItem records needing restocking.
        """
        conn = get_connection(self._db_path)
        try:
            cursor = conn.execute(
                "SELECT ingredient, display_name, category, status, "
                "current_quantity, current_unit, "
                "default_quantity, default_unit, default_search_term, notes, item_id "
                "FROM household_inventory WHERE status IN (?, ?) "
                "ORDER BY ingredient",
                (InventoryStatus.LOW.value, InventoryStatus.OUT.value),
            )
            return _rows_to_items(cursor.fetchall())
        finally:
            conn.close()

    def clear_restock_queue(self) -> int:
        """Set all low/out items to on_hand.

        Returns:
            Number of items updated.
        """
        queued = self.get_restock_queue()
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
            cleared = cursor.rowcount
        finally:
            conn.close()
        for item in queued:
            self._observe(item.item_id, InventoryStatus.ON_HAND)
        return cleared

    def _observe(
        self,
        item_id: int | None,
        status: InventoryStatus,
        quantity: float | None = None,
        unit: str | None = None,
    ) -> None:
        """Append a status change to the observation ledger (ADR 0007).

        The inventory table is still what older readers use; the ledger is
        the complete record. Runs after the inventory write has committed,
        because the ledger writes on its own connection. The item's
        replenishment need is then synced with its new state (ADR 0008).

        Args:
            item_id: The row's household item, or None for a row that was
                never linked (nothing is recorded then).
            status: The state being stated.
            quantity: A stated count, if one came with the change.
            unit: The count's unit.
        """
        if item_id is None:
            return
        InventoryLedger(self._db_path).record(
            NewObservation(
                item_id=item_id,
                state=StockState(status.value),
                source=ObservationSource.HUMAN,
                actor=SYSTEM_ACTOR,
                quantity=quantity,
                unit=unit,
            )
        )
        ReplenishmentNeeds(self._db_path).sync_item(item_id)

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


def _rows_to_items(rows: list[Any]) -> list[InventoryItem]:
    """Convert inventory rows to models, leaving out any with no name.

    A row with a blank name can only come from a database older than the
    model's own check (issue #198). It can't be shown, bought or matched,
    and one such row must not make the whole inventory unreadable.

    Args:
        rows: Rows from the household_inventory table.

    Returns:
        One InventoryItem per row that has a name.
    """
    named = [row for row in rows if str(row["ingredient"]).strip()]
    if len(named) < len(rows):
        logger.warning("Skipped %d inventory rows with no name", len(rows) - len(named))
    return [_row_to_item(row) for row in named]


def _row_to_item(row: Any) -> InventoryItem:
    """Convert a database row to an InventoryItem model.

    Args:
        row: A dict-like row from the household_inventory table.

    Returns:
        Populated InventoryItem instance.
    """
    return InventoryItem(
        ingredient=row["ingredient"],
        display_name=row["display_name"],
        category=coerce_category_optional(row["category"]),
        status=row["status"],
        current_quantity=row["current_quantity"],
        current_unit=row["current_unit"],
        default_quantity=row["default_quantity"],
        default_unit=row["default_unit"],
        default_search_term=row["default_search_term"],
        notes=row["notes"] or "",
        item_id=row["item_id"],
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
        qty_str = ""
        if item.current_quantity is not None and item.current_unit is not None:
            qty_str = f" ({item.current_quantity:g} {item.current_unit})"
        lines.append(f"- {item.display_name}: {item.status.value}{qty_str}")
    return "\n".join(lines)


def _parse_claude_response(text: str) -> list[InventoryUpdate]:
    """Parse Claude's JSON response into InventoryUpdate objects.

    Strips markdown code fences (e.g. ```json ... ```) before parsing,
    since Claude sometimes wraps its JSON output that way. Filters out
    low-confidence matches (< 0.8).

    Args:
        text: Raw text response from Claude API.

    Returns:
        List of validated InventoryUpdate objects.

    Raises:
        json.JSONDecodeError: If the response is not valid JSON.
        NotAJsonArrayError: If the JSON is not an array (a ValueError).
    """
    data = json.loads(extract_json_text(text))
    if not isinstance(data, list):
        raise NotAJsonArrayError

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
    # Two layers, on purpose. This loop asks again when the reply isn't valid
    # JSON. create_message, inside it, retries only a passing transport fault.
    while attempts <= max_retries:
        try:
            response = create_message(
                client,
                what="model.inventory_update",
                model="claude-sonnet-4-6",
                max_tokens=1024,
                messages=[{"role": "user", "content": prompt}],
            )
            text = response.content[0].text
            return _parse_claude_response(text)
        except (json.JSONDecodeError, ValueError):
            attempts += 1
            if attempts > max_retries:
                logger.warning("Failed to parse Claude response after retries")
                extraction_failed(_EXTRACTION_STAGE, "invalid_json")
                return []
        except Exception:
            logger.exception("Claude API call failed")
            extraction_failed(_EXTRACTION_STAGE, "call_failed")
            return []
    return []  # pragma: no cover
