"""Tests for household members and idempotency records (ADR 0002, ADR 0009)."""

from __future__ import annotations

import datetime as dt
import os
from contextlib import closing
from typing import TYPE_CHECKING

import pytest

from grocery_butler.db import get_connection, init_db
from grocery_butler.idempotency import (
    RETENTION,
    ClaimOutcome,
    IdempotencyStore,
    is_valid_key,
    request_hash,
)
from grocery_butler.members import (
    InvalidMemberKeyError,
    MemberExistsError,
    MemberNotFoundError,
    MemberStore,
    normalize_member_key,
)

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

NOW = dt.datetime(2026, 11, 26, 12, 0, tzinfo=dt.UTC)


class FakeClock:
    """A clock tests move by hand."""

    def __init__(self) -> None:
        """Start at NOW."""
        self.now = NOW

    def __call__(self) -> dt.datetime:
        """Return the current fake time."""
        return self.now


@pytest.fixture()
def db_path(tmp_path: Path) -> str:
    """Return a migrated temporary database path."""
    path = str(tmp_path / "members.db")
    init_db(path)
    return path


class TestMemberKeys:
    """A member key is a short lowercase identifier."""

    @pytest.mark.parametrize(
        ("raw", "expected"),
        [("Geoff", "geoff"), ("  jess ", "jess"), ("kid_2", "kid_2"), ("a-b", "a-b")],
    )
    def test_valid_keys_are_normalized(self, raw: str, expected: str) -> None:
        """Case and padding don't matter."""
        assert normalize_member_key(raw) == expected

    @pytest.mark.parametrize(
        "raw", ["", "has space", "-lead", "x" * 33, "émile", "a@b"]
    )
    def test_invalid_keys_are_rejected(self, raw: str) -> None:
        """Anything else is refused with what a valid key looks like."""
        with pytest.raises(InvalidMemberKeyError, match="lowercase letters"):
            normalize_member_key(raw)


class TestMemberStore:
    """Members are added, listed and deactivated; never deleted."""

    def test_add_and_get(self, db_path: str) -> None:
        """A new member is active and records who added them and when."""
        store = MemberStore(db_path, clock=FakeClock())

        member = store.add("Geoff", " Geoff E ", created_by="rubotpaul")

        assert (member.key, member.display_name) == ("geoff", "Geoff E")
        assert member.active is True
        assert member.created_at == NOW
        assert member.created_by == "rubotpaul"
        assert store.get("GEOFF") == member

    def test_duplicate_key_is_refused(self, db_path: str) -> None:
        """One key, one person."""
        store = MemberStore(db_path)
        store.add("geoff", "Geoff", created_by="t")

        with pytest.raises(MemberExistsError, match="geoff"):
            store.add("Geoff", "Someone Else", created_by="t")

    def test_blank_display_name_is_refused(self, db_path: str) -> None:
        """People need a name to be shown by."""
        with pytest.raises(ValueError, match="display name"):
            MemberStore(db_path).add("geoff", "   ", created_by="t")

    def test_unknown_or_malformed_key_gets_none(self, db_path: str) -> None:
        """get never raises."""
        store = MemberStore(db_path)

        assert store.get("nobody") is None
        assert store.get("not a key!") is None

    def test_deactivated_member_is_hidden_by_default_and_kept(
        self, db_path: str
    ) -> None:
        """Deactivation hides a member from the default list only."""
        store = MemberStore(db_path)
        store.add("geoff", "Geoff", created_by="t")
        store.add("sam", "Sam", created_by="t")

        changed = store.set_active("sam", active=False)

        assert changed.active is False
        assert [m.key for m in store.list_members()] == ["geoff"]
        assert [m.key for m in store.list_members(include_inactive=True)] == [
            "geoff",
            "sam",
        ]
        assert store.set_active("sam", active=True).active is True

    def test_set_active_on_unknown_member_raises(self, db_path: str) -> None:
        """You can only change someone who exists."""
        with pytest.raises(MemberNotFoundError, match="nobody"):
            MemberStore(db_path).set_active("nobody", active=False)


class TestIdempotencyKeys:
    """Key shape and request fingerprints."""

    @pytest.mark.parametrize("key", ["12345678", "a" * 128, "order:2026-11-26/retry_1"])
    def test_valid_keys(self, key: str) -> None:
        """8 to 128 printable ASCII characters without spaces."""
        assert is_valid_key(key) is True

    @pytest.mark.parametrize(
        "key", ["1234567", "a" * 129, "has space", "tab\there", "é" * 9]
    )
    def test_invalid_keys(self, key: str) -> None:
        """Too short, too long, spaced, control or non-ASCII keys."""
        assert is_valid_key(key) is False

    def test_hash_changes_with_method_path_and_body(self) -> None:
        """Any difference in the request is a different fingerprint."""
        base = request_hash("POST", "/api/v2/items", b"{}")

        assert request_hash("post", "/api/v2/items", b"{}") == base
        assert request_hash("PATCH", "/api/v2/items", b"{}") != base
        assert request_hash("POST", "/api/v2/item", b"s{}") != base
        assert request_hash("POST", "/api/v2/items", b"{ }") != base


