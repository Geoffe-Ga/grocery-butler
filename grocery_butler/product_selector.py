"""Claude-assisted product selection with brand preference support.

Selects the best Safeway product from search results using Claude,
respecting brand preferences (preferred/avoided) and price sensitivity.

Selection flow:
1. Filter out products from avoided brands
2. Load brand preferences (ingredient-level overrides category-level)
3. Ask Claude to select the best product given preferences
4. Return the selected product with reasoning
"""

from __future__ import annotations

import json
import logging
from dataclasses import dataclass
from typing import TYPE_CHECKING, Any

from grocery_butler.claude_utils import extract_json_text, filter_avoided_brands
from grocery_butler.models import (
    BrandPreference,
    BrandPreferenceType,
    PriceSensitivity,
    SafewayProduct,
    ShoppingListItem,
)
from grocery_butler.prompt_loader import load_prompt

if TYPE_CHECKING:
    from grocery_butler.recipe_store import RecipeStore

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-20250514"
_MAX_TOKENS = 1024


class ProductSelectionError(Exception):
    """Raised when product selection fails."""


@dataclass
class SelectionResult:
    """The outcome of product selection for one shopping list item.

    Attributes:
        item: The original shopping list item.
        product: The selected product, or None if no match found.
        reasoning: Claude's explanation for the selection.
    """

    item: ShoppingListItem
    product: SafewayProduct | None
    reasoning: str


class ProductSelector:
    """Claude-assisted product selector with brand preferences.

    Args:
        claude_client: Anthropic API client.
        recipe_store: Database access for brand preferences.
    """

    def __init__(
        self,
        claude_client: Any,
        recipe_store: RecipeStore,
    ) -> None:
        """Initialize the product selector.

        Args:
            claude_client: Anthropic API client (typed as Any for flexibility).
            recipe_store: Database access for brand preferences.
        """
        self._client = claude_client
        self._store = recipe_store

    def select_product(
        self,
        item: ShoppingListItem,
        candidates: list[SafewayProduct],
    ) -> SelectionResult:
        """Select the best product for a shopping list item.

        Filters out avoided brands, then uses Claude to pick the
        best option respecting brand preferences and price sensitivity.

        Args:
            item: The shopping list item needing a product.
            candidates: Search results from Safeway.

        Returns:
            SelectionResult with chosen product and reasoning.
        """
        if not candidates:
            return SelectionResult(
                item=item,
                product=None,
                reasoning="No products available",
            )

        brand_prefs = self._get_brand_preferences(item)
        filtered = filter_avoided_brands(candidates, brand_prefs)

        if not filtered:
            return SelectionResult(
                item=item,
                product=None,
                reasoning="All available products are from avoided brands",
            )

        price_sensitivity = self._get_price_sensitivity()
        return self._ask_claude(item, filtered, brand_prefs, price_sensitivity)

    def select_products(
        self,
        items_and_candidates: list[tuple[ShoppingListItem, list[SafewayProduct]]],
    ) -> list[SelectionResult]:
        """Select products for multiple shopping list items.

        Args:
            items_and_candidates: List of (item, candidates) tuples.

        Returns:
            List of SelectionResult for each item.
        """
        return [
            self.select_product(item, candidates)
            for item, candidates in items_and_candidates
        ]

    # ------------------------------------------------------------------
    # Internal helpers
    # ------------------------------------------------------------------

    def _get_brand_preferences(
        self,
        item: ShoppingListItem,
    ) -> list[BrandPreference]:
        """Get brand preferences for a shopping list item.

        Checks ingredient-level first, falls back to category-level.

        Args:
            item: The shopping list item.

        Returns:
            List of relevant brand preferences.
        """
        return self._store.get_brands_for_ingredient(
            item.ingredient,
            category=item.category.value,
        )

    def _get_price_sensitivity(self) -> str:
        """Get the user's price sensitivity setting.

        Returns:
            Price sensitivity string, defaults to "moderate".
        """
        value = self._store.get_preference("price_sensitivity")
        if value is not None:
            try:
                return PriceSensitivity(value).value
            except ValueError:
                pass
        return PriceSensitivity.MODERATE.value

    def _ask_claude(
        self,
        item: ShoppingListItem,
        candidates: list[SafewayProduct],
        brand_prefs: list[BrandPreference],
        price_sensitivity: str,
    ) -> SelectionResult:
        """Ask Claude to select the best product.

        Args:
            item: The shopping list item.
            candidates: Filtered product candidates.
            brand_prefs: Relevant brand preferences.
            price_sensitivity: User's price sensitivity setting.

        Returns:
            SelectionResult with Claude's selection.
        """
        prompt = self._build_prompt(item, candidates, brand_prefs, price_sensitivity)
        response_text = self._call_claude(prompt)
        if response_text is None:
            return self._fallback_selection(item, candidates, brand_prefs)

        result = _parse_selection_response(response_text, candidates)
        if result is not None:
            return SelectionResult(item=item, product=result[0], reasoning=result[1])

        return self._fallback_selection(item, candidates, brand_prefs)

    def _build_prompt(
        self,
        item: ShoppingListItem,
        candidates: list[SafewayProduct],
        brand_prefs: list[BrandPreference],
        price_sensitivity: str,
    ) -> str:
        """Build the product selection prompt.

        Args:
            item: The shopping list item.
            candidates: Filtered product candidates.
            brand_prefs: Relevant brand preferences.
            price_sensitivity: User's price sensitivity.

        Returns:
            Formatted prompt string.
        """
        products_json = json.dumps(
            [_product_to_dict(p) for p in candidates],
            indent=2,
        )
        brand_text = _format_brand_preferences(brand_prefs)

        return load_prompt(
            "product_selection",
            ingredient=item.ingredient,
            quantity=str(item.quantity),
            unit=str(item.unit),
            search_term=item.search_term,
            category=item.category.value,
            products_json=products_json,
            brand_preferences=brand_text,
            price_sensitivity=price_sensitivity,
        )

    def _call_claude(self, prompt: str) -> str | None:
        """Send a prompt to Claude and return the text response.

        Args:
            prompt: The formatted prompt string.

        Returns:
            Response text or None on failure.
        """
        try:
            response = self._client.messages.create(
                model=_MODEL,
                max_tokens=_MAX_TOKENS,
                messages=[{"role": "user", "content": prompt}],
            )
            return str(response.content[0].text)
        except Exception:
            logger.exception("Claude product selection call failed")
            return None

    @staticmethod
    def _fallback_selection(
        item: ShoppingListItem,
        candidates: list[SafewayProduct],
        brand_prefs: list[BrandPreference],
    ) -> SelectionResult:
        """Select a product without Claude using simple heuristics.

        Prefers preferred-brand in-stock products, then cheapest.

        Args:
            item: The shopping list item.
            candidates: Filtered product candidates.
            brand_prefs: Relevant brand preferences.

        Returns:
            SelectionResult from heuristic selection.
        """
        preferred = _get_preferred_brands(brand_prefs)
        best = _heuristic_select(candidates, preferred)
        return SelectionResult(
            item=item,
            product=best,
            reasoning="Selected via fallback heuristic (Claude unavailable)",
        )


