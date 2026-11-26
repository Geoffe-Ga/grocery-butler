"""Convention-discovered Python hook for migration 010.

Links every existing recipe ingredient, pantry staple and inventory row to
a canonical household item (ADR 0006). The logic lives in
:func:`grocery_butler.household_items.backfill_item_links`.
"""

from __future__ import annotations

from grocery_butler.household_items import backfill_item_links as migrate

__all__ = ["migrate"]
