"""Idempotency records for API v2 mutations (ADR 0009).

A mutation carries an ``Idempotency-Key``. The key is claimed before the
change runs and completed with the response afterwards, so a retry of the
same request returns the stored response and changes nothing. A failed
request releases its key: it changed nothing, so retrying runs it again.
"""

from __future__ import annotations

import datetime as dt
import enum
import hashlib
from contextlib import closing
from dataclasses import dataclass
from typing import TYPE_CHECKING

from grocery_butler.db import get_connection
from grocery_butler.db.adapter import IntegrityError
from grocery_butler.inventory_ledger import utc_now

if TYPE_CHECKING:
    from collections.abc import Callable

RETENTION = dt.timedelta(days=30)
MIN_KEY_LENGTH = 8
MAX_KEY_LENGTH = 128


class ClaimOutcome(enum.Enum):
    """What claiming an idempotency key found."""

    NEW = "new"
    REPLAY = "replay"
    IN_PROGRESS = "in_progress"
    MISMATCH = "mismatch"


@dataclass(frozen=True)
class Claim:
    """The result of claiming a key.

    ``status_code`` and ``response_body`` are set only for ``REPLAY``.
    """

    outcome: ClaimOutcome
    status_code: int | None = None
    response_body: str | None = None


def is_valid_key(key: str) -> bool:
    """Check that a key is printable ASCII of a sensible length.

    Args:
        key: The ``Idempotency-Key`` header value.

    Returns:
        True when the key can be stored and compared safely.
    """
    return (
        MIN_KEY_LENGTH <= len(key) <= MAX_KEY_LENGTH
        and key.isascii()
        and key.isprintable()
        and " " not in key
    )


def request_hash(method: str, path: str, body: bytes) -> str:
    """Fingerprint a request, so a reused key can be told from a retry.

    Args:
        method: The HTTP method.
        path: The request path.
        body: The raw request body.

    Returns:
        A hex SHA-256 digest of the three.
    """
    digest = hashlib.sha256()
    digest.update(method.upper().encode())
    digest.update(b"\n")
    digest.update(path.encode())
    digest.update(b"\n")
    digest.update(body)
    return digest.hexdigest()


class IdempotencyStore:
    """Claim, complete and release idempotency keys.

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

    def claim(self, key: str, fingerprint: str) -> Claim:
        """Claim a key for a request that is about to run.

        Also deletes records older than the retention period.

        Args:
            key: The idempotency key.
            fingerprint: The request's hash from :func:`request_hash`.

        Returns:
            ``NEW`` when the caller should run the request, ``REPLAY`` with
            the stored response when it already succeeded, ``IN_PROGRESS``
            when another request holds the key, or ``MISMATCH`` when the
            key was used for a different request.
        """
        now = self._clock()
        with closing(get_connection(self._db_path)) as conn:
            conn.execute(
                "DELETE FROM api_idempotency_keys WHERE created_at < ?",
                ((now - RETENTION).isoformat(),),
            )
            conn.commit()
            try:
                conn.execute(
                    "INSERT INTO api_idempotency_keys"
                    " (idempotency_key, request_hash, created_at)"
                    " VALUES (?, ?, ?) RETURNING idempotency_key",
                    (key, fingerprint, now.isoformat()),
                ).fetchall()
                conn.commit()
            except IntegrityError:
                return self._existing(key, fingerprint)
        return Claim(ClaimOutcome.NEW)

    def complete(self, key: str, status_code: int, response_body: str) -> None:
        """Store the successful response of a claimed request.

        Args:
            key: The idempotency key.
            status_code: The response's HTTP status.
            response_body: The response body text.
        """
        with closing(get_connection(self._db_path)) as conn:
            conn.execute(
                "UPDATE api_idempotency_keys SET status_code = ?, response_body = ?"
                " WHERE idempotency_key = ?",
                (status_code, response_body, key),
            )
            conn.commit()

    def release(self, key: str) -> None:
        """Give up a claimed key after its request failed.

        Args:
            key: The idempotency key.
        """
        with closing(get_connection(self._db_path)) as conn:
            conn.execute(
                "DELETE FROM api_idempotency_keys"
                " WHERE idempotency_key = ? AND status_code IS NULL",
                (key,),
            )
            conn.commit()

    def _existing(self, key: str, fingerprint: str) -> Claim:
        """Classify a key that is already recorded.

        Runs on a fresh connection: on Postgres the failed insert aborted
        the claiming connection's transaction.

        Args:
            key: The idempotency key.
            fingerprint: The new request's hash.

        Returns:
            ``MISMATCH``, ``IN_PROGRESS`` or ``REPLAY``.
        """
        with closing(get_connection(self._db_path)) as conn:
            rows = conn.execute(
                "SELECT request_hash, status_code, response_body"
                " FROM api_idempotency_keys WHERE idempotency_key = ?",
                (key,),
            ).fetchall()
        if not rows:  # pragma: no cover - released between the two statements
            return Claim(ClaimOutcome.IN_PROGRESS)
        row = rows[0]
        if row["request_hash"] != fingerprint:
            return Claim(ClaimOutcome.MISMATCH)
        if row["status_code"] is None:
            return Claim(ClaimOutcome.IN_PROGRESS)
        return Claim(ClaimOutcome.REPLAY, row["status_code"], row["response_body"])
