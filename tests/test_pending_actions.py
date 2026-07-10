"""Tests for grocery_butler.pending_actions (staged-action audit log)."""

from __future__ import annotations

import datetime as dt
import os
from typing import TYPE_CHECKING, Any

import pytest

from grocery_butler.db import get_connection, init_db
from grocery_butler.db.adapter import IntegrityError
from grocery_butler.db.migrate import migrate
from grocery_butler.models import PendingAction, PendingActionStatus
from grocery_butler.pending_actions import PendingActionsStore

if TYPE_CHECKING:
    from pathlib import Path


def _in_five_minutes() -> dt.datetime:
    return dt.datetime.now(dt.UTC) + dt.timedelta(minutes=5)


def _payload() -> dict[str, Any]:
    return {"cart": [{"item": "milk", "qty": 2}], "total": "12.50"}


@pytest.fixture
def db_path(tmp_path: Path) -> str:
    path = str(tmp_path / "test.db")
    init_db(path)
    return path


@pytest.fixture
def store(db_path: str) -> PendingActionsStore:
    return PendingActionsStore(db_path)


class TestMigration:
    """The pending_actions migration applies via the standard runner."""

    def test_creates_pending_actions_table(self, db_path: str) -> None:
        """Test init_db creates the pending_actions table."""
        conn = get_connection(db_path)
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name='pending_actions'"
            )
            row = cursor.fetchone()
        finally:
            conn.close()
        assert row is not None

    def test_creates_status_expiry_index(self, db_path: str) -> None:
        """Test the (status, expires_at) index exists."""
        conn = get_connection(db_path)
        try:
            cursor = conn.execute(
                "SELECT name FROM sqlite_master WHERE type='index' "
                "AND name='idx_pending_actions_status'"
            )
            row = cursor.fetchone()
        finally:
            conn.close()
        assert row is not None

    def test_migration_recorded_and_idempotent(self, db_path: str) -> None:
        """Test the migration is tracked and safe to re-run."""
        assert migrate(db_path) == 0  # already applied by init_db
        conn = get_connection(db_path)
        try:
            cursor = conn.execute(
                "SELECT name FROM schema_migrations WHERE name='pending_actions'"
            )
            row = cursor.fetchone()
        finally:
            conn.close()
        assert row is not None


class TestInsertPendingAction:
    """Tests for PendingActionsStore.insert_pending_action."""

    def test_insert_returns_pending_action(self, store: PendingActionsStore) -> None:
        """Test insert returns the stored row as a model."""
        expires = _in_five_minutes()
        action = store.insert_pending_action(
            action_id="a1",
            kind="safeway_order_submit",
            payload=_payload(),
            expires_at=expires,
        )
        assert isinstance(action, PendingAction)
        assert action.action_id == "a1"
        assert action.kind == "safeway_order_submit"
        assert action.status is PendingActionStatus.PENDING
        assert action.resolved_at is None

    def test_insert_defaults_requester_to_rubotpaul(
        self, store: PendingActionsStore
    ) -> None:
        """Test the requester column defaults to 'rubotpaul'."""
        action = store.insert_pending_action(
            action_id="a2",
            kind="brands_set",
            payload={},
            expires_at=_in_five_minutes(),
        )
        assert action.requester == "rubotpaul"

    def test_insert_honors_custom_requester(self, store: PendingActionsStore) -> None:
        """Test a custom requester value is persisted."""
        action = store.insert_pending_action(
            action_id="a3",
            kind="brands_set",
            payload={},
            expires_at=_in_five_minutes(),
            requester="webui",
        )
        assert action.requester == "webui"

    def test_insert_sets_created_at(self, store: PendingActionsStore) -> None:
        """Test created_at is stamped by the database."""
        action = store.insert_pending_action(
            action_id="a4",
            kind="preferences_set",
            payload={},
            expires_at=_in_five_minutes(),
        )
        assert action.created_at is not None

    def test_payload_round_trips_nested_structure(
        self, store: PendingActionsStore
    ) -> None:
        """Test a nested JSON payload survives the write/read cycle."""
        payload = _payload()
        store.insert_pending_action(
            action_id="a5",
            kind="safeway_order_submit",
            payload=payload,
            expires_at=_in_five_minutes(),
        )
        fetched = store.get_pending_action("a5")
        assert fetched is not None
        assert fetched.payload == payload

    def test_duplicate_action_id_raises_integrity_error(
        self, store: PendingActionsStore
    ) -> None:
        """Test the primary key rejects duplicate action ids."""
        store.insert_pending_action(
            action_id="dup",
            kind="brands_set",
            payload={},
            expires_at=_in_five_minutes(),
        )
        with pytest.raises(IntegrityError):
            store.insert_pending_action(
                action_id="dup",
                kind="brands_set",
                payload={},
                expires_at=_in_five_minutes(),
            )

    def test_insert_raises_if_row_unreadable(
        self, store: PendingActionsStore, monkeypatch: pytest.MonkeyPatch
    ) -> None:
        """Test a defensive error if the inserted row cannot be re-read."""
        monkeypatch.setattr(store, "get_pending_action", lambda action_id: None)
        with pytest.raises(RuntimeError, match="a6"):
            store.insert_pending_action(
                action_id="a6",
                kind="brands_set",
                payload={},
                expires_at=_in_five_minutes(),
            )


