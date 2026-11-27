"""How a household's soft preferences are carried on a shopping plan line.

A plan line's ``constraints`` mapping holds what the household would like
for that item (ADR 0012). This module owns the keys of that mapping, the
rule that fills it from the stored preferences, and the words for the
preferences that no retailer filter can express. The plan builder, the
list export and the Instacart translation all read it from here, so they
can't disagree about what a line asks for (issue #215).
"""

from __future__ import annotations

from typing import TYPE_CHECKING, Any

from grocery_butler.models import SoftPreference, SoftPreferenceKind

if TYPE_CHECKING:
    from collections.abc import Iterable, Mapping

PREFERRED_BRAND = "preferred_brand"
PREFERRED_SIZE = "preferred_size"
ORGANIC = "organic"
GENERIC = "generic"
AVOID_BRANDS = "avoid_brands"
VERIFIED_UPC = "verified_upc"

#: Lengths of a product code a retailer accepts: UPC-A and GTIN-14.
UPC_LENGTHS = (12, 14)

#: Where a preference set on the item itself is stored. It beats the rest.
ITEM_SOURCE = "item"


def _first_choice(
    preferences: Iterable[SoftPreference], kind: SoftPreferenceKind
) -> str | None:
    """Pick the one value to use for a kind that takes a single value.

    Args:
        preferences: Every soft preference that applies to the item.
        kind: ``brand`` or ``size``.

    Returns:
        The item's own value if it has one, else the first by name, else
        None.
    """
    wanted = [p for p in preferences if p.kind is kind and p.value]
    own = [p.value for p in wanted if p.source == ITEM_SOURCE]
    candidates = own or sorted(p.value for p in wanted)
    return candidates[0] if candidates else None


def line_preferences(
    preferences: Iterable[SoftPreference], verified_upc: str | None = None
) -> dict[str, Any]:
    """Turn an item's soft preferences into a plan line's ``constraints``.

    Args:
        preferences: Every soft preference that applies to the item.
        verified_upc: The product code a member confirmed for the item on a
            receipt, if there is one (ADR 0023).

    Returns:
        A mapping with a key only for what is wanted: a preferred brand, a
        preferred size, ``organic``, ``generic``, the brands to avoid
        (sorted, without repeats) and the verified product code. The same
        inputs always give the same mapping, so a plan's content hash is
        stable.
    """
    known = list(preferences)
    kinds = {p.kind for p in known}
    avoided = sorted(
        {p.value for p in known if p.kind is SoftPreferenceKind.AVOID_BRAND and p.value}
    )
    found: dict[str, Any] = {
        PREFERRED_BRAND: _first_choice(known, SoftPreferenceKind.BRAND),
        PREFERRED_SIZE: _first_choice(known, SoftPreferenceKind.SIZE),
        ORGANIC: SoftPreferenceKind.ORGANIC in kinds,
        GENERIC: SoftPreferenceKind.GENERIC in kinds,
        AVOID_BRANDS: avoided,
        VERIFIED_UPC: verified_upc,
    }
    return {key: value for key, value in found.items() if value}


def unfiltered_phrases(constraints: Mapping[str, Any]) -> list[str]:
    """Word the preferences on a line that no retailer filter can express.

    A store brand and a brand to avoid can't be sent as a filter. They are
    said in words instead, so a person can apply them while shopping.

    Args:
        constraints: A plan line's ``constraints``.

    Returns:
        A short phrase for each such preference; empty when there are none.
    """
    phrases = ["prefer the store brand"] if constraints.get(GENERIC) is True else []
    avoided = [str(brand) for brand in constraints.get(AVOID_BRANDS) or []]
    if avoided:
        phrases.append(f"avoid {', '.join(avoided)}")
    return phrases


def is_product_code(value: str) -> bool:
    """Say whether text is a product code that can be sent as it stands.

    Args:
        value: The candidate code.

    Returns:
        True for exactly 12 or 14 ASCII digits.
    """
    return value.isascii() and value.isdigit() and len(value) in UPC_LENGTHS
