"""Claude-powered ingredient consolidation and shopping list generation.

Takes parsed meals, restock queue, and pantry state, then produces
a consolidated shopping list with merged quantities and categories.
Falls back to pure-Python dedup when no API client is configured.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from grocery_butler.claude_utils import extract_json_text
from grocery_butler.models import IngredientCategory, ShoppingListItem, Unit, parse_unit
from grocery_butler.prompt_loader import load_prompt

if TYPE_CHECKING:
    from grocery_butler.config import Config
    from grocery_butler.models import InventoryItem, ParsedMeal

logger = logging.getLogger(__name__)


def _format_pantry_staples(pantry_staples: list[str]) -> str:
    """Format pantry staples list for the prompt template.

    Args:
        pantry_staples: List of pantry staple ingredient names.

    Returns:
        Formatted string for prompt insertion.
    """
    if not pantry_staples:
        return "None"
    return ", ".join(pantry_staples)


def _format_restock_queue(restock_queue: list[InventoryItem]) -> str:
    """Format restock queue items for the prompt template.

    Only includes items with status 'low' or 'out'.

    Args:
        restock_queue: List of inventory items to check.

    Returns:
        Formatted string for prompt insertion.
    """
    entries: list[str] = []
    for item in restock_queue:
        if item.status not in ("low", "out"):
            continue
        parts = [f"- {item.display_name} (status: {item.status})"]
        if item.default_quantity is not None and item.default_unit is not None:
            parts.append(f"  qty: {item.default_quantity} {item.default_unit}")
        entries.append(" ".join(parts))
    if not entries:
        return "None"
    return "\n".join(entries)


def _format_inventory_overrides(inventory_overrides: list[str] | None) -> str:
    """Format inventory override staples for the prompt template.

    These are pantry staples that need restocking despite being staples.

    Args:
        inventory_overrides: List of staple names that need restocking.

    Returns:
        Formatted string for prompt insertion.
    """
    if not inventory_overrides:
        return "None"
    return ", ".join(inventory_overrides)


def _parse_shopping_item(data: dict[str, object]) -> ShoppingListItem:
    """Parse a single shopping list item dict into a ShoppingListItem model.

    Args:
        data: Dictionary with shopping list item fields.

    Returns:
        Validated ShoppingListItem instance.
    """
    raw_quantity = data.get("quantity", 0)
    quantity = float(raw_quantity) if isinstance(raw_quantity, (int, float)) else 0.0

    raw_price = data.get("estimated_price")
    estimated_price: float | None = None
    if isinstance(raw_price, (int, float)):
        estimated_price = float(raw_price)

    raw_from_meals = data.get("from_meals", [])
    from_meals: list[str] = (
        [str(m) for m in raw_from_meals] if isinstance(raw_from_meals, list) else []
    )

    raw_category = str(data.get("category", "other"))
    try:
        category = IngredientCategory(raw_category)
    except ValueError:
        category = IngredientCategory.OTHER

    return ShoppingListItem(
        ingredient=str(data.get("ingredient", "")),
        quantity=quantity,
        unit=parse_unit(str(data.get("unit", ""))),
        category=category,
        search_term=str(data.get("search_term", "")),
        from_meals=from_meals,
        estimated_price=estimated_price,
    )


def _parse_response_items(response_text: str) -> list[ShoppingListItem] | None:
    """Parse Claude's JSON response into a list of ShoppingListItem.

    Args:
        response_text: Raw JSON response from Claude.

    Returns:
        List of ShoppingListItem or None if parsing fails.
    """
    try:
        cleaned = extract_json_text(response_text)
        data = json.loads(cleaned)
    except (json.JSONDecodeError, ValueError):
        logger.warning("Failed to parse consolidation response")
        return None

    if not isinstance(data, list):
        return None

    return [_parse_shopping_item(item) for item in data if isinstance(item, dict)]


def _flatten_meal_ingredients(
    meals: list[ParsedMeal],
) -> list[tuple[str, float, str, str, str]]:
    """Flatten all purchase items from meals into a list of tuples.

    Each tuple contains (ingredient, quantity, unit, category, meal_name).

    Args:
        meals: List of parsed meals.

    Returns:
        List of (ingredient, quantity, unit, category, meal_name) tuples.
    """
    result: list[tuple[str, float, str, str, str]] = []
    for meal in meals:
        for item in meal.purchase_items:
            result.append(
                (
                    item.ingredient,
                    item.quantity,
                    item.unit,
                    str(item.category),
                    meal.name,
                )
            )
    return result


def _build_ingredient_text(
    meals: list[ParsedMeal],
) -> str:
    """Build ingredient list text for the consolidation prompt.

    Args:
        meals: List of parsed meals.

    Returns:
        Formatted ingredient list string.
    """
    if not meals:
        return "No meal ingredients."
    lines: list[str] = []
    for meal in meals:
        lines.append(f"### {meal.name} ({meal.servings} servings)")
        for item in meal.purchase_items:
            lines.append(f"- {item.quantity} {item.unit} {item.ingredient}")
    return "\n".join(lines)


class Consolidator:
    """Consolidates meal ingredients into a unified shopping list.

    Uses Claude API to intelligently merge quantities, handle unit
    conversions, and exclude pantry staples. Falls back to pure-Python
    dedup when no API client is available.
    """

    def __init__(
        self,
        anthropic_client: Any = None,
        config: Config | None = None,
    ) -> None:
        """Initialize the consolidator with dependencies.

        Args:
            anthropic_client: Optional Anthropic SDK client for Claude calls.
            config: Optional application configuration.
        """
        self._client = anthropic_client
        self._config = config

    # ------------------------------------------------------------------
    # Public API
    # ------------------------------------------------------------------

    def consolidate(
        self,
        meals: list[ParsedMeal],
        restock_queue: list[InventoryItem],
        pantry_staples: list[str],
        inventory_overrides: list[str] | None = None,
    ) -> list[ShoppingListItem]:
        """Consolidate meal ingredients into a shopping list using Claude.

        Steps:
        1. Flatten purchase_items from all meals with from_meals tracking.
        2. Format pantry staples, restock queue, and inventory overrides.
        3. Call Claude with the ingredient_consolidation prompt.
        4. Parse the response into ShoppingListItem list.
        5. Retry once on invalid JSON.
        6. Fall back to consolidate_simple on total failure.

        Args:
            meals: List of parsed meals with ingredient lists.
            restock_queue: Inventory items to check for restocking.
            pantry_staples: Names of pantry staple ingredients.
            inventory_overrides: Staples that need restocking despite
                being pantry items.

        Returns:
            Consolidated shopping list.
        """
        if self._client is None:
            return self.consolidate_simple(
                meals,
                restock_queue,
                pantry_staples,
            )

        prompt = self._build_prompt(
            meals,
            restock_queue,
            pantry_staples,
            inventory_overrides,
        )

        response_text = self._call_claude(prompt)
        if response_text is None:
            return self.consolidate_simple(
                meals,
                restock_queue,
                pantry_staples,
            )

        items = _parse_response_items(response_text)
        if items is not None:
            return items

        # Retry once on invalid JSON
        retry_text = self._retry_claude(prompt, response_text)
        if retry_text is not None:
            retried = _parse_response_items(retry_text)
            if retried is not None:
                return retried

        return self.consolidate_simple(
            meals,
            restock_queue,
            pantry_staples,
        )

    def consolidate_simple(
        self,
        meals: list[ParsedMeal],
        restock_queue: list[InventoryItem],
        pantry_staples: list[str],
    ) -> list[ShoppingListItem]:
        """Pure-Python fallback consolidation without Claude.

        Performs basic dedup and quantity summing. Used when the API
        is unavailable or in tests.

        Args:
            meals: List of parsed meals with ingredient lists.
            restock_queue: Inventory items to check for restocking.
            pantry_staples: Names of pantry staple ingredients.

        Returns:
            Consolidated shopping list with basic merging.
        """
        pantry_lower = {s.lower() for s in pantry_staples}
        merged = self._merge_meal_ingredients(meals, pantry_lower)
        result = self._build_items_from_merged(merged)
        restock_items = self._build_restock_items(restock_queue)
        result.extend(restock_items)
        return result

    # ------------------------------------------------------------------
    # Simple consolidation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _merge_meal_ingredients(
        meals: list[ParsedMeal],
        pantry_lower: set[str],
    ) -> dict[str, dict[str, object]]:
        """Merge ingredient quantities across meals, excluding pantry staples.

        Args:
            meals: List of parsed meals.
            pantry_lower: Lowercased pantry staple names to exclude.

        Returns:
            Dict keyed by ingredient name with merged data.
        """
        merged: dict[str, dict[str, object]] = {}
        for meal in meals:
            for item in meal.purchase_items:
                key = item.ingredient.lower()
                if key in pantry_lower:
                    continue
                if key in merged:
                    entry = merged[key]
                    raw_qty = entry.get("quantity", 0.0)
                    current_qty = (
                        float(raw_qty) if isinstance(raw_qty, (int, float)) else 0.0
                    )
                    entry["quantity"] = current_qty + item.quantity
                    raw_meals = entry.get("from_meals", [])
                    from_meals_list: list[str] = (
                        list(raw_meals) if isinstance(raw_meals, list) else []
                    )
                    if meal.name not in from_meals_list:
                        from_meals_list.append(meal.name)
                    entry["from_meals"] = from_meals_list
                else:
                    merged[key] = {
                        "ingredient": item.ingredient,
                        "quantity": item.quantity,
                        "unit": item.unit,
                        "category": str(item.category),
                        "from_meals": [meal.name],
                    }
        return merged

    @staticmethod
    def _build_items_from_merged(
        merged: dict[str, dict[str, object]],
    ) -> list[ShoppingListItem]:
        """Convert merged ingredient dict into ShoppingListItem list.

        Args:
            merged: Dict keyed by ingredient name with merged data.

        Returns:
            List of ShoppingListItem instances.
        """
        result: list[ShoppingListItem] = []
        for entry in merged.values():
            raw_qty = entry.get("quantity", 0.0)
            quantity = float(raw_qty) if isinstance(raw_qty, (int, float)) else 0.0
            raw_from = entry.get("from_meals", [])
            from_meals: list[str] = (
                [str(m) for m in raw_from] if isinstance(raw_from, list) else []
            )
            raw_category = str(entry.get("category", "other"))
            try:
                category = IngredientCategory(raw_category)
            except ValueError:
                category = IngredientCategory.OTHER
            ingredient_name = str(entry.get("ingredient", ""))
            result.append(
                ShoppingListItem(
                    ingredient=ingredient_name,
                    quantity=quantity,
                    unit=parse_unit(str(entry.get("unit", ""))),
                    category=category,
                    search_term=ingredient_name,
                    from_meals=from_meals,
                    estimated_price=None,
                )
            )
        return result

    @staticmethod
    def _build_restock_items(
        restock_queue: list[InventoryItem],
    ) -> list[ShoppingListItem]:
        """Build ShoppingListItem entries for restock queue items.

        Only includes items with status 'low' or 'out'.

        Args:
            restock_queue: Inventory items to check.

        Returns:
            List of ShoppingListItem with from_meals=["restock"].
        """
        items: list[ShoppingListItem] = []
        for inv in restock_queue:
            if inv.status not in ("low", "out"):
                continue
            items.append(
                ShoppingListItem(
                    ingredient=inv.ingredient,
                    quantity=inv.default_quantity
                    if inv.default_quantity is not None
                    else 1.0,
                    unit=inv.default_unit
                    if inv.default_unit is not None
                    else Unit.EACH,
                    category=inv.category
                    if inv.category is not None
                    else IngredientCategory.OTHER,
                    search_term=inv.default_search_term
                    if inv.default_search_term is not None
                    else inv.ingredient,
                    from_meals=["restock"],
                    estimated_price=None,
                )
            )
        return items

    # ------------------------------------------------------------------
    # Claude API helpers
    # ------------------------------------------------------------------

    def _build_prompt(
        self,
        meals: list[ParsedMeal],
        restock_queue: list[InventoryItem],
        pantry_staples: list[str],
        inventory_overrides: list[str] | None,
    ) -> str:
        """Build the consolidation prompt from template and data.

        Args:
            meals: List of parsed meals.
            restock_queue: Inventory items for restocking.
            pantry_staples: Pantry staple names.
            inventory_overrides: Staples that need restocking.

        Returns:
            Formatted prompt string.
        """
        ingredient_text = _build_ingredient_text(meals)
        override_text = _format_inventory_overrides(inventory_overrides)
        restock_text = _format_restock_queue(restock_queue)

        prompt = load_prompt(
            "ingredient_consolidation",
            pantry_staples=_format_pantry_staples(pantry_staples),
            restock_queue=restock_text,
            restock_items=override_text,
        )
        return f"{prompt}\n\n## Meal Ingredients\n{ingredient_text}"

    def _call_claude(self, prompt: str) -> str | None:
        """Send a prompt to Claude and return the text response.

        Args:
            prompt: The formatted prompt string.

        Returns:
            Response text or None on failure.
        """
        try:
            response = self._client.messages.create(
                model=self._get_model(),
                max_tokens=4096,
                messages=[{"role": "user", "content": prompt}],
            )
            return str(response.content[0].text)
        except Exception:
            logger.exception("Claude API call failed")
            return None

    def _retry_claude(
        self,
        original_prompt: str,
        bad_response: str,
    ) -> str | None:
        """Retry a Claude call after invalid JSON.

        Args:
            original_prompt: The original prompt that produced bad output.
            bad_response: The invalid response text.

        Returns:
            New response text or None on failure.
        """
        try:
            response = self._client.messages.create(
                model=self._get_model(),
                max_tokens=4096,
                messages=[
                    {"role": "user", "content": original_prompt},
                    {"role": "assistant", "content": bad_response},
                    {
                        "role": "user",
                        "content": (
                            "Your previous response was not valid JSON. "
                            "Please return ONLY valid JSON with no "
                            "markdown fences or explanation."
                        ),
                    },
                ],
            )
            return str(response.content[0].text)
        except Exception:
            logger.exception("Claude retry call failed")
            return None

    def _get_model(self) -> str:
        """Get the Claude model name.

        Returns:
            Model identifier string.
        """
        return "claude-sonnet-4-20250514"
