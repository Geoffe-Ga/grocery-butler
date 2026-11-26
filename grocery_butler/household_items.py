"""Canonical household items: identity, aliases, and reversible merges.

Implements ADR 0006. A household item is one thing the household keeps,
with a stable id. Every name it answers to is an alias; text resolves to at
most one item. Merging sets a pointer and rewrites nothing, so unmerging
restores the earlier state exactly. Every identity change is appended to
``household_item_events`` with the actor who made it.
"""

from __future__ import annotations

import json
import re
from contextlib import closing
from typing import TYPE_CHECKING, Any

from grocery_butler.db import get_connection
from grocery_butler.db.adapter import IntegrityError
from grocery_butler.models import (
    HouseholdItem,
    HouseholdItemUpdate,
    IngredientCategory,
    ItemEvent,
    ItemEventType,
    StaplePolicy,
    Unit,
    coerce_category_optional,
)

if TYPE_CHECKING:
    from grocery_butler.db.adapter import DatabaseConnection

_WHITESPACE_RE = re.compile(r"\s+")

# These two select every column, not a named list, on purpose. Migration hooks
# for versions before 020 read items through this store while the newer
# columns don't exist yet (a fresh install, or a backup being restored), so
# the reader below treats those columns as optional.
_SELECT_ITEM_BY_ID = "SELECT * FROM household_items WHERE id = ?"
_SELECT_ALL_ITEMS = "SELECT * FROM household_items ORDER BY name"

# Statements for the tables that link to an item. Written out, not built
# from table names, so no SQL is ever assembled from a variable.
_UNLINKED_ROWS = (
    (
        "SELECT id, ingredient, display_name, category"
        " FROM household_inventory WHERE item_id IS NULL",
        "UPDATE household_inventory SET item_id = ? WHERE id = ?",
    ),
    (
        "SELECT id, ingredient, display_name, category"
        " FROM pantry_staples WHERE item_id IS NULL",
        "UPDATE pantry_staples SET item_id = ? WHERE id = ?",
    ),
    (
        "SELECT id, ingredient, NULL AS display_name, category"
        " FROM recipe_ingredients WHERE item_id IS NULL",
        "UPDATE recipe_ingredients SET item_id = ? WHERE id = ?",
    ),
)

MIGRATION_ACTOR = "migration"
# Recorded when an item is created as a side effect of saving a recipe, a
# staple or an inventory row. Those paths don't know the household member
# yet; API v2 (#136) threads the real actor through.
SYSTEM_ACTOR = "system"


class ItemNotFoundError(LookupError):
    """Raised when a household item id does not exist."""

    def __init__(self, item_id: int) -> None:
        """Name the missing item.

        Args:
            item_id: The id that was looked up.
        """
        super().__init__(f"No household item has id {item_id}.")
        self.item_id = item_id


class TargetIncompleteError(ValueError):
    """Raised when a target amount would be left without half of itself."""

    def __init__(self) -> None:
        """Use the one message this error carries."""
        super().__init__(
            "A target amount needs both a quantity and a unit. Set both, or clear both."
        )


class AliasConflictError(ValueError):
    """Raised when an alias already belongs to a different item."""

    def __init__(self, alias: str, owner_id: int) -> None:
        """Say which item owns the alias.

        Args:
            alias: The normalized alias that was requested.
            owner_id: Id of the item that already has it.
        """
        super().__init__(
            f'"{alias}" already names household item {owner_id}. '
            "Merge the two items if they are the same thing."
        )
        self.alias = alias
        self.owner_id = owner_id


class ItemWriteError(RuntimeError):
    """Raised when the database breaks a guarantee an item write relies on."""

    def __init__(self, name: str) -> None:
        """Name the item that could not be written or read back.

        Args:
            name: The normalized item name involved.
        """
        super().__init__(f'Household item "{name}" could not be saved.')


class MergeError(ValueError):
    """Raised when a merge or unmerge would break the identity rules."""


def normalize_item_name(text: str) -> str:
    """Normalize text into an item name or alias key.

    Lowercases, trims and collapses whitespace, and nothing more. It never
    folds plurals or drops words, so it can't join two distinct products.

    Args:
        text: A raw item name.

    Returns:
        The normalized key (empty if the text was blank).
    """
    return _WHITESPACE_RE.sub(" ", text).strip().lower()


def _optional(row: Any, column: str) -> Any:
    """Read a column that an older schema may not have yet.

    Args:
        row: A ``household_items`` row.
        column: A column added after migration 010.

    Returns:
        The value, or None when the schema predates the column.
    """
    return dict(row).get(column)