class TestGetPendingAction:
    """Tests for PendingActionsStore.get_pending_action."""

    def test_missing_action_returns_none(self, store: PendingActionsStore) -> None:
        """Test lookup of an unknown id returns None."""
        assert store.get_pending_action("nope") is None

    def test_returns_datetime_fields(self, store: PendingActionsStore) -> None:
        """Test expires_at round-trips as a datetime."""
        expires = _in_five_minutes()
        store.insert_pending_action(
            action_id="g1",
            kind="brands_set",
            payload={},
            expires_at=expires,
        )
        fetched = store.get_pending_action("g1")
        assert fetched is not None
        assert isinstance(fetched.expires_at, dt.datetime)
        assert fetched.expires_at == expires


class TestStatusTransitions:
    """Tests for the mark_pending_* transition functions."""

    def _staged(self, store: PendingActionsStore, action_id: str) -> PendingAction:
        return store.insert_pending_action(
            action_id=action_id,
            kind="safeway_order_submit",
            payload=_payload(),
            expires_at=_in_five_minutes(),
        )

    def test_mark_approved(self, store: PendingActionsStore) -> None:
        """Test a pending action can be approved."""
        self._staged(store, "t1")
        assert store.mark_pending_approved("t1") is True
        action = store.get_pending_action("t1")
        assert action is not None
        assert action.status is PendingActionStatus.APPROVED
        assert action.resolved_at is not None

    def test_mark_denied(self, store: PendingActionsStore) -> None:
        """Test a pending action can be denied."""
        self._staged(store, "t2")
        assert store.mark_pending_denied("t2") is True
        action = store.get_pending_action("t2")
        assert action is not None
        assert action.status is PendingActionStatus.DENIED
        assert action.resolved_at is not None

    def test_mark_expired(self, store: PendingActionsStore) -> None:
        """Test a pending action can be expired."""
        self._staged(store, "t3")
        assert store.mark_pending_expired("t3") is True
        action = store.get_pending_action("t3")
        assert action is not None
        assert action.status is PendingActionStatus.EXPIRED
        assert action.resolved_at is not None

    def test_approve_after_deny_fails(self, store: PendingActionsStore) -> None:
        """Test a denied action cannot later be approved."""
        self._staged(store, "t4")
        store.mark_pending_denied("t4")
        assert store.mark_pending_approved("t4") is False
        action = store.get_pending_action("t4")
        assert action is not None
        assert action.status is PendingActionStatus.DENIED

    def test_deny_after_approve_fails(self, store: PendingActionsStore) -> None:
        """Test an approved action cannot later be denied."""
        self._staged(store, "t5")
        store.mark_pending_approved("t5")
        assert store.mark_pending_denied("t5") is False
        action = store.get_pending_action("t5")
        assert action is not None
        assert action.status is PendingActionStatus.APPROVED

    def test_expire_after_approve_fails(self, store: PendingActionsStore) -> None:
        """Test an approved action cannot later be expired."""
        self._staged(store, "t6")
        store.mark_pending_approved("t6")
        assert store.mark_pending_expired("t6") is False

    def test_double_approve_fails(self, store: PendingActionsStore) -> None:
        """Test approving twice reports failure the second time."""
        self._staged(store, "t7")
        assert store.mark_pending_approved("t7") is True
        assert store.mark_pending_approved("t7") is False

    def test_transitions_on_missing_id_return_false(
        self, store: PendingActionsStore
    ) -> None:
        """Test all transitions report failure for unknown ids."""
        assert store.mark_pending_approved("ghost") is False
        assert store.mark_pending_denied("ghost") is False
        assert store.mark_pending_expired("ghost") is False


