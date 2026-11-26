"""Named household members (ADR 0002).

A member is a person in the household. Members have no login: a member key
is attribution, recorded as the actor on every API v2 mutation, and never a
security boundary.
"""

from __future__ import annotations

import datetime as dt
import re
from contextlib import closing
from typing import TYPE_CHECKING, Any

from pydantic import BaseModel

from grocery_butler.db import get_connection
from grocery_butler.db.adapter import IntegrityError
from grocery_butler.inventory_ledger import utc_now

if TYPE_CHECKING:
    from collections.abc import Callable

_KEY_RE = re.compile(r"^[a-z0-9][a-z0-9_-]{0,31}$")

_SELECT_MEMBER = (
    "SELECT id, member_key, display_name, active, created_at, created_by"
    " FROM household_members WHERE member_key = ?"
)
_SELECT_ALL_MEMBERS = (
    "SELECT id, member_key, display_name, active, created_at, created_by"
    " FROM household_members ORDER BY member_key"
)


class Member(BaseModel):
    """One person in the household."""

    id: int
    key: str
    display_name: str
    active: bool
    created_at: dt.datetime
    created_by: str


class InvalidMemberKeyError(ValueError):
    """Raised when a member key is not a short lowercase identifier."""

    def __init__(self, key: str) -> None:
        """Say what a valid key looks like.

        Args:
            key: The rejected key.
        """
        super().__init__(
            f'"{key}" is not a valid member key. Use 1 to 32 lowercase letters, '
            "digits, hyphens or underscores, starting with a letter or digit."
        )
        self.key = key


class MemberExistsError(ValueError):
    """Raised when adding a member whose key is already taken."""

    def __init__(self, key: str) -> None:
        """Name the key that is taken.

        Args:
            key: The member key that already exists.
        """
        super().__init__(f'A household member with the key "{key}" already exists.')
        self.key = key


class MemberNotFoundError(LookupError):
    """Raised when a member key does not exist."""

    def __init__(self, key: str) -> None:
        """Name the missing member.

        Args:
            key: The key that was looked up.
        """
        super().__init__(f'No household member has the key "{key}".')
        self.key = key


def normalize_member_key(raw: str) -> str:
    """Normalize a member key and check its shape.

    Args:
        raw: A member key in any case, possibly padded with spaces.

    Returns:
        The lowercase, trimmed key.

    Raises:
        InvalidMemberKeyError: If the key is not a valid identifier.
    """
    key = raw.strip().lower()
    if not _KEY_RE.match(key):
        raise InvalidMemberKeyError(raw)
    return key


def _row_to_member(row: Any) -> Member:
    """Build a Member from a database row.

    Args:
        row: A row from one of the member SELECT statements.

    Returns:
        The member.
    """
    return Member(
        id=row["id"],
        key=row["member_key"],
        display_name=row["display_name"],
        active=bool(row["active"]),
        created_at=dt.datetime.fromisoformat(row["created_at"]),
        created_by=row["created_by"],
    )


class MemberStore:
    """Read and change the household's members.

    Opens a connection per operation. The schema must already be migrated.

    Args:
        db_path: SQLite file path or PostgreSQL URL.
        clock: Returns the current UTC time. Tests pass a fixed clock.
    """

    def __init__(
        self, db_path: str, clock: Callable[[], dt.datetime] = utc_now
    ) -> None:
        """Initialize the store.

        Args:
            db_path: SQLite file path or PostgreSQL URL.
            clock: Returns the current UTC time.
        """
        self._db_path = db_path
        self._clock = clock

    def get(self, key: str) -> Member | None:
        """Return the member with this key, active or not.

        Args:
            key: A member key in any case.

        Returns:
            The member, or None when the key is unknown or malformed.
        """
        try:
            normalized = normalize_member_key(key)
        except InvalidMemberKeyError:
            return None
        with closing(get_connection(self._db_path)) as conn:
            row = conn.execute(_SELECT_MEMBER, (normalized,)).fetchone()
        return _row_to_member(row) if row else None

    def list_members(self, *, include_inactive: bool = False) -> list[Member]:
        """List members, sorted by key.

        Args:
            include_inactive: Also return deactivated members.

        Returns:
            The matching members.
        """
        with closing(get_connection(self._db_path)) as conn:
            rows = conn.execute(_SELECT_ALL_MEMBERS).fetchall()
        members = [_row_to_member(row) for row in rows]
        return [member for member in members if include_inactive or member.active]

    def add(self, key: str, display_name: str, *, created_by: str) -> Member:
        """Add a household member.

        Args:
            key: The member's key, e.g. ``geoff``.
            display_name: The name shown to people.
            created_by: The actor adding the member.

        Returns:
            The new member.

        Raises:
            InvalidMemberKeyError: If the key is not a valid identifier.
            MemberExistsError: If the key is already taken.
            ValueError: If the display name is blank.
        """
        normalized = normalize_member_key(key)
        name = display_name.strip()
        if not name:
            msg = "A household member needs a display name."
            raise ValueError(msg)
        with closing(get_connection(self._db_path)) as conn:
            try:
                conn.execute(
                    "INSERT INTO household_members"
                    " (member_key, display_name, created_at, created_by)"
                    " VALUES (?, ?, ?, ?)",
                    (normalized, name, self._clock().isoformat(), created_by),
                )
                conn.commit()
            except IntegrityError as err:
                raise MemberExistsError(normalized) from err
        member = self.get(normalized)
        if member is None:  # pragma: no cover - just inserted
            raise MemberNotFoundError(normalized)
        return member

    def set_active(self, key: str, *, active: bool) -> Member:
        """Deactivate or reactivate a member.

        A deactivated member keeps their history but can no longer be named
        as the actor of a new change.

        Args:
            key: The member's key.
            active: The new state.

        Returns:
            The member after the change.

        Raises:
            MemberNotFoundError: If the key is unknown.
        """
        member = self.get(key)
        if member is None:
            raise MemberNotFoundError(key)
        with closing(get_connection(self._db_path)) as conn:
            conn.execute(
                "UPDATE household_members SET active = ? WHERE member_key = ?",
                (active, member.key),
            )
            conn.commit()
        return member.model_copy(update={"active": active})