def _row_to_item(row: Any) -> HouseholdItem:
    """Build a HouseholdItem from a database row.

    Args:
        row: A row from one of the item SELECT statements.

    Returns:
        The item.
    """
    policy = row["staple_policy"]
    unit = _optional(row, "target_unit")
    return HouseholdItem(
        id=row["id"],
        name=row["name"],
        display_name=row["display_name"],
        category=coerce_category_optional(row["category"]),
        preferred_unit=row["preferred_unit"],
        staple_policy=StaplePolicy(policy) if policy else None,
        preferred_brand=row["preferred_brand"],
        preferred_size=row["preferred_size"],
        target_quantity=_optional(row, "target_quantity"),
        target_unit=Unit(unit) if unit else None,
        cadence_days=_optional(row, "cadence_days"),
        active=bool(row["active"]),
        merged_into_id=row["merged_into_id"],
    )


def _stored_value(value: object) -> object:
    """Convert an update value into what the database stores.

    Args:
        value: A field value from a ``HouseholdItemUpdate``.

    Returns:
        The enum's string value for enums, otherwise the value unchanged.
    """
    if isinstance(value, (IngredientCategory, StaplePolicy, Unit)):
        return value.value
    return value


class HouseholdItemStore:
    """Read and change canonical household items.

    Opens a connection per operation, like the other stores. The schema
    must already be migrated.

    Args:
        db_path: SQLite file path or PostgreSQL URL.
    """

    def __init__(self, db_path: str) -> None:
        """Initialize the store.

        Args:
            db_path: SQLite file path or PostgreSQL URL.
        """
        self._db_path = db_path

    def _connect(self) -> DatabaseConnection:
        """Open a new database connection.

        Returns:
            A connection to this store's database.
        """
        return get_connection(self._db_path)

    # -- reads ---------------------------------------------------------

    def get(self, item_id: int) -> HouseholdItem | None:
        """Return the item with this id, merged or not.

        Args:
            item_id: The item's id.

        Returns:
            The item, or None if there is none.
        """
        with closing(self._connect()) as conn:
            return self._get(conn, item_id)

    def canonical(self, item_id: int) -> HouseholdItem:
        """Return the item readers should use for this id.

        Follows the merge pointer, so the id of a merged item returns the
        item it was merged into.

        Args:
            item_id: Any item id, merged or not.

        Returns:
            The canonical item.

        Raises:
            ItemNotFoundError: If no item has this id.
        """
        with closing(self._connect()) as conn:
            return self._canonical(conn, self._require(conn, item_id))

    def resolve(self, text: str) -> HouseholdItem | None:
        """Find the canonical item a piece of text names. Never creates one.

        Args:
            text: An item name or alias in any case or spacing.

        Returns:
            The canonical item, or None when no alias matches.
        """
        with closing(self._connect()) as conn:
            return self._resolve(conn, normalize_item_name(text))

    def list_items(self, *, include_inactive: bool = False) -> list[HouseholdItem]:
        """List items, sorted by name.

        Args:
            include_inactive: Also return merged (inactive) items.

        Returns:
            The matching items.
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(_SELECT_ALL_ITEMS).fetchall()
        items = [_row_to_item(row) for row in rows]
        return [item for item in items if include_inactive or item.active]

    def aliases(self, item_id: int) -> list[str]:
        """Return every alias of an item, its own name included, sorted.

        Args:
            item_id: The item's id.

        Returns:
            The item's aliases.
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT alias FROM household_item_aliases"
                " WHERE item_id = ? ORDER BY alias",
                (item_id,),
            ).fetchall()
        return [row["alias"] for row in rows]

    def history(self, item_id: int) -> list[ItemEvent]:
        """Return an item's audit trail, oldest first.

        Args:
            item_id: The item's id.

        Returns:
            Every recorded event for the item.
        """
        with closing(self._connect()) as conn:
            rows = conn.execute(
                "SELECT id, item_id, event_type, detail, actor, created_at"
                " FROM household_item_events WHERE item_id = ? ORDER BY id",
                (item_id,),
            ).fetchall()
        return [
            ItemEvent(
                id=row["id"],
                item_id=row["item_id"],
                event_type=ItemEventType(row["event_type"]),
                detail=json.loads(row["detail"]),
                actor=row["actor"],
                created_at=str(row["created_at"]),
            )
            for row in rows
        ]

    # -- writes --------------------------------------------------------

    def resolve_or_create(
        self,
        text: str,
        *,
        actor: str,
        display_name: str | None = None,
        category: IngredientCategory | None = None,
    ) -> HouseholdItem:
        """Return the item this text names, creating it if nothing matches.

        Matching is by alias only. Text that matches no alias always makes a
        new item; it is never guessed onto a similar one.

        Args:
            text: An item name in any case or spacing.
            actor: Who is acting, recorded if an item is created.
            display_name: Display name for a new item. Defaults to the
                title-cased name.
            category: Category for a new item.

        Returns:
            The canonical item.

        Raises:
            ValueError: If the text is blank.
        """
        name = normalize_item_name(text)
        if not name:
            msg = "A household item needs a name."
            raise ValueError(msg)

        with closing(self._connect()) as conn:
            existing = self._resolve(conn, name)
            if existing is not None:
                return existing
            try:
                return self._create(conn, name, display_name, category, actor)
            except IntegrityError:
                # Another writer created it between the lookup and the
                # insert. Look again on a fresh connection: on Postgres this
                # one's transaction is now aborted.
                pass

        with closing(self._connect()) as conn:
            winner = self._resolve(conn, name)
        if winner is None:  # pragma: no cover - the unique alias guarantees a row
            raise ItemWriteError(name)
        return winner

    def add_alias(self, item_id: int, alias: str, *, actor: str) -> None:
        """Give an item another name. Adding one it already has is a no-op.

        Args:
            item_id: The item to name.
            alias: The new name, in any case or spacing.
            actor: Who is acting.

        Raises:
            ItemNotFoundError: If no item has this id.
            AliasConflictError: If the alias belongs to a different item.
            ValueError: If the alias is blank.
        """
        key = normalize_item_name(alias)
        if not key:
            msg = "An alias needs some text."
            raise ValueError(msg)

        with closing(self._connect()) as conn:
            self._require(conn, item_id)
            owner = self._alias_owner(conn, key)
            if owner == item_id:
                return
            if owner is not None:
                raise AliasConflictError(key, owner)
            conn.execute(
                "INSERT INTO household_item_aliases (item_id, alias) VALUES (?, ?)",
                (item_id, key),
            )
            self._record(
                conn, item_id, ItemEventType.ALIAS_ADDED, {"alias": key}, actor
            )
            conn.commit()

    def remove_alias(self, item_id: int, alias: str, *, actor: str) -> None:
        """Take a name away from an item.

        Args:
            item_id: The item that has the alias.
            alias: The alias to remove, in any case or spacing.
            actor: Who is acting.

        Raises:
            ItemNotFoundError: If no item has this id.
            ValueError: If the alias is the item's own name, or is not an
                alias of this item.
        """
        key = normalize_item_name(alias)
        with closing(self._connect()) as conn:
            item = self._require(conn, item_id)
            if key == item.name:
                msg = f"\"{key}\" is the item's own name and can't be removed."
                raise ValueError(msg)
            if self._alias_owner(conn, key) != item_id:
                msg = f'"{key}" is not an alias of household item {item_id}.'
                raise ValueError(msg)
            conn.execute("DELETE FROM household_item_aliases WHERE alias = ?", (key,))
            self._record(
                conn, item_id, ItemEventType.ALIAS_REMOVED, {"alias": key}, actor
            )
            conn.commit()

    def update(
        self, item_id: int, changes: HouseholdItemUpdate, *, actor: str
    ) -> HouseholdItem:
        """Change an item's display name, category, policy or preferences.

        Only fields set on ``changes`` are applied; a field set to None is
        cleared. An update with no fields does nothing.

        Args:
            item_id: The item to change.
            changes: The fields to change.
            actor: Who is acting.

        Returns:
            The item after the change.

        Raises:
            ItemNotFoundError: If no item has this id.
            TargetIncompleteError: If the change would leave a target
                quantity without a unit, or a unit without a quantity.
        """
        fields = {
            name: _stored_value(getattr(changes, name))
            for name in changes.model_fields_set
        }
        with closing(self._connect()) as conn:
            item = self._require(conn, item_id)
            if not fields:
                return item
            _require_whole_target(item, fields)
            for column, statement in _UPDATE_STATEMENTS.items():
                if column in fields:
                    conn.execute(statement, (fields[column], item_id))
            self._record(conn, item_id, ItemEventType.UPDATED, fields, actor)
            conn.commit()
            return self._require(conn, item_id)

    def merge(self, source_id: int, target_id: int, *, actor: str) -> None:
        """Merge one item into another, reversibly.

        Sets a pointer on the source and deactivates it. Nothing that
        references the source is rewritten; readers follow the pointer.

        Args:
            source_id: The item that goes away.
            target_id: The item that survives. Must be canonical.
            actor: Who is acting.

        Raises:
            ItemNotFoundError: If either id does not exist.
            MergeError: If the items are the same, either is already merged,
                or the source has itself absorbed other items.
        """
        with closing(self._connect()) as conn:
            source = self._require(conn, source_id)
            target = self._require(conn, target_id)
            self._check_mergeable(conn, source, target)
            conn.execute(
                "UPDATE household_items SET merged_into_id = ?, active = ?,"
                " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (target_id, False, source_id),
            )
            self._record(
                conn, source_id, ItemEventType.MERGED, {"into": target_id}, actor
            )
            self._record(
                conn, target_id, ItemEventType.MERGED, {"absorbed": source_id}, actor
            )
            conn.commit()

    def unmerge(self, source_id: int, *, actor: str) -> None:
        """Undo a merge, restoring the item as it was.

        Args:
            source_id: The merged item to restore.
            actor: Who is acting.

        Raises:
            ItemNotFoundError: If no item has this id.
            MergeError: If the item is not merged.
        """
        with closing(self._connect()) as conn:
            source = self._require(conn, source_id)
            if source.merged_into_id is None:
                msg = f"Household item {source_id} is not merged into anything."
                raise MergeError(msg)
            conn.execute(
                "UPDATE household_items SET merged_into_id = NULL, active = ?,"
                " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
                (True, source_id),
            )
            self._record(
                conn,
                source_id,
                ItemEventType.UNMERGED,
                {"from": source.merged_into_id},
                actor,
            )
            self._record(
                conn,
                source.merged_into_id,
                ItemEventType.UNMERGED,
                {"released": source_id},
                actor,
            )
            conn.commit()

    # -- internals -----------------------------------------------------

    def _get(self, conn: DatabaseConnection, item_id: int) -> HouseholdItem | None:
        """Fetch one item by id on an open connection.

        Args:
            conn: Open connection.
            item_id: The item's id.

        Returns:
            The item, or None.
        """
        row = conn.execute(_SELECT_ITEM_BY_ID, (item_id,)).fetchone()
        return _row_to_item(row) if row else None

    def _require(self, conn: DatabaseConnection, item_id: int) -> HouseholdItem:
        """Fetch one item by id, or raise.

        Args:
            conn: Open connection.
            item_id: The item's id.

        Returns:
            The item.

        Raises:
            ItemNotFoundError: If no item has this id.
        """
        item = self._get(conn, item_id)
        if item is None:
            raise ItemNotFoundError(item_id)
        return item

    def _canonical(
        self, conn: DatabaseConnection, item: HouseholdItem
    ) -> HouseholdItem:
        """Follow an item's merge pointer to the item readers should use.

        Merge targets must be canonical, so the pointer is one hop deep.

        Args:
            conn: Open connection.
            item: Any item.

        Returns:
            The item itself if it is not merged, else its target.
        """
        if item.merged_into_id is None:
            return item
        return self._require(conn, item.merged_into_id)

    def _alias_owner(self, conn: DatabaseConnection, key: str) -> int | None:
        """Return the id of the item that has this alias.

        Args:
            conn: Open connection.
            key: A normalized alias.

        Returns:
            The owning item's id, or None when nobody has the alias.
        """
        row = conn.execute(
            "SELECT item_id FROM household_item_aliases WHERE alias = ?", (key,)
        ).fetchone()
        return int(row["item_id"]) if row else None

    def _resolve(self, conn: DatabaseConnection, key: str) -> HouseholdItem | None:
        """Resolve a normalized alias to its canonical item.

        Args:
            conn: Open connection.
            key: A normalized alias.

        Returns:
            The canonical item, or None when no alias matches.
        """
        owner = self._alias_owner(conn, key)
        if owner is None:
            return None
        return self._canonical(conn, self._require(conn, owner))

    def _create(
        self,
        conn: DatabaseConnection,
        name: str,
        display_name: str | None,
        category: IngredientCategory | None,
        actor: str,
    ) -> HouseholdItem:
        """Insert a new item, its name alias and its created event.

        Args:
            conn: Open connection.
            name: The normalized, unused name.
            display_name: Display name, or None for the title-cased name.
            category: The item's category, if known.
            actor: Who is acting.

        Returns:
            The new item.
        """
        cursor = conn.execute(
            "INSERT INTO household_items (name, display_name, category)"
            " VALUES (?, ?, ?)",
            (name, display_name or name.title(), category.value if category else None),
        )
        item_id = cursor.lastrowid
        if item_id is None:  # pragma: no cover - both backends return the id
            raise ItemWriteError(name)
        conn.execute(
            "INSERT INTO household_item_aliases (item_id, alias) VALUES (?, ?)",
            (item_id, name),
        )
        self._record(conn, item_id, ItemEventType.CREATED, {"name": name}, actor)
        conn.commit()
        return self._require(conn, item_id)

    def _check_mergeable(
        self, conn: DatabaseConnection, source: HouseholdItem, target: HouseholdItem
    ) -> None:
        """Refuse a merge that would hide an item or start a chain.

        Args:
            conn: Open connection.
            source: The item that would go away.
            target: The item that would survive.

        Raises:
            MergeError: If the merge breaks an identity rule.
        """
        if source.id == target.id:
            msg = "A household item can't be merged into itself."
            raise MergeError(msg)
        for item in (source, target):
            if item.merged_into_id is not None:
                msg = (
                    f"Household item {item.id} is already merged into item "
                    f"{item.merged_into_id}. Unmerge it first."
                )
                raise MergeError(msg)
        absorbed = conn.execute(
            "SELECT id FROM household_items WHERE merged_into_id = ?",
            (source.id,),
        ).fetchall()
        if absorbed:
            msg = (
                f"Household item {source.id} has absorbed other items. "
                "Unmerge those first, or merge the other way round."
            )
            raise MergeError(msg)

    def _record(
        self,
        conn: DatabaseConnection,
        item_id: int,
        event_type: ItemEventType,
        detail: dict[str, Any],
        actor: str,
    ) -> None:
        """Append an event to an item's audit trail (not committed here).

        Args:
            conn: Open connection.
            item_id: The item the event is about.
            event_type: What happened.
            detail: JSON-serializable facts about the change.
            actor: Who made it.
        """
        conn.execute(
            "INSERT INTO household_item_events (item_id, event_type, detail, actor)"
            " VALUES (?, ?, ?, ?)",
            (item_id, event_type.value, json.dumps(detail, sort_keys=True), actor),
        )


