"""Out-of-stock substitution flow with Claude-ranked alternatives.

When a selected product is out of stock, this module searches for
alternatives and uses Claude to rank them by suitability.  Substitutions
are never silent -- they always produce a :class:`SubstitutionResult`
that must be presented to the user for approval.
"""

from __future__ import annotations

import json
import logging
from typing import TYPE_CHECKING, Any

from grocery_butler.claude_utils import extract_json_text, filter_avoided_brands
from grocery_butler.models import (
    BrandPreference,
    BrandPreferenceType,
    SafewayProduct,
    ShoppingListItem,
    SubstitutionOption,
    SubstitutionResult,
    SubstitutionSuitability,
)
from grocery_butler.prompt_loader import load_prompt

if TYPE_CHECKING:
    from grocery_butler.product_search import ProductSearchService
    from grocery_butler.recipe_store import RecipeStore

logger = logging.getLogger(__name__)

_MODEL = "claude-sonnet-4-6"
_MAX_TOKENS = 2048


class SubstitutionService:
    """Rank alternative products for out-of-stock items.

    Args:
        claude_client: Anthropic API client (typed as Any).
        search_service: Product search service for finding alternatives.
        recipe_store: Database access for brand preferences.
    """

    def __init__(
        self,
        claude_client: Any,
        search_service: ProductSearchService,
        recipe_store: RecipeStore,
    ) -> None:
        """Initialize the substitution service.

        Args:
            claude_client: Anthropic API client.
            search_service: Product search service.
            recipe_store: Database for brand preferences.
        """
        self._client = claude_client
        self._search = search_service
        self._store = recipe_store

    def find_substitutions(
        self,
        item: ShoppingListItem,
        original_product: SafewayProduct,
    ) -> SubstitutionResult:
        """Find and rank substitutions for an out-of-stock product.

        Searches for alternatives, filters out avoided brands,
        removes the original product, then ranks with Claude.

        Args:
            item: The shopping list item needing a substitute.
            original_product: The out-of-stock product.

        Returns:
            SubstitutionResult with ranked alternatives.
        """
        alternatives = self._search_alternatives(item, original_product)
        if not alternatives:
            return SubstitutionResult(
                status="no_alternatives",
                original_item=item,
                message="No alternative products found",
            )

        brand_prefs = self._store.get_brands_for_ingredient(
            item.ingredient,
            category=item.category.value,
        )
        filtered = filter_avoided_brands(alternatives, brand_prefs)
        if not filtered:
            return SubstitutionResult(
                status="all_avoided",
                original_item=item,
                message="All alternatives are from avoided brands",
            )

        ranked = self._rank_with_claude(item, filtered, brand_prefs)
        return SubstitutionResult(
            status="alternatives_found",
            original_item=item,
            alternatives=ranked,
            message=f"Found {len(ranked)} alternative(s)",
        )

    # ------------------------------------------------------------------
    # Internal
    # ------------------------------------------------------------------

    def _search_alternatives(
        self,
        item: ShoppingListItem,
        original: SafewayProduct,
    ) -> list[SafewayProduct]:
        """Search for alternative products, excluding the original.

        Args:
            item: Shopping list item for the search term.
            original: The out-of-stock product to exclude.

        Returns:
            List of in-stock alternative products.
        """
        results = self._search.search_products(item.search_term)
        return [
            p for p in results if p.product_id != original.product_id and p.in_stock
        ]

    def _rank_with_claude(
        self,
        item: ShoppingListItem,
        alternatives: list[SafewayProduct],
        brand_prefs: list[BrandPreference],
    ) -> list[SubstitutionOption]:
        """Use Claude to rank alternatives by suitability.

        Args:
            item: The shopping list item.
            alternatives: Candidate products.
            brand_prefs: Relevant brand preferences.

        Returns:
            Ranked list of SubstitutionOption.
        """
        prompt = self._build_prompt(item, alternatives, brand_prefs)
        response_text = self._call_claude(prompt)
        if response_text is None:
            return _fallback_ranking(alternatives)

        ranked = _parse_ranking_response(response_text, alternatives)
        if ranked is not None:
            return ranked

        return _fallback_ranking(alternatives)

    def _build_prompt(
        self,
        item: ShoppingListItem,
        alternatives: list[SafewayProduct],
        brand_prefs: list[BrandPreference],
    ) -> str:
        """Build the substitution ranking prompt.

        Args:
            item: The shopping list item.
            alternatives: Products to rank.
            brand_prefs: Brand preferences.

        Returns:
            Formatted prompt string.
        """
        alt_dicts = [
            {
                "name": p.name,
                "price": p.price,
                "size": p.size,
                "product_id": p.product_id,
            }
            for p in alternatives
        ]
        brand_text = _format_brand_prefs(brand_prefs)
        return load_prompt(
            "substitution_ranking",
            ingredient=item.ingredient,
            quantity=str(item.quantity),
            unit=str(item.unit),
            search_term=item.search_term,
            alternatives_json=json.dumps(alt_dicts, indent=2),
            brand_preferences=brand_text,
        )

    def _call_claude(self, prompt: str) -> str | None:
        """Send a prompt to Claude.

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
            logger.exception("Claude substitution ranking call failed")
            return None


# ------------------------------------------------------------------
# Pure helper functions
# ------------------------------------------------------------------


def _format_brand_prefs(prefs: list[BrandPreference]) -> str:
    """Format brand preferences as text.

    Args:
        prefs: Brand preference list.

    Returns:
        Human-readable brand preferences string.
    """
    if not prefs:
        return "None"
    parts: list[str] = []
    for pref in prefs:
        label = (
            "prefer"
            if pref.preference_type == BrandPreferenceType.PREFERRED
            else "avoid"
        )
        parts.append(f"{label} {pref.brand}")
    return ", ".join(parts)


def _parse_ranking_response(
    text: str,
    alternatives: list[SafewayProduct],
) -> list[SubstitutionOption] | None:
    """Parse Claude's substitution ranking response.

    Args:
        text: Raw response text.
        alternatives: The products that were ranked.

    Returns:
        Ordered list of SubstitutionOption, or None on parse failure.
    """
    cleaned = extract_json_text(text)
    try:
        data = json.loads(cleaned)
    except json.JSONDecodeError:
        logger.warning("Failed to parse substitution ranking as JSON")
        return None

    if not isinstance(data, list):
        return None

    options: list[SubstitutionOption] = []
    for entry in data:
        option = _parse_single_ranking(entry, alternatives)
        if option is not None:
            options.append(option)

    return options if options else None


def _parse_single_ranking(
    entry: dict[str, Any],
    alternatives: list[SafewayProduct],
) -> SubstitutionOption | None:
    """Parse a single ranking entry from Claude's response.

    Args:
        entry: A dict from Claude's JSON array.
        alternatives: Products that were ranked.

    Returns:
        A SubstitutionOption, or None if invalid.
    """
    if not isinstance(entry, dict):
        return None

    index = entry.get("index")
    if not isinstance(index, int) or index < 0 or index >= len(alternatives):
        return None

    suitability_str = entry.get("suitability", "acceptable")
    try:
        suitability = SubstitutionSuitability(suitability_str)
    except ValueError:
        suitability = SubstitutionSuitability.ACCEPTABLE

    return SubstitutionOption(
        product=alternatives[index],
        suitability=suitability,
        form_warning=entry.get("form_warning"),
        reasoning=str(entry.get("reasoning", "No reasoning provided")),
    )


def _fallback_ranking(
    alternatives: list[SafewayProduct],
) -> list[SubstitutionOption]:
    """Rank alternatives by price (cheapest first) without Claude.

    Args:
        alternatives: Products to rank.

    Returns:
        SubstitutionOption list sorted by price.
    """
    sorted_alts = sorted(alternatives, key=lambda p: p.price)
    return [
        SubstitutionOption(
            product=p,
            suitability=SubstitutionSuitability.ACCEPTABLE,
            reasoning="Ranked by price (Claude unavailable)",
        )
        for p in sorted_alts
    ]
