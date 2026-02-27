"""Shared utilities for Claude API response parsing and common helpers.

Functions used across multiple modules: Claude response parsing,
brand-preference filtering, Anthropic client creation, and
shopping-list item construction.
"""

from __future__ import annotations

import logging

from grocery_butler.models import (
    BrandPreference,
    BrandPreferenceType,
    IngredientCategory,
    SafewayProduct,
    ShoppingListItem,
    Unit,
)

logger = logging.getLogger(__name__)


def extract_json_text(raw: str) -> str:
    """Extract JSON from a Claude response, stripping markdown fences.

    Args:
        raw: Raw text from Claude's response.

    Returns:
        Cleaned string ready for JSON parsing.
    """
    text = raw.strip()
    if text.startswith("```"):
        first_newline = text.index("\n")
        text = text[first_newline + 1 :]
    if text.endswith("```"):
        text = text[: -len("```")]
    return text.strip()


def filter_avoided_brands(
    products: list[SafewayProduct],
    brand_prefs: list[BrandPreference],
) -> list[SafewayProduct]:
    """Remove products whose name contains an avoided brand.

    Args:
        products: Candidate products.
        brand_prefs: Brand preferences to check.

    Returns:
        Products not matching any avoided brand.
    """
    avoided = {
        pref.brand.lower()
        for pref in brand_prefs
        if pref.preference_type == BrandPreferenceType.AVOID
    }
    if not avoided:
        return products
    return [
        p for p in products if not any(brand in p.name.lower() for brand in avoided)
    ]


def make_anthropic_client(api_key: str) -> object | None:
    """Create an Anthropic client from the given API key.

    Args:
        api_key: Anthropic API key string.

    Returns:
        Anthropic client instance or None on import failure.
    """
    try:
        import anthropic

        return anthropic.Anthropic(api_key=api_key)
    except Exception:
        logger.warning("Anthropic client unavailable; Claude features disabled")
        return None


def items_from_string(items_str: str) -> list[ShoppingListItem]:
    """Convert comma-separated item names to ShoppingListItem list.

    Args:
        items_str: Comma-separated ingredient names.

    Returns:
        List of ShoppingListItem with defaults.
    """
    names = [n.strip() for n in items_str.split(",") if n.strip()]
    return [
        ShoppingListItem(
            ingredient=name.lower(),
            quantity=1.0,
            unit=Unit.EACH,
            category=IngredientCategory.OTHER,
            search_term=name.lower(),
            from_meals=["manual"],
        )
        for name in names
    ]
