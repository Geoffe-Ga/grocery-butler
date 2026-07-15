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
        """Test rows stay 'pending' until explicitly swept or resolved.

        Issue #75 (W4): a past-due row is *not* auto-resolved just by
        being read -- only an explicit :meth:`PendingActionsStore.sweep_expired`
        call (or a direct resolution) transitions it. This extends the
        original "no implicit sweeper" pin with the new explicit-sweep
        half of the contract, rather than replacing it, since both
        halves still hold under the new sweep semantics.
        """
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

        # Only an explicit sweep flips the row.
        assert store.sweep_expired() == 1
        swept = store.get_pending_action("e5")
        assert swept is not None
        assert swept.status is PendingActionStatus.EXPIRED


# ---------------------------------------------------------------------------
# W1 (issue #75): requester/resolver persistence
# ---------------------------------------------------------------------------


class TestRequesterAndResolver:
    """Tests for requester/resolver persistence (W1, issue #75)."""

    def test_insert_pending_action_resolver_defaults_to_none(
        self, store: PendingActionsStore
    ) -> None:
        """Test a freshly staged action has no resolver yet."""
        action = store.insert_pending_action(
            action_id="rr1",
            kind="brands_set",
            payload={},
            expires_at=_in_five_minutes(),
        )
        assert action.resolver is None

    def test_mark_approved_records_resolver(self, store: PendingActionsStore) -> None:
        """Test approving a row stamps the resolving caller."""
        store.insert_pending_action(
            action_id="rr2",
            kind="brands_set",
            payload={},
            expires_at=_in_five_minutes(),
        )
        assert store.mark_pending_approved("rr2", resolver="alice") is True
        action = store.get_pending_action("rr2")
        assert action is not None
        assert action.resolver == "alice"

    def test_mark_denied_records_resolver(self, store: PendingActionsStore) -> None:
        """Test denying a row stamps the resolving caller."""
        store.insert_pending_action(
            action_id="rr3",
            kind="brands_set",
            payload={},
            expires_at=_in_five_minutes(),
        )
        assert store.mark_pending_denied("rr3", resolver="bob") is True
        action = store.get_pending_action("rr3")
        assert action is not None
        assert action.resolver == "bob"

    def test_mark_expired_leaves_resolver_none(
        self, store: PendingActionsStore
    ) -> None:
        """Test expiry is system-initiated, so no resolver is stamped."""
        store.insert_pending_action(
            action_id="rr4",
            kind="brands_set",
            payload={},
            expires_at=_in_five_minutes(),
        )
        assert store.mark_pending_expired("rr4") is True
        action = store.get_pending_action("rr4")
        assert action is not None
        assert action.resolver is None


# ---------------------------------------------------------------------------
# W3 (issue #75): PendingActionStatus.FAILED and mark_pending_failed
# ---------------------------------------------------------------------------


class TestMarkPendingFailed:
    """Tests for PendingActionsStore.mark_pending_failed (W3, issue #75)."""

    def _staged(self, store: PendingActionsStore, action_id: str) -> PendingAction:
        """Stage a fresh pending action for transition tests."""
        return store.insert_pending_action(
            action_id=action_id,
            kind="safeway_order_submit",
            payload=_payload(),
            expires_at=_in_five_minutes(),
        )

    def test_failed_status_member_exists(self) -> None:
        """Test PendingActionStatus gains a FAILED member."""
        assert PendingActionStatus.FAILED.value == "failed"

    def test_mark_failed_after_approved_transitions_to_failed(
        self, store: PendingActionsStore
    ) -> None:
        """Test an approved row can be marked failed after a post-claim error."""
        self._staged(store, "mf1")
        store.mark_pending_approved("mf1", resolver="bot")
        assert store.mark_pending_failed("mf1") is True
        action = store.get_pending_action("mf1")
        assert action is not None
        assert action.status is PendingActionStatus.FAILED
        assert action.resolved_at is not None
        assert action.resolver == "bot"

    def test_mark_failed_from_pending_returns_false(
        self, store: PendingActionsStore
    ) -> None:
        """Test a never-claimed row cannot be marked failed directly."""
        self._staged(store, "mf2")
        assert store.mark_pending_failed("mf2") is False
        action = store.get_pending_action("mf2")
        assert action is not None
        assert action.status is PendingActionStatus.PENDING

    def test_mark_failed_from_denied_returns_false(
        self, store: PendingActionsStore
    ) -> None:
        """Test a denied row cannot be marked failed."""
        self._staged(store, "mf3")
        store.mark_pending_denied("mf3", resolver="bot")
        assert store.mark_pending_failed("mf3") is False

    def test_mark_failed_from_expired_returns_false(
        self, store: PendingActionsStore
    ) -> None:
        """Test an expired row cannot be marked failed."""
        self._staged(store, "mf4")
        store.mark_pending_expired("mf4")
        assert store.mark_pending_failed("mf4") is False

    def test_mark_failed_idempotent_second_call_returns_false(
        self, store: PendingActionsStore
    ) -> None:
        """Test marking failed twice reports failure the second time."""
        self._staged(store, "mf5")
        store.mark_pending_approved("mf5", resolver="bot")
        assert store.mark_pending_failed("mf5") is True
        assert store.mark_pending_failed("mf5") is False

    def test_mark_failed_unknown_id_returns_false(
        self, store: PendingActionsStore
    ) -> None:
        """Test an unknown action id reports failure, not an error."""
        assert store.mark_pending_failed("ghost") is False


