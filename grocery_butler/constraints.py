"""Hard constraints and soft preferences, kept apart on purpose.

Implements ADR 0012. A hard constraint is something that must hold for what
the household buys: an allergen exclusion, a dietary rule, or "don't
substitute this item". A soft preference is something the household would
like: a brand, a size, organic, generic. They are different types in
different tables, and nothing here converts one into the other, so a
fulfillment adapter can never be handed an allergen as a filter it is free
to ignore.
"""

from __future__ import annotations

import datetime as dt
from contextlib import closing
from typing import TYPE_CHECKING, Any

from grocery_butler.db import get_connection
from grocery_butler.household_items import (
    MIGRATION_ACTOR,
    HouseholdItemStore,
    normalize_item_name,
)
from grocery_butler.inventory_ledger import utc_now
from grocery_butler.models import (
    HardConstraint,
    HardConstraintKind,
    HouseholdItem,
    SoftPreference,
    SoftPreferenceKind,
)

if TYPE_CHECKING:
    from collections.abc import Callable

    from grocery_butler.db.adapter import DatabaseConnection

# The nine major food allergens, offered to clients as suggestions. A
# household may exclude anything; this is guidance, not a closed list.
COMMON_ALLERGENS = (
    "milk",
    "egg",
    "fish",
    "shellfish",
    "tree nut",
    "peanut",
    "wheat",
    "soy",
    "sesame",
)

MIGRATED_NOTE = (
    "Copied from the old dietary restrictions text; unverified. Enter allergens "
    "as allergen exclusions so they can be enforced."
)

# Dietary text that means "there are none".
_NO_RESTRICTIONS = frozenset({"", "none", "n/a", "na", "no"})

_VALUE_REQUIRED = frozenset(
    {HardConstraintKind.ALLERGEN_EXCLUSION, HardConstraintKind.DIETARY_RULE}
)

_SELECT_HARD = (
    "SELECT id, kind, value, item_id, note, active, created_by, created_at,"
    " deactivated_by, deactivated_at FROM hard_constraints ORDER BY id"
)
_SELECT_HARD_BY_ID = (
    "SELECT id, kind, value, item_id, note, active, created_by, created_at,"
    " deactivated_by, deactivated_at FROM hard_constraints WHERE id = ?"
)
_SELECT_SOFT = (
    "SELECT kind, value, item_id FROM soft_preferences ORDER BY kind, value, id"
)

_BRAND_RULE_KINDS = {
    "preferred": SoftPreferenceKind.BRAND,
    "avoid": SoftPreferenceKind.AVOID_BRAND,
}


class ConstraintNotFoundError(LookupError):
    """Raised when a hard constraint id does not exist."""

    def __init__(self, constraint_id: int) -> None:
        """Name the missing constraint.

        Args:
            constraint_id: The id that was looked up.
        """
        super().__init__(f"No hard constraint has id {constraint_id}.")
        self.constraint_id = constraint_id


def _when(value: str | None) -> dt.datetime | None:
    """Parse a stored ISO 8601 time.

    Args:
        value: The stored text, or None.

    Returns:
        The time, or None.
    """
    return dt.datetime.fromisoformat(value) if value else None


def _row_to_hard(row: Any) -> HardConstraint:
    """Build a HardConstraint from a database row.

    Args:
        row: A row from one of the hard constraint SELECT statements.

    Returns:
        The constraint.
    """
    return HardConstraint(
        id=row["id"],
        kind=HardConstraintKind(row["kind"]),
        value=row["value"],
        item_id=row["item_id"],
        note=row["note"],
        active=bool(row["active"]),
        created_by=row["created_by"],
        created_at=dt.datetime.fromisoformat(row["created_at"]),
        deactivated_by=row["deactivated_by"],
        deactivated_at=_when(row["deactivated_at"]),
    )