def _require_whole_target(item: HouseholdItem, fields: dict[str, Any]) -> None:
    """Refuse a change that leaves half a target amount.

    Args:
        item: The item before the change.
        fields: The stored values being applied, by column.

    Raises:
        TargetIncompleteError: If, after the change, exactly one of the
            target quantity and target unit would be set.
    """
    # ``fields`` holds stored values (an enum is already its text), so the
    # item's own unit is read as text too before the two are compared.
    quantity = fields.get("target_quantity", item.target_quantity)
    unit = fields.get(
        "target_unit", item.target_unit.value if item.target_unit else None
    )
    if (quantity is None) != (unit is None):
        raise TargetIncompleteError


_UPDATE_STATEMENTS: dict[str, str] = {
    "display_name": "UPDATE household_items SET display_name = ?,"
    " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
    "category": "UPDATE household_items SET category = ?,"
    " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
    "preferred_unit": "UPDATE household_items SET preferred_unit = ?,"
    " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
    "staple_policy": "UPDATE household_items SET staple_policy = ?,"
    " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
    "preferred_brand": "UPDATE household_items SET preferred_brand = ?,"
    " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
    "preferred_size": "UPDATE household_items SET preferred_size = ?,"
    " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
    "target_quantity": "UPDATE household_items SET target_quantity = ?,"
    " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
    "target_unit": "UPDATE household_items SET target_unit = ?,"
    " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
    "cadence_days": "UPDATE household_items SET cadence_days = ?,"
    " updated_at = CURRENT_TIMESTAMP WHERE id = ?",
}


def backfill_item_links(db_path: str) -> None:
    """Link every unlinked row of the older tables to a household item.

    Migration 010's hook. Finds or creates the item each row's name refers
    to and stores its id on the row. Rows already linked are left alone, so
    running it again changes nothing.

    Args:
        db_path: SQLite file path or PostgreSQL URL.
    """
    store = HouseholdItemStore(db_path)
    for select_sql, update_sql in _UNLINKED_ROWS:
        with closing(get_connection(db_path)) as conn:
            rows = conn.execute(select_sql).fetchall()
        links = [
            (
                store.resolve_or_create(
                    row["ingredient"],
                    actor=MIGRATION_ACTOR,
                    display_name=row["display_name"],
                    category=coerce_category_optional(row["category"]),
                ).id,
                row["id"],
            )
            for row in rows
            if normalize_item_name(row["ingredient"])
        ]
        with closing(get_connection(db_path)) as conn:
            for item_id, row_id in links:
                conn.execute(update_sql, (item_id, row_id))
            conn.commit()