# ---------------------------------------------------------------------------
# W4 (issue #75): sweep_expired
# ---------------------------------------------------------------------------


class TestSweepExpired:
    """Tests for PendingActionsStore.sweep_expired (W4, issue #75)."""

    def _stage_past_due(self, store: PendingActionsStore, action_id: str) -> None:
        """Stage a pending action whose expiry is already in the past."""
        past = dt.datetime.now(dt.UTC) - dt.timedelta(minutes=1)
        store.insert_pending_action(
            action_id=action_id,
            kind="brands_set",
            payload={},
            expires_at=past,
        )

    def test_sweep_expired_flips_past_due_pending_row(
        self, store: PendingActionsStore
    ) -> None:
        """Test a pending row past its deadline flips to expired on sweep."""
        self._stage_past_due(store, "sw1")
        count = store.sweep_expired()
        assert count == 1
        action = store.get_pending_action("sw1")
        assert action is not None
        assert action.status is PendingActionStatus.EXPIRED
        assert action.resolved_at is not None

    def test_sweep_expired_resolver_stays_none(
        self, store: PendingActionsStore
    ) -> None:
        """Test sweeping is system-initiated: resolver stays NULL."""
        self._stage_past_due(store, "sw2")
        store.sweep_expired()
        action = store.get_pending_action("sw2")
        assert action is not None
        assert action.resolver is None

    def test_sweep_expired_returns_count_of_affected_rows(
        self, store: PendingActionsStore
    ) -> None:
        """Test multiple past-due rows are all counted in one sweep."""
        for action_id in ("sw3", "sw4", "sw5"):
            self._stage_past_due(store, action_id)
        assert store.sweep_expired() == 3

    def test_sweep_expired_is_idempotent(self, store: PendingActionsStore) -> None:
        """Test a second sweep call finds nothing new to expire."""
        self._stage_past_due(store, "sw6")
        assert store.sweep_expired() == 1
        assert store.sweep_expired() == 0

    def test_sweep_expired_leaves_future_pending_untouched(
        self, store: PendingActionsStore
    ) -> None:
        """Test a pending row not yet due is left alone."""
        store.insert_pending_action(
            action_id="sw7",
            kind="brands_set",
            payload={},
            expires_at=_in_five_minutes(),
        )
        assert store.sweep_expired() == 0
        action = store.get_pending_action("sw7")
        assert action is not None
        assert action.status is PendingActionStatus.PENDING

    def test_sweep_expired_leaves_approved_untouched(
        self, store: PendingActionsStore
    ) -> None:
        """Test an already-approved row is never re-resolved by the sweep."""
        self._stage_past_due(store, "sw8")
        store.mark_pending_approved("sw8", resolver="bot")
        assert store.sweep_expired() == 0
        action = store.get_pending_action("sw8")
        assert action is not None
        assert action.status is PendingActionStatus.APPROVED

    def test_sweep_expired_leaves_denied_untouched(
        self, store: PendingActionsStore
    ) -> None:
        """Test an already-denied row is never re-resolved by the sweep."""
        self._stage_past_due(store, "sw9")
        store.mark_pending_denied("sw9", resolver="bot")
        assert store.sweep_expired() == 0
        action = store.get_pending_action("sw9")
        assert action is not None
        assert action.status is PendingActionStatus.DENIED

    def test_sweep_expired_accepts_explicit_now(
        self, store: PendingActionsStore
    ) -> None:
        """Test an explicit reference time is honored over wall-clock now."""
        future_deadline = dt.datetime.now(dt.UTC) + dt.timedelta(minutes=10)
        store.insert_pending_action(
            action_id="sw10",
            kind="brands_set",
            payload={},
            expires_at=future_deadline,
        )
        way_future = future_deadline + dt.timedelta(minutes=1)
        assert store.sweep_expired(now=way_future) == 1
        action = store.get_pending_action("sw10")
        assert action is not None
        assert action.status is PendingActionStatus.EXPIRED


