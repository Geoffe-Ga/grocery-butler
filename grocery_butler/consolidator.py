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
from grocery_butler.models import (
    IngredientCategory,
    ShoppingListItem,
    Unit,
    coerce_category,
    parse_unit,
)
from grocery_butler.prompt_loader import load_prompt
from grocery_butler.units import convert, dimension_of

if TYPE_CHECKING:
    from grocery_butler.config import Config
    from grocery_butler.models import Ingredient, InventoryItem, ParsedMeal

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

    category = coerce_category(data.get("category", "other"))

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


RESTOCK_REASON = "restock"


def _quantity_in(quantity: float, unit: Unit, target_unit: Unit) -> float:
    """Express a quantity in another unit of the same merge group.

    Callers only combine quantities that share a group key, so the units are
    always convertible. If they ever were not, the quantity is returned
    unconverted rather than dropped.

    Args:
        quantity: The amount to convert.
        unit: The unit it is in.
        target_unit: The unit to express it in.

    Returns:
        The converted amount.
    """
    converted = convert(quantity, unit, target_unit)
    return converted if converted is not None else quantity


def _absorb_line(target: ShoppingListItem, extra: ShoppingListItem) -> ShoppingListItem:
    """Fold one line into another line of the same ingredient and dimension.

    Args:
        target: The line that survives; its unit and category are kept.
        extra: The line folded into it.

    Returns:
        A new line with the summed quantity and both lines' reasons. A
        search term someone chose (one that differs from the bare
        ingredient name) is kept in preference to a default one, and a
        price estimate on either line is kept.
    """
    chosen = target.search_term.lower() != target.ingredient.lower()
    return target.model_copy(
        update={
            "quantity": target.quantity
            + _quantity_in(extra.quantity, extra.unit, target.unit),
            "from_meals": _union(target.from_meals, extra.from_meals),
            "search_term": target.search_term if chosen else extra.search_term,
            "estimated_price": (
                extra.estimated_price
                if target.estimated_price is None
                else target.estimated_price
            ),
        }
    )


def _union(first: list[str], second: list[str]) -> list[str]:
    """Join two reason lists, keeping order and dropping repeats.

    Args:
        first: Reasons already on a line.
        second: Reasons to add.

    Returns:
        The combined reasons.
    """
    return [*first, *[reason for reason in second if reason not in first]]


def _share_restock_reason(lines: list[ShoppingListItem]) -> list[ShoppingListItem]:
    """Make unmergeable lines of a restocked ingredient show every reason.

    When an ingredient has a restock line and meal lines whose units can't
    be added to it, each meal line gains the restock reason and the restock
    line gains the meal names. Two meal lines never trade names.

    Args:
        lines: Lines already merged by ingredient and dimension.

    Returns:
        The lines, with reasons shared where a restock line exists.
    """
    by_name: dict[str, list[ShoppingListItem]] = {}
    for line in lines:
        by_name.setdefault(line.ingredient.lower(), []).append(line)

    shared: list[ShoppingListItem] = []
    for line in lines:
        siblings = [s for s in by_name[line.ingredient.lower()] if s is not line]
        if RESTOCK_REASON in line.from_meals:
            extra = [r for s in siblings for r in s.from_meals]
        elif any(RESTOCK_REASON in s.from_meals for s in siblings):
            extra = [RESTOCK_REASON]
        else:
            extra = []
        reasons = _union(line.from_meals, extra)
        shared.append(line.model_copy(update={"from_meals": reasons}))
    return shared


def merge_duplicate_lines(lines: list[ShoppingListItem]) -> list[ShoppingListItem]:
    """Merge shopping-list lines that are the same ingredient (issue #141).

    An ingredient needed by a meal and also on the restock queue is one
    line carrying both reasons, with quantities converted and summed, when
    the units share a dimension. Where they don't, the lines stay separate
    and each shows the other's reason. Applied to the pure-Python list and
    to Claude's, so both paths give the same guarantee.

    Args:
        lines: Shopping-list lines, possibly with repeats.

    Returns:
        New lines, in first-appearance order, with repeats merged.
    """
    merged: dict[tuple[str, str], ShoppingListItem] = {}
    for line in lines:
        key = Consolidator._ingredient_group_key(line.ingredient, line.unit)
        merged[key] = _absorb_line(merged[key], line) if key in merged else line
    return _share_restock_reason(list(merged.values()))