# ------------------------------------------------------------------
# Pure helper functions
# ------------------------------------------------------------------


def _get_preferred_brands(
    brand_prefs: list[BrandPreference],
) -> set[str]:
    """Extract preferred brand names (lowercased).

    Args:
        brand_prefs: Brand preferences to check.

    Returns:
        Set of preferred brand names in lowercase.
    """
    return {
        pref.brand.lower()
        for pref in brand_prefs
        if pref.preference_type == BrandPreferenceType.PREFERRED
    }


def _heuristic_select(
    candidates: list[SafewayProduct],
    preferred_brands: set[str],
) -> SafewayProduct:
    """Select a product using simple heuristics.

    Priority: in-stock preferred brand > in-stock any > cheapest.

    Args:
        candidates: Non-empty list of product candidates.
        preferred_brands: Lowercased preferred brand names.

    Returns:
        The best candidate by heuristic ranking.
    """
    in_stock = [p for p in candidates if p.in_stock]
    pool = in_stock if in_stock else candidates

    if preferred_brands:
        preferred_matches = [
            p for p in pool if any(b in p.name.lower() for b in preferred_brands)
        ]
        if preferred_matches:
            return min(preferred_matches, key=lambda p: p.price)

    return min(pool, key=lambda p: p.price)


def _product_to_dict(product: SafewayProduct) -> dict[str, Any]:
    """Convert a SafewayProduct to a dict for JSON serialization.

    Args:
        product: The product to convert.

    Returns:
        Dict with product fields.
    """
    return {
        "product_id": product.product_id,
        "name": product.name,
        "price": product.price,
        "unit_price": product.unit_price,
        "size": product.size,
        "in_stock": product.in_stock,
    }


def _format_brand_preferences(prefs: list[BrandPreference]) -> str:
    """Format brand preferences as human-readable text.

    Args:
        prefs: List of brand preferences.

    Returns:
        Formatted string describing preferences.
    """
    if not prefs:
        return "No brand preferences set."

    lines: list[str] = []
    for pref in prefs:
        action = (
            "PREFER"
            if pref.preference_type == BrandPreferenceType.PREFERRED
            else "AVOID"
        )
        lines.append(
            f"- {action}: {pref.brand} (for {pref.match_type}: {pref.match_target})"
        )
    return "\n".join(lines)


def _parse_selection_response(
    text: str,
    candidates: list[SafewayProduct],
) -> tuple[SafewayProduct | None, str] | None:
    """Parse Claude's product selection response.

    Args:
        text: Raw response text from Claude.
        candidates: The product candidates that were presented.

    Returns:
        Tuple of (selected_product, reasoning) or None on parse failure.
    """
    cleaned = extract_json_text(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Failed to parse product selection response as JSON")
        return None

    if not isinstance(data, dict):
        return None

    index = data.get("selected_index")
    reasoning = str(data.get("reasoning", "No reasoning provided"))

    if not isinstance(index, int):
        return None

    if index == -1:
        return (None, reasoning)

    if 0 <= index < len(candidates):
        return (candidates[index], reasoning)

    return None
