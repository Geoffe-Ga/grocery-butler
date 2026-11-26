"""Convention-discovered Python hook for migration 011.

Imports each existing ``household_inventory`` row into the observation
ledger as one ``system`` observation (ADR 0007). The logic lives in
:func:`grocery_butler.inventory_ledger.import_inventory_rows`.
"""

from __future__ import annotations

from grocery_butler.inventory_ledger import import_inventory_rows as migrate

__all__ = ["migrate"]