# ---------------------------------------------------------------------------
# W4 (issue #75): list_pending_actions
# ---------------------------------------------------------------------------


class TestListPendingActions:
    """Tests for PendingActionsStore.list_pending_actions (W4, issue #75)."""

    def _set_created_at(self, db_path: str, action_id: str, when: dt.datetime) -> None:
        """Force a row's created_at for deterministic ordering assertions."""
        conn = get_connection(db_path)
        try:
            conn.execute(
                "UPDATE pending_actions SET created_at = ? WHERE action_id = ?",
                (when.isoformat(), action_id),
            )
            conn.commit()
        finally:
            conn.close()

    def test_list_pending_actions_orders_by_created_at_desc(
        self, store: PendingActionsStore, db_path: str
    ) -> None:
        """Test rows come back newest-created first."""
        base = dt.datetime.now(dt.UTC)
        for offset, action_id in enumerate(("la1", "la2", "la3")):
            store.insert_pending_action(
                action_id=action_id,
                kind="brands_set",
                payload={},
                expires_at=_in_five_minutes(),
            )
            self._set_created_at(
                db_path, action_id, base + dt.timedelta(minutes=offset)
            )

        actions = store.list_pending_actions(limit=50, status=None)
        ids = [a.action_id for a in actions if a.action_id in ("la1", "la2", "la3")]
        assert ids == ["la3", "la2", "la1"]

    def test_list_pending_actions_respects_limit(
        self, store: PendingActionsStore
    ) -> None:
        """Test only the requested number of rows are returned."""
        for action_id in ("lb1", "lb2", "lb3"):
            store.insert_pending_action(
                action_id=action_id,
                kind="brands_set",
                payload={},
                expires_at=_in_five_minutes(),
            )
        actions = store.list_pending_actions(limit=2, status=None)
        assert len(actions) == 2

    def test_list_pending_actions_filters_by_status(
        self, store: PendingActionsStore
    ) -> None:
        """Test only rows matching the requested status are returned."""
        store.insert_pending_action(
            action_id="lc1",
            kind="brands_set",
            payload={},
            expires_at=_in_five_minutes(),
        )
        store.insert_pending_action(
            action_id="lc2",
            kind="brands_set",
            payload={},
            expires_at=_in_five_minutes(),
        )
        store.mark_pending_denied("lc2", resolver="bot")

        actions = store.list_pending_actions(
            limit=50, status=PendingActionStatus.DENIED
        )
        ids = [a.action_id for a in actions]
        assert ids == ["lc2"]

    def test_list_pending_actions_status_none_returns_all_statuses(
        self, store: PendingActionsStore
    ) -> None:
        """Test a None status filter returns rows regardless of status."""
        store.insert_pending_action(
            action_id="ld1",
            kind="brands_set",
            payload={},
            expires_at=_in_five_minutes(),
        )
        store.insert_pending_action(
            action_id="ld2",
            kind="brands_set",
            payload={},
            expires_at=_in_five_minutes(),
        )
        store.mark_pending_denied("ld2", resolver="bot")

        actions = store.list_pending_actions(limit=50, status=None)
        ids = {a.action_id for a in actions}
        assert {"ld1", "ld2"}.issubset(ids)

    def test_list_pending_actions_empty_when_no_rows(
        self, store: PendingActionsStore
    ) -> None:
        """Test an empty table yields an empty list, not an error."""
        assert store.list_pending_actions(limit=50, status=None) == []


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
