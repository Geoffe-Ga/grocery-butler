"""Shared utilities for Claude API response parsing and brand filtering.

Functions used across multiple modules that interact with Claude's API
responses or apply brand-preference filtering to product lists.
"""

from __future__ import annotations

from grocery_butler.models import (
    BrandPreference,
    BrandPreferenceType,
    SafewayProduct,
)


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