class TestIdempotencyStore:
    """Claim, complete, release, replay."""

    def test_new_then_in_progress_then_replay(self, db_path: str) -> None:
        """A key moves from claimed to completed."""
        store = IdempotencyStore(db_path)

        first = store.claim("key-00000001", "hash-a")
        running = store.claim("key-00000001", "hash-a")
        store.complete("key-00000001", 201, '{"id": 1}')
        replay = store.claim("key-00000001", "hash-a")

        assert first.outcome is ClaimOutcome.NEW
        assert running.outcome is ClaimOutcome.IN_PROGRESS
        assert replay.outcome is ClaimOutcome.REPLAY
        assert (replay.status_code, replay.response_body) == (201, '{"id": 1}')

    def test_different_request_with_the_same_key_is_a_mismatch(
        self, db_path: str
    ) -> None:
        """Whether or not the first request finished."""
        store = IdempotencyStore(db_path)
        store.claim("key-00000001", "hash-a")

        assert store.claim("key-00000001", "hash-b").outcome is ClaimOutcome.MISMATCH
        store.complete("key-00000001", 200, "{}")
        assert store.claim("key-00000001", "hash-b").outcome is ClaimOutcome.MISMATCH

    def test_release_frees_a_failed_request_but_not_a_completed_one(
        self, db_path: str
    ) -> None:
        """Only an unfinished claim can be given up."""
        store = IdempotencyStore(db_path)
        store.claim("key-failed-1", "hash-a")
        store.claim("key-done-001", "hash-a")
        store.complete("key-done-001", 200, "{}")

        store.release("key-failed-1")
        store.release("key-done-001")

        assert store.claim("key-failed-1", "hash-a").outcome is ClaimOutcome.NEW
        assert store.claim("key-done-001", "hash-a").outcome is ClaimOutcome.REPLAY

    def test_records_older_than_the_retention_period_are_pruned(
        self, db_path: str
    ) -> None:
        """A key can be used again once its record has aged out."""
        clock = FakeClock()
        store = IdempotencyStore(db_path, clock=clock)
        store.claim("key-old-0001", "hash-a")
        store.complete("key-old-0001", 200, "{}")

        clock.now += RETENTION - dt.timedelta(seconds=1)
        kept = store.claim("key-old-0001", "hash-a")
        clock.now += dt.timedelta(seconds=2)
        pruned = store.claim("key-old-0001", "hash-a")

        assert kept.outcome is ClaimOutcome.REPLAY
        assert pruned.outcome is ClaimOutcome.NEW


_PG_URL = os.environ.get("TEST_DATABASE_URL", "")


@pytest.mark.skipif(not _PG_URL, reason="TEST_DATABASE_URL not set")
class TestPostgres:
    """Members and idempotency records behave the same on Postgres."""

    @pytest.fixture()
    def clean(self) -> Iterator[None]:
        """Remove this suite's rows before and after."""
        init_db(_PG_URL)
        self._clean()
        yield
        self._clean()

    def _clean(self) -> None:
        """Delete this suite's member and keys."""
        with closing(get_connection(_PG_URL)) as conn:
            conn.execute(
                "DELETE FROM household_members WHERE member_key = ?", ("pgm-geoff",)
            )
            conn.execute(
                "DELETE FROM api_idempotency_keys WHERE idempotency_key LIKE ?",
                ("pgm-key-%",),
            )
            conn.commit()

    @pytest.mark.usefixtures("clean")
    def test_member_and_key_lifecycle(self) -> None:
        """Duplicate members and claimed keys are detected on Postgres."""
        members = MemberStore(_PG_URL)
        keys = IdempotencyStore(_PG_URL)

        members.add("pgm-geoff", "Geoff", created_by="t")
        with pytest.raises(MemberExistsError):
            members.add("pgm-geoff", "Again", created_by="t")
        first = keys.claim("pgm-key-0001", "hash-a")
        second = keys.claim("pgm-key-0001", "hash-a")
        mismatch = keys.claim("pgm-key-0001", "hash-b")
        keys.complete("pgm-key-0001", 201, "{}")
        replay = keys.claim("pgm-key-0001", "hash-a")

        assert members.set_active("pgm-geoff", active=False).active is False
        assert first.outcome is ClaimOutcome.NEW
        assert second.outcome is ClaimOutcome.IN_PROGRESS
        assert mismatch.outcome is ClaimOutcome.MISMATCH
        assert replay.outcome is ClaimOutcome.REPLAY