class TestExpirySemantics:
    """Tests for expiry deadline handling."""

    def test_is_expired_false_before_deadline(self, store: PendingActionsStore) -> None:
        """Test is_expired is False while the deadline is in the future."""
        action = store.insert_pending_action(
            action_id="e1",
            kind="brands_set",
            payload={},
            expires_at=_in_five_minutes(),
        )
        assert action.is_expired() is False

    def test_is_expired_true_after_deadline(self, store: PendingActionsStore) -> None:
        """Test is_expired is True once the deadline has passed."""
        past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        action = store.insert_pending_action(
            action_id="e2",
            kind="brands_set",
            payload={},
            expires_at=past,
        )
        assert action.is_expired() is True

    def test_is_expired_accepts_explicit_reference_time(self) -> None:
        """Test is_expired compares against a caller-supplied clock."""
        expires = dt.datetime(2026, 1, 1, 12, 0, tzinfo=dt.UTC)
        action = PendingAction(
            action_id="e3",
            kind="brands_set",
            payload={},
            expires_at=expires,
        )
        before = dt.datetime(2026, 1, 1, 11, 59, tzinfo=dt.UTC)
        after = dt.datetime(2026, 1, 1, 12, 1, tzinfo=dt.UTC)
        assert action.is_expired(now=before) is False
        assert action.is_expired(now=after) is True

    def test_is_expired_handles_naive_expiry(self) -> None:
        """Test a naive expires_at is treated as UTC."""
        action = PendingAction(
            action_id="e4",
            kind="brands_set",
            payload={},
            expires_at=dt.datetime(2026, 1, 1, 12, 0),  # intentionally naive
        )
        after = dt.datetime(2026, 1, 1, 12, 1, tzinfo=dt.UTC)
        assert action.is_expired(now=after) is True

    def test_deadline_passing_does_not_change_status(
        self, store: PendingActionsStore
    ) -> None:
        """Test rows stay 'pending' until explicitly marked expired."""
        past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        store.insert_pending_action(
            action_id="e5",
            kind="brands_set",
            payload={},
            expires_at=past,
        )
        action = store.get_pending_action("e5")
        assert action is not None
        assert action.status is PendingActionStatus.PENDING
        assert action.is_expired() is True


# ------------------------------------------------------------------
# PostgreSQL integration tests (require a running Postgres instance)
# ------------------------------------------------------------------

_TEST_DB_URL = os.environ.get("TEST_DATABASE_URL", "")
_skip_no_pg = pytest.mark.skipif(
    not _TEST_DB_URL,
    reason="TEST_DATABASE_URL not set — no Postgres server available",
)


@_skip_no_pg
class TestPendingActionsPostgres:
    """Integration tests against PostgreSQL.

    Requires a running PostgreSQL instance. Set TEST_DATABASE_URL
    to run: ``TEST_DATABASE_URL=postgresql://user:pass@localhost/test``.
    """

    @pytest.fixture
    def pg_store(self) -> PendingActionsStore:
        migrate(_TEST_DB_URL)
        conn = get_connection(_TEST_DB_URL)
        try:
            conn.execute(
                "DELETE FROM pending_actions WHERE requester = ?", ("pg-test",)
            )
            conn.commit()
        finally:
            conn.close()
        return PendingActionsStore(_TEST_DB_URL)

    def test_full_lifecycle(self, pg_store: PendingActionsStore) -> None:
        """Test insert, read, and transition round-trip on PostgreSQL."""
        action = pg_store.insert_pending_action(
            action_id="pg-1",
            kind="safeway_order_submit",
            payload=_payload(),
            expires_at=_in_five_minutes(),
            requester="pg-test",
        )
        assert action.status is PendingActionStatus.PENDING
        assert action.payload == _payload()
        assert pg_store.mark_pending_approved("pg-1") is True
        assert pg_store.mark_pending_approved("pg-1") is False
        fetched = pg_store.get_pending_action("pg-1")
        assert fetched is not None
        assert fetched.status is PendingActionStatus.APPROVED
