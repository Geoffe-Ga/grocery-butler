"""Duplicate-order ledger backing the Safeway order-submission guard.

Issue #61 (real-money launch blocker): a timeout during order submission
leaves the outcome unknown, and a naive client-side retry (or a re-staged
identical cart) can submit the same cart to Safeway twice, double-charging
the user. This module provides two building blocks for preventing that:

* :func:`cart_fingerprint` — a deterministic, content-only hash of a cart
  (items, quantities, and fulfillment method — no prices/totals) used to
  recognize "the same cart" across separate submission attempts.
* :class:`OrderSubmissionStore` — a small SQLite/PostgreSQL-backed ledger
  of submission attempts, keyed by that fingerprint, that
  :class:`grocery_butler.safeway_pipeline.SafewayPipeline` consults before
  allowing a new submission to proceed.
"""

from __future__ import annotations

import datetime as dt
import hashlib
import json
import logging
import tempfile
import uuid
from pathlib import Path
from typing import TYPE_CHECKING

from grocery_butler.db import get_connection, init_db

if TYPE_CHECKING:
    from grocery_butler.db.adapter import DatabaseConnection, DictRow
    from grocery_butler.models import CartSummary

logger = logging.getLogger(__name__)

#: Statuses that block a resubmission of the same cart fingerprint. A
#: 'failed' submission is definitive (Safeway rejected it outright) and
#: does not block retries; 'submitted' (in flight), 'unknown' (timed out —
#: outcome unclear), and 'confirmed' (already placed) all do.
_BLOCKING_STATUSES = ("submitted", "unknown", "confirmed")

#: How long a submission attempt blocks a same-cart resubmission.
DUPLICATE_WINDOW = dt.timedelta(minutes=30)


def cart_fingerprint(cart: CartSummary) -> str:
    """Compute a deterministic, content-only fingerprint for a cart.

    The fingerprint covers exactly what makes two submissions "the same
    order": the total quantity ordered per product id across both regular
    and restock items, plus the recommended fulfillment method. It
    deliberately excludes prices and totals, which can drift between
    submission attempts (e.g. re-priced products) without changing what
    would actually be ordered.

    Quantities for a repeated product id are summed before hashing rather
    than hashed as separate (product_id, quantity) line entries. The cart
    submitted to ``/order/submit`` is client-supplied JSON (validated only
    by schema, not rebuilt server-side), so a client that split one
    product's quantity across two line items — accidentally or
    otherwise — would otherwise produce a different fingerprint for an
    economically identical order and slip past the duplicate-order guard.

    Args:
        cart: The cart summary to fingerprint.

    Returns:
        A 64-character lowercase hex SHA-256 digest.
    """
    quantities: dict[str, int] = {}
    for item in [*cart.items, *cart.restock_items]:
        product_id = item.safeway_product.product_id
        quantities[product_id] = quantities.get(product_id, 0) + item.quantity_to_order
    payload = {
        "items": sorted(quantities.items()),
        "fulfillment": cart.recommended_fulfillment.value,
    }
    canonical = json.dumps(payload, sort_keys=True, separators=(",", ":"))
    return hashlib.sha256(canonical.encode("utf-8")).hexdigest()


def _resolve_db_path(db_path: str) -> str:
    """Resolve the effective SQLite path for a new store.

    The literal ``":memory:"`` sentinel maps, at the adapter layer, to a
    single process-wide shared-cache SQLite database that is kept alive
    for the lifetime of the process so its data survives across
    connections (see :func:`grocery_butler.db.init_db`). That sharing is
    exactly what other stores want when pointed at the same app
    database, but it also means every ``OrderSubmissionStore(":memory:")``
    created in the same process — e.g. by a separate
    :class:`~grocery_butler.safeway_pipeline.SafewayPipeline` built in an
    unrelated test — would see the *same* duplicate-order ledger and
    could spuriously block each other. ``":memory:"`` is only ever used
    as a throwaway placeholder in tests (production always supplies a
    real file path or PostgreSQL URL), so each such store instead gets
    its own private on-disk SQLite file, keeping unrelated pipeline
    instances' ledgers isolated. A warning is logged when this fallback
    is taken, since it means the duplicate-order ledger — the guard
    against double-charging a real payment method — is being redirected
    to a throwaway temp file rather than a durable, shared path.

    Args:
        db_path: The path or URL passed to the constructor.

    Returns:
        ``db_path`` unchanged, unless it is the ``":memory:"`` sentinel,
        in which case a fresh unique temp-file path.
    """
    if db_path != ":memory:":
        return db_path
    logger.warning(
        "OrderSubmissionStore(':memory:') is redirecting the "
        "duplicate-order ledger to a throwaway temp file; production "
        "must supply a real SQLite file path or PostgreSQL URL."
    )
    unique_name = f"grocery_butler_order_submissions_{uuid.uuid4().hex}.db"
    return str(Path(tempfile.gettempdir()) / unique_name)


