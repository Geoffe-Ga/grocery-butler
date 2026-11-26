"""Convention-discovered Python hook for migration 024.

Opens a replenishment need for every item whose inventory already says it
is low or out (ADR 0008). Needs are otherwise opened only when a new
observation arrives, so a database upgraded from before needs existed
kept its low and out items but lost them from every plan until someone
observed each one again. The logic lives in
:func:`grocery_butler.replenishment.backfill_needs`.
"""

from __future__ import annotations

from grocery_butler.replenishment import backfill_needs as migrate

__all__ = ["migrate"]
