"""Convention-discovered Python hook for migration 003.

This module is the Python hook for migration ``003_normalize_unit_enum``.
It is discovered by ``grocery_butler.db.migrate._discover_hook`` purely by
file-naming convention (``NNN_name.py`` alongside the corresponding
``NNN_name.sql`` file) and must expose a ``migrate(db_path: str) -> None``
callable.

The actual transform logic lives in
:mod:`grocery_butler.db.migrate_unit_enum`, which remains the single
source of truth (it is also runnable standalone via its own CLI). This
module re-exports its ``migrate`` function unchanged so the migration
runner and the standalone script share one implementation.
"""

from __future__ import annotations

from grocery_butler.db.migrate_unit_enum import migrate

__all__ = ["migrate"]