def _lock_fingerprint(conn: DatabaseConnection, fingerprint: str) -> None:
    """Take a PostgreSQL advisory lock scoped to a cart fingerprint.

    PostgreSQL's default READ COMMITTED isolation lets two concurrent
    transactions both evaluate a guarded insert's ``WHERE NOT EXISTS``
    subquery as true before either commits, so two racing submissions of
    an identical cart could otherwise both insert a row (Issue #61
    security review). Taking a ``pg_advisory_xact_lock`` on the
    fingerprint first, on the same connection, forces the second
    transaction to wait until the first commits (and its row becomes
    visible) or rolls back. The lock is transaction-scoped and is
    released automatically on commit/rollback.

    Args:
        conn: The connection to issue the lock on. Must be the same
            connection subsequently used for the guarded insert.
        fingerprint: The cart fingerprint to lock.
    """
    conn.execute("SELECT pg_advisory_xact_lock(hashtext(?))", (fingerprint,))


class OrderSubmissionStore:
    """Ledger of Safeway order-submission attempts, keyed by cart fingerprint.

    Backs the duplicate-order guard in
    :class:`grocery_butler.safeway_pipeline.SafewayPipeline`: every
    submission attempt is recorded before the outbound Safeway call
    (fail-closed) and later marked with its final status, so a
    resubmission of the same cart within :data:`DUPLICATE_WINDOW` can be
    detected and blocked.

    Args:
        db_path: Path to the SQLite database file, or a PostgreSQL URL.
    """

    def __init__(self, db_path: str) -> None:
        """Initialize the store and ensure its schema exists.

        Args:
            db_path: Path to the SQLite database file, or a PostgreSQL URL.
        """
        self._db_path = _resolve_db_path(db_path)
        self._is_postgres = self._db_path.startswith(("postgresql://", "postgres://"))
        init_db(self._db_path)

    def _connect(self) -> DatabaseConnection:
        """Open a new database connection.

        Returns:
            Configured DatabaseConnection.
        """
        return get_connection(self._db_path)

    def record_attempt(self, idempotency_key: str, cart_fingerprint: str) -> int:
        """Record a new submission attempt before calling Safeway.

        Inserted with status ``'submitted'`` so the attempt blocks
        concurrent/duplicate resubmissions even before the outbound call
        completes (fail-closed).

        Warning:
            This is a non-atomic building block, not the duplicate-order
            guard. Pairing a :meth:`find_recent_blocking` check with a
            subsequent call to this method runs the two as separate
            statements on separate connections, which is vulnerable to a
            check-then-insert (TOCTOU) race: two concurrent submissions
            of an identical cart can both pass the check before either
            insert commits, defeating the guard (Issue #61 security
            review). The duplicate-order guard MUST use
            :meth:`try_record_attempt` instead, which performs the
            check and insert atomically on one connection. This method
            (and :meth:`find_recent_blocking`) are kept only for their
            direct unit tests.

        Args:
            idempotency_key: The client order id used for this attempt.
            cart_fingerprint: Fingerprint of the cart being submitted.

        Returns:
            The new row's integer id, for use with :meth:`mark`.

        Raises:
            RuntimeError: If the insert did not yield a row id.
        """
        created_at = dt.datetime.now(dt.UTC).isoformat()
        conn = self._connect()
        try:
            result = conn.execute(
                "INSERT INTO order_submissions "
                "(idempotency_key, cart_fingerprint, status, created_at) "
                "VALUES (?, ?, 'submitted', ?)",
                (idempotency_key, cart_fingerprint, created_at),
            )
            submission_id = result.lastrowid
            conn.commit()
        finally:
            conn.close()
        if submission_id is None:
            raise RuntimeError("order_submissions insert did not return a row id")
        return int(submission_id)

    def mark(
        self,
        submission_id: int,
        status: str,
        order_id: str | None = None,
    ) -> None:
        """Update a submission attempt's final status.

        Args:
            submission_id: Row id returned by :meth:`record_attempt`.
            status: New status (``'confirmed'``, ``'unknown'``, or
                ``'failed'``).
            order_id: Safeway order id, if the submission succeeded.
        """
        conn = self._connect()
        try:
            conn.execute(
                "UPDATE order_submissions SET status = ?, order_id = ? WHERE id = ?",
                (status, order_id, submission_id),
            )
            conn.commit()
        finally:
            conn.close()

    def find_recent_blocking(
        self,
        cart_fingerprint: str,
        within: dt.timedelta,
    ) -> DictRow | None:
        """Find a recent submission that should block a new attempt.

        Warning:
            This is a non-atomic building block, not the duplicate-order
            guard. Pairing this check with a subsequent
            :meth:`record_attempt` call runs the two as separate
            statements on separate connections, which is vulnerable to a
            check-then-insert (TOCTOU) race: two concurrent submissions
            of an identical cart can both pass this check before either
            insert commits, defeating the guard (Issue #61 security
            review). The duplicate-order guard MUST use
            :meth:`try_record_attempt` instead, which performs the
            check and insert atomically on one connection. This method
            (and :meth:`record_attempt`) are kept only for their direct
            unit tests.

        Args:
            cart_fingerprint: Fingerprint of the cart being submitted.
            within: How far back to look for a blocking submission.

        Returns:
            The blocking row, or None if no blocking submission exists.
        """
        cutoff = (dt.datetime.now(dt.UTC) - within).isoformat()
        conn = self._connect()
        try:
            result = conn.execute(
                "SELECT id, status, order_id, created_at FROM order_submissions "
                "WHERE cart_fingerprint = ? AND status IN (?, ?, ?) "
                "AND created_at >= ? "
                "ORDER BY created_at DESC LIMIT 1",
                (cart_fingerprint, *_BLOCKING_STATUSES, cutoff),
            )
            row: DictRow | None = result.fetchone()
        finally:
            conn.close()
        return row

    def try_record_attempt(
        self,
        idempotency_key: str,
        cart_fingerprint: str,
        within: dt.timedelta,
    ) -> int | None:
        """Atomically record an attempt if no recent one blocks it.

        This is the race-safe variant of the duplicate-order guard that
        :class:`~grocery_butler.safeway_pipeline.SafewayPipeline` must
        use (Issue #61 security review): the previous
        :meth:`find_recent_blocking` + :meth:`record_attempt` pair ran
        as a SELECT then an INSERT on two separate connections, so two
        concurrent submissions of an identical cart could both pass the
        SELECT and both reach ``OrderService.submit_order`` — the exact
        double-charge this guard exists to prevent. This method instead
        performs a single guarded ``INSERT ... SELECT ... WHERE NOT
        EXISTS (...)`` on one connection, so only one of two racing
        attempts for the same fingerprint can insert a row.

        For a PostgreSQL-backed store, a ``pg_advisory_xact_lock`` on
        the cart fingerprint is taken first, on that same connection
        (see :func:`_lock_fingerprint`): PostgreSQL's default READ
        COMMITTED isolation can otherwise let two concurrent
        transactions both evaluate the guarded insert's subquery as
        true before either commits. SQLite is single-writer, so no such
        lock is needed there.

        Args:
            idempotency_key: The client order id used for this attempt.
            cart_fingerprint: Fingerprint of the cart being submitted.
            within: How far back to look for a blocking submission.

        Returns:
            The new row's integer id if the insert succeeded, or None
            if a recent blocking submission already exists for this
            fingerprint.
        """
        created_at = dt.datetime.now(dt.UTC).isoformat()
        cutoff = (dt.datetime.now(dt.UTC) - within).isoformat()
        conn = self._connect()
        try:
            if self._is_postgres:
                _lock_fingerprint(conn, cart_fingerprint)
            result = conn.execute(
                "INSERT INTO order_submissions "
                "(idempotency_key, cart_fingerprint, status, created_at) "
                "SELECT ?, ?, 'submitted', ? "
                "WHERE NOT EXISTS ("
                "SELECT 1 FROM order_submissions "
                "WHERE cart_fingerprint = ? AND status IN (?, ?, ?) "
                "AND created_at >= ?"
                ")",
                (
                    idempotency_key,
                    cart_fingerprint,
                    created_at,
                    cart_fingerprint,
                    *_BLOCKING_STATUSES,
                    cutoff,
                ),
            )
            submission_id = result.lastrowid
            rowcount = result.rowcount
            conn.commit()
        finally:
            conn.close()
        if rowcount == 0 or submission_id is None:
            return None
        return int(submission_id)