class Consolidator:
    """Consolidates meal ingredients into a unified shopping list.

    Uses Claude API to intelligently merge quantities, handle unit
    conversions, and exclude pantry staples. Falls back to a pure-Python,
    dimension-aware merge (see :meth:`consolidate_simple`) when no API
    client is available.
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
            return merge_duplicate_lines(items)

        # Retry once on invalid JSON
        retry_text = self._retry_claude(prompt, response_text)
        if retry_text is not None:
            retried = _parse_response_items(retry_text)
            if retried is not None:
                return merge_duplicate_lines(retried)

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

        Performs dimension-aware merging: ingredients sharing a name and a
        compatible unit (same physical dimension -- mass, volume, or count)
        have their quantities converted to a common unit and summed into a
        single line item. Ingredients with incompatible units (e.g. a
        packaging unit like "jar" vs. "can", or a dimensioned unit vs. a
        packaging unit) are kept as separate line items rather than being
        naively added together. Used when the API is unavailable or in
        tests.

        Args:
            meals: List of parsed meals with ingredient lists.
            restock_queue: Inventory items to check for restocking.
            pantry_staples: Names of pantry staple ingredients.

        Returns:
            Consolidated shopping list with unit-aware merging. An
            ingredient that a meal needs and that is also on the restock
            queue is one line with both reasons (see
            :func:`merge_duplicate_lines`).
        """
        pantry_lower = {s.lower() for s in pantry_staples}
        merged = self._merge_meal_ingredients(meals, pantry_lower)
        result = self._build_items_from_merged(merged)
        result.extend(self._build_restock_items(restock_queue))
        return merge_duplicate_lines(result)

    # ------------------------------------------------------------------
    # Simple consolidation helpers
    # ------------------------------------------------------------------

    @staticmethod
    def _ingredient_group_key(ingredient: str, unit: Unit) -> tuple[str, str]:
        """Build the composite merge key for an ingredient/unit pairing.

        Units with a fixed physical dimension (mass, volume, count) are
        grouped by dimension so compatible units (e.g. cup and gallon) merge
        into a single line item. Packaging/"other" units with no fixed
        dimension are grouped by their exact unit instead, so incompatible
        packaging (e.g. jar vs. can) stays split into separate line items.

        Args:
            ingredient: Raw ingredient name (not yet lowercased).
            unit: The ingredient's parsed unit.

        Returns:
            A ``(ingredient_lower, group_token)`` tuple suitable for use as
            a merge dict key.
        """
        dimension = dimension_of(unit)
        if dimension is not None:
            return (ingredient.lower(), f"dim:{dimension.value}")
        return (ingredient.lower(), f"unit:{unit.value}")

    @staticmethod
    def _merge_quantity(entry: dict[str, object], item: Ingredient) -> None:
        """Add an ingredient's quantity into an existing merge entry in place.

        Converts ``item.quantity`` from ``item.unit`` into the entry's
        canonical unit before summing, so dimensionally compatible units
        (e.g. cups and gallons) combine correctly. If the units turn out to
        be incomparable (``convert`` returns ``None`` -- unreachable in
        normal orchestration since entries only share a group key when
        their units are compatible, but guarded defensively), falls back to
        adding the raw quantities unconverted.

        Args:
            entry: The existing merge entry to update in place. Expected to
                hold ``"quantity"`` (a number) and ``"unit"`` (a ``Unit``).
            item: The ingredient whose quantity is being merged in.

        Returns:
            None. ``entry["quantity"]`` is updated in place.
        """
        raw_qty = entry.get("quantity", 0.0)
        current_qty = float(raw_qty) if isinstance(raw_qty, (int, float)) else 0.0

        raw_unit = entry.get("unit")
        canonical_unit = raw_unit if isinstance(raw_unit, Unit) else Unit.EACH

        entry["quantity"] = current_qty + _quantity_in(
            item.quantity, item.unit, canonical_unit
        )

    @staticmethod
    def _merge_meal_ingredients(
        meals: list[ParsedMeal],
        pantry_lower: set[str],
    ) -> dict[tuple[str, str], dict[str, object]]:
        """Merge ingredient quantities across meals, excluding pantry staples.

        Ingredients are grouped by ``(ingredient_lower, group_token)`` via
        :meth:`_ingredient_group_key` so unit conversion only ever combines
        dimensionally compatible quantities; incompatible units for the same
        ingredient are kept as separate line items.

        Args:
            meals: List of parsed meals.
            pantry_lower: Lowercased pantry staple names to exclude.

        Returns:
            Dict keyed by ``(ingredient_lower, group_token)`` with merged
            data.
        """
        merged: dict[tuple[str, str], dict[str, object]] = {}
        for meal in meals:
            for item in meal.purchase_items:
                if item.ingredient.lower() in pantry_lower:
                    continue
                key = Consolidator._ingredient_group_key(item.ingredient, item.unit)
                if key in merged:
                    entry = merged[key]
                    Consolidator._merge_quantity(entry, item)
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
        merged: dict[tuple[str, str], dict[str, object]],
    ) -> list[ShoppingListItem]:
        """Convert merged ingredient dict into ShoppingListItem list.

        Args:
            merged: Dict keyed by ``(ingredient_lower, group_token)`` with
                merged data.

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
            List of ShoppingListItem with from_meals=[RESTOCK_REASON].
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
                    from_meals=[RESTOCK_REASON],
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
        return "claude-sonnet-4-6"