class HouseholdRules:
    """Read and change the household's hard constraints and soft preferences.

    Opens a connection per operation. The schema must already be migrated.

    Args:
        db_path: SQLite file path or PostgreSQL URL.
        clock: Returns the current UTC time. Tests pass a fixed clock.
    """

    def __init__(
        self, db_path: str, clock: Callable[[], dt.datetime] = utc_now
    ) -> None:
        """Initialize the service.

        Args:
            db_path: SQLite file path or PostgreSQL URL.
            clock: Returns the current UTC time.
        """
        self._db_path = db_path
        self._clock = clock
        self._items = HouseholdItemStore(db_path)

    # -- hard constraints ----------------------------------------------

    def list_hard(self, *, include_inactive: bool = False) -> list[HardConstraint]:
        """List hard constraints, oldest first.

        Args:
            include_inactive: Also return deactivated constraints.

        Returns:
            The matching constraints.
        """
        with closing(get_connection(self._db_path)) as conn:
            rows = conn.execute(_SELECT_HARD).fetchall()
        constraints = [_row_to_hard(row) for row in rows]
        return [c for c in constraints if include_inactive or c.active]

    def hard_for_item(self, item_id: int) -> list[HardConstraint]:
        """Return every active hard constraint that applies to an item.

        That is each household-wide constraint, plus those set on the item
        or on any item merged into it.

        Args:
            item_id: Any item id; a merged id is followed to its target.

        Returns:
            The constraints, sorted by kind, value and scope.

        Raises:
            ItemNotFoundError: If no item has this id.
        """
        canonical = self._items.canonical(item_id)
        family = self._family_ids(canonical.id)
        applicable = [
            constraint
            for constraint in self.list_hard()
            if constraint.item_id is None or constraint.item_id in family
        ]
        return sorted(applicable, key=lambda c: (c.kind.value, c.value, c.scope))

    def add_hard(
        self,
        kind: HardConstraintKind,
        value: str,
        *,
        actor: str,
        item_id: int | None = None,
        note: str = "",
    ) -> HardConstraint:
        """Add a hard constraint. Adding one that is already active is a no-op.

        Args:
            kind: What must hold.
            value: The allergen or rule, e.g. ``peanut``. Ignored for
                ``do_not_substitute``.
            actor: Who is adding it.
            item_id: The item it is about, or None for the whole household.
            note: Context for the household, e.g. why.

        Returns:
            The active constraint.

        Raises:
            ValueError: If an allergen or dietary rule has no value, or
                ``do_not_substitute`` names no item.
            ItemNotFoundError: If the item does not exist.
        """
        normalized = self._hard_value(kind, value, item_id)
        target = self._items.canonical(item_id).id if item_id is not None else None
        for existing in self.list_hard():
            same = (existing.kind, existing.value, existing.item_id) == (
                kind,
                normalized,
                target,
            )
            if same:
                return existing
        with closing(get_connection(self._db_path)) as conn:
            cursor = conn.execute(
                "INSERT INTO hard_constraints"
                " (kind, value, item_id, note, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?, ?)",
                (
                    kind.value,
                    normalized,
                    target,
                    note,
                    actor,
                    self._clock().isoformat(),
                ),
            )
            conn.commit()
            return self._require_hard(conn, cursor.lastrowid)

    def deactivate_hard(self, constraint_id: int, *, actor: str) -> HardConstraint:
        """Stop a hard constraint applying, keeping its record.

        Args:
            constraint_id: The constraint to deactivate.
            actor: Who is removing it.

        Returns:
            The deactivated constraint.

        Raises:
            ConstraintNotFoundError: If the constraint does not exist.
        """
        with closing(get_connection(self._db_path)) as conn:
            self._require_hard(conn, constraint_id)
            conn.execute(
                "UPDATE hard_constraints SET active = ?, deactivated_by = ?,"
                " deactivated_at = ? WHERE id = ? AND active = ?",
                (False, actor, self._clock().isoformat(), constraint_id, True),
            )
            conn.commit()
            return self._require_hard(conn, constraint_id)

    # -- soft preferences ----------------------------------------------

    def soft_for_household(self) -> list[SoftPreference]:
        """Return the household-wide soft preferences.

        Returns:
            Preferences stored without an item, sorted by kind and value.
        """
        return [p for p in self._stored_soft() if p.item_id is None]

    def soft_for_item(self, item_id: int) -> list[SoftPreference]:
        """Return every soft preference that applies to an item.

        Combines the item's own preferred brand and size, the brand rules
        that match its name or category, and stored preferences for the
        item or the household. Nothing is moved; this only reads.

        Args:
            item_id: Any item id; a merged id is followed to its target.

        Returns:
            The preferences, sorted by kind, value and source.

        Raises:
            ItemNotFoundError: If no item has this id.
        """
        item = self._items.canonical(item_id)
        found = [
            *self._item_preferences(item),
            *self._brand_rules(item),
            *(p for p in self._stored_soft() if p.item_id in (None, item.id)),
        ]
        return sorted(found, key=lambda p: (p.kind.value, p.value, p.source))

    def add_soft(
        self,
        kind: SoftPreferenceKind,
        value: str,
        *,
        actor: str,
        item_id: int | None = None,
    ) -> None:
        """Store a soft preference. Storing the same one twice is a no-op.

        Args:
            kind: What the household would like.
            value: The brand or size, or empty for ``organic``/``generic``.
            actor: Who is adding it.
            item_id: The item it is about, or None for the whole household.

        Raises:
            TypeError: If ``kind`` is not a soft preference kind. A hard
                constraint can never be stored as a preference.
            ItemNotFoundError: If the item does not exist.
        """
        if not isinstance(kind, SoftPreferenceKind):
            msg = (
                f"{kind!r} is not a soft preference. A hard constraint is added "
                "with add_hard and can't be stored as a preference."
            )
            raise TypeError(msg)
        target = self._items.canonical(item_id).id if item_id is not None else None
        text = value.strip()
        with closing(get_connection(self._db_path)) as conn:
            existing = conn.execute(_SELECT_SOFT).fetchall()
            duplicate = any(
                (row["kind"], row["value"], row["item_id"])
                == (kind.value, text, target)
                for row in existing
            )
            if duplicate:
                return
            conn.execute(
                "INSERT INTO soft_preferences"
                " (kind, value, item_id, created_by, created_at)"
                " VALUES (?, ?, ?, ?, ?)",
                (kind.value, text, target, actor, self._clock().isoformat()),
            )
            conn.commit()

    # -- internals -----------------------------------------------------

    @staticmethod
    def _hard_value(kind: HardConstraintKind, value: str, item_id: int | None) -> str:
        """Normalize a hard constraint's value and check it fits its kind.

        Args:
            kind: The constraint kind.
            value: The raw value.
            item_id: The item it is about, if any.

        Returns:
            The lowercased, trimmed value; empty for ``do_not_substitute``.

        Raises:
            ValueError: If the kind needs a value or an item and has none.
        """
        if kind is HardConstraintKind.DO_NOT_SUBSTITUTE:
            if item_id is None:
                msg = "Do-not-substitute is about one item; say which."
                raise ValueError(msg)
            return ""
        normalized = normalize_item_name(value)
        if kind in _VALUE_REQUIRED and not normalized:
            msg = f"A {kind.value.replace('_', ' ')} needs a value, e.g. peanut."
            raise ValueError(msg)
        return normalized

    def _family_ids(self, canonical_id: int) -> set[int]:
        """Return a canonical item's id and the ids merged into it.

        Args:
            canonical_id: A canonical item id.

        Returns:
            Every id whose constraints belong to the canonical item.
        """
        with closing(get_connection(self._db_path)) as conn:
            rows = conn.execute(
                "SELECT id FROM household_items WHERE id = ? OR merged_into_id = ?",
                (canonical_id, canonical_id),
            ).fetchall()
        return {row["id"] for row in rows}

    def _stored_soft(self) -> list[SoftPreference]:
        """Read the preferences table.

        Returns:
            Stored preferences, sorted by kind and value.
        """
        with closing(get_connection(self._db_path)) as conn:
            rows = conn.execute(_SELECT_SOFT).fetchall()
        return [
            SoftPreference(
                kind=SoftPreferenceKind(row["kind"]),
                value=row["value"],
                item_id=row["item_id"],
                source="preference",
            )
            for row in rows
        ]

    @staticmethod
    def _item_preferences(item: HouseholdItem) -> list[SoftPreference]:
        """Read the brand and size stored on an item.

        Args:
            item: The canonical item.

        Returns:
            Its preferred brand and size, where set.
        """
        pairs = (
            (SoftPreferenceKind.BRAND, item.preferred_brand),
            (SoftPreferenceKind.SIZE, item.preferred_size),
        )
        return [
            SoftPreference(kind=kind, value=value, item_id=item.id, source="item")
            for kind, value in pairs
            if value
        ]

    def _brand_rules(self, item: HouseholdItem) -> list[SoftPreference]:
        """Read the brand rules that match an item's name or category.

        Args:
            item: The canonical item.

        Returns:
            Preferred and avoided brands from the older brand rules table.
        """
        names = set(self._items.aliases(item.id))
        category = item.category.value if item.category else None
        with closing(get_connection(self._db_path)) as conn:
            rows = conn.execute(
                "SELECT match_target, match_type, brand, preference_type"
                " FROM brand_preferences ORDER BY id"
            ).fetchall()
        matched = [
            row
            for row in rows
            if (
                row["match_type"] == "ingredient"
                and normalize_item_name(row["match_target"]) in names
            )
            or (row["match_type"] == "category" and row["match_target"] == category)
        ]
        return [
            SoftPreference(
                kind=_BRAND_RULE_KINDS[row["preference_type"]],
                value=row["brand"],
                item_id=item.id,
                source="brand_rule",
            )
            for row in matched
            if row["preference_type"] in _BRAND_RULE_KINDS
        ]

    def _require_hard(
        self, conn: DatabaseConnection, constraint_id: int | None
    ) -> HardConstraint:
        """Fetch one hard constraint on an open connection, or raise.

        Args:
            conn: Open connection.
            constraint_id: The constraint's id.

        Returns:
            The constraint.

        Raises:
            ConstraintNotFoundError: If the constraint does not exist.
        """
        row = conn.execute(_SELECT_HARD_BY_ID, (constraint_id,)).fetchone()
        if row is None:
            raise ConstraintNotFoundError(constraint_id or 0)
        return _row_to_hard(row)


def import_dietary_preference(db_path: str) -> None:
    """Copy the old dietary restrictions text into one dietary rule.

    Migration 017's hook. The text is carried verbatim as a single
    household ``dietary_rule``; nothing is parsed out of it, because a
    guess at an allergen would be worse than none. The original preference
    stays, since the recipe parser still reads it. Running it again changes
    nothing.

    Args:
        db_path: SQLite file path or PostgreSQL URL.
    """
    with closing(get_connection(db_path)) as conn:
        rows = conn.execute(
            "SELECT value FROM preferences WHERE key = ?", ("dietary_restrictions",)
        ).fetchall()
    text = rows[0]["value"] if rows else ""
    if normalize_item_name(text) in _NO_RESTRICTIONS:
        return
    HouseholdRules(db_path).add_hard(
        HardConstraintKind.DIETARY_RULE,
        text,
        actor=MIGRATION_ACTOR,
        note=MIGRATED_NOTE,
    )
