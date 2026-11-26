"""Bounded, logged retries for calls that leave the process (ADR 0015).

A model call or a fulfillment adapter call can fail for a moment: a timeout,
a dropped connection, a rate limit. :func:`call_with_retry` tries again a
fixed number of times with a growing pause, and writes an audit event for
every attempt. Anything that isn't a passing fault fails at once.

This is for production calls only. It is never a way to make a test pass.
"""

from __future__ import annotations

import time
from typing import TYPE_CHECKING, TypeVar

from grocery_butler.observability import audit

if TYPE_CHECKING:
    from collections.abc import Callable

T = TypeVar("T")

DEFAULT_ATTEMPTS = 3
DEFAULT_BASE_DELAY = 0.5
DEFAULT_MAX_DELAY = 8.0

_REQUEST_TIMEOUT = 408
_TOO_MANY_REQUESTS = 429
_SERVER_ERROR = 500


class RetriesExhaustedError(RuntimeError):
    """Raised when every attempt of a retried call failed.

    The last failure is this error's ``__cause__``.
    """

    def __init__(self, what: str, attempts: int) -> None:
        """Say what gave up and after how many tries.

        Args:
            what: The call's name.
            attempts: How many times it was tried.
        """
        self.what = what
        self.attempts = attempts
        super().__init__(f"{what} failed after {attempts} attempts.")


def _is_provider_connection_error(error: BaseException) -> bool:
    """Say whether an error is the Anthropic SDK's connection failure.

    Args:
        error: The error raised.

    Returns:
        True for a connection or timeout error from the SDK.
    """
    try:
        import anthropic
    except ImportError:  # pragma: no cover - anthropic is a runtime dependency
        return False
    return isinstance(error, anthropic.APIConnectionError)


def is_transient(error: BaseException) -> bool:
    """Say whether an error is worth trying again.

    Args:
        error: The error a call raised.

    Returns:
        True for timeouts, connection failures, an error that marks
        itself ``transient``, and a provider's 408, 429 or 5xx. False for
        everything else, including a provider's other 4xx responses:
        asking again won't change the answer.
    """
    if isinstance(error, TimeoutError | ConnectionError):
        return True
    if getattr(error, "transient", False) is True:
        return True
    status = getattr(error, "status_code", None)
    if isinstance(status, int):
        return status in (_REQUEST_TIMEOUT, _TOO_MANY_REQUESTS) or (
            status >= _SERVER_ERROR
        )
    return _is_provider_connection_error(error)


def _require_attempts(attempts: int) -> None:
    """Refuse a retry budget of zero or less.

    Args:
        attempts: The number of tries asked for.

    Raises:
        ValueError: If it is less than 1.
    """
    if attempts < 1:
        msg = f"attempts must be at least 1, got {attempts}."
        raise ValueError(msg)


def call_with_retry(
    operation: Callable[[], T],
    *,
    what: str,
    attempts: int = DEFAULT_ATTEMPTS,
    base_delay: float = DEFAULT_BASE_DELAY,
    max_delay: float = DEFAULT_MAX_DELAY,
    sleep: Callable[[float], None] = time.sleep,
    retryable: Callable[[BaseException], bool] = is_transient,
) -> T:
    """Run a call, retrying a passing fault a bounded number of times.

    The pause doubles each time, from ``base_delay`` up to ``max_delay``.
    Every attempt writes a ``call.attempt`` audit event.

    Args:
        operation: The call to make. It takes no arguments.
        what: A name for the audit trail, e.g. ``model.meal_parse``.
        attempts: The most times to try. At least 1.
        base_delay: Seconds to wait after the first failure.
        max_delay: The longest wait between tries.
        sleep: Waits the given seconds; tests pass a recorder.
        retryable: Decides whether an error is worth another try.

    Returns:
        Whatever the call returned.

    Raises:
        ValueError: If ``attempts`` is less than 1.
        RetriesExhaustedError: If the last attempt also failed with a
            passing fault. The fault is its ``__cause__``.
        Exception: A fault that isn't worth retrying is raised as itself.
    """
    _require_attempts(attempts)
    for attempt in range(1, attempts + 1):
        try:
            result = operation()
        except Exception as err:
            transient = retryable(err)
            will_retry = transient and attempt < attempts
            audit(
                "call.attempt",
                what=what,
                attempt=attempt,
                of=attempts,
                outcome="failed",
                error=type(err).__name__,
                will_retry=will_retry,
            )
            if not transient:
                raise
            if not will_retry:
                raise RetriesExhaustedError(what, attempts) from err
            sleep(min(max_delay, base_delay * 2 ** (attempt - 1)))
        else:
            audit("call.attempt", what=what, attempt=attempt, of=attempts, outcome="ok")
            return result
    raise RetriesExhaustedError(what, attempts)  # pragma: no cover
