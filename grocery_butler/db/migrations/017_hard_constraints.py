"""Convention-discovered Python hook for migration 017.

Copies the free-text dietary preference into one household dietary rule
(ADR 0012). The logic lives in
:func:`grocery_butler.constraints.import_dietary_preference`.
"""

from __future__ import annotations

from grocery_butler.constraints import import_dietary_preference as migrate

__all__ = ["migrate"]
