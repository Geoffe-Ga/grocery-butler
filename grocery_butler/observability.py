"""Audit events and correlation ids (ADR 0015).

An audit event is one line of JSON on the ``grocery_butler.audit`` logger:
what happened, when, and the correlation id of the work it belongs to. A
correlation id ties together everything one request did. A capture session
(today, an observation batch) carries its own ``capture_session`` field, so
its events can be found across the several requests it spans.

Two rules hold for every event:

- No image, no raw bytes and no secret is ever written. A field whose name
  suggests one is replaced, whatever its value.
- Emitting an event never breaks the work it describes.

This module knows nothing about the database or the domain. Counting is a
callable the caller binds; see :mod:`grocery_butler.metrics`.
"""

from __future__ import annotations

import contextvars
import datetime as dt
import json
import logging
import re
import sys
import uuid
from contextlib import contextmanager
from typing import TYPE_CHECKING, Any

if TYPE_CHECKING:
    from collections.abc import Callable, Iterator

    from flask import Flask, Response

AUDIT_LOGGER = "grocery_butler.audit"
CORRELATION_HEADER = "X-Correlation-ID"
REDACTED = "[redacted]"
MAX_TEXT = 500
STATE_CHANGING_METHODS = frozenset({"POST", "PUT", "PATCH", "DELETE"})

_TRUNCATED = "...[truncated]"
_CORRELATION_RE = re.compile(r"[A-Za-z0-9_-]{8,64}")
# A field whose name contains one of these never has its value logged.
_SENSITIVE_PARTS = (
    "image",
    "bytes",
    "secret",
    "token",
    "password",
    "passphrase",
    "authorization",
    "api_key",
    "apikey",
    "credential",
    "cookie",
)
_RESERVED = frozenset({"event", "at", "correlation_id"})

_logger = logging.getLogger(AUDIT_LOGGER)
_correlation: contextvars.ContextVar[str | None] = contextvars.ContextVar(
    "grocery_butler_correlation", default=None
)
_counter: contextvars.ContextVar[Callable[[str], None] | None] = contextvars.ContextVar(
    "grocery_butler_counter", default=None
)


def new_correlation_id() -> str:
    """Make a fresh correlation id.

    Returns:
        32 hexadecimal characters.
    """
    return uuid.uuid4().hex


def valid_correlation_id(text: str) -> bool:
    """Say whether a caller-supplied id is safe to adopt and log.

    Args:
        text: The candidate id.

    Returns:
        True for 8 to 64 letters, digits, hyphens and underscores.
    """
    return _CORRELATION_RE.fullmatch(text) is not None


def current_correlation_id() -> str:
    """Return the id of the work in progress, making one if none is bound.

    Returns:
        The correlation id.
    """
    current = _correlation.get()
    if current is None:
        current = new_correlation_id()
        _correlation.set(current)
    return current


@contextmanager
def bound(
    correlation_id: str | None, count: Callable[[str], None] | None = None
) -> Iterator[None]:
    """Bind a correlation id, and optionally a counter, for a block of work.

    Args:
        correlation_id: The id to use, or None to have one made on first use.
        count: Called with each event's name, so some can be counted.

    Yields:
        Nothing. The previous bindings return when the block ends.
    """
    id_token = _correlation.set(correlation_id)
    count_token = _counter.set(count)
    try:
        yield
    finally:
        _counter.reset(count_token)
        _correlation.reset(id_token)


def _is_sensitive(name: str) -> bool:
    """Say whether a field name suggests a secret or an image.

    Args:
        name: The field name.

    Returns:
        True if its value must not be logged.
    """
    lowered = name.lower()
    return any(part in lowered for part in _SENSITIVE_PARTS)


def _safe(value: Any) -> Any:
    """Reduce a value to something safe and small enough to log.

    Args:
        value: Any field value.

    Returns:
        A JSON-ready value with bytes removed, sensitive keys redacted and
        long text cut.
    """
    if isinstance(value, bytes | bytearray | memoryview):
        return REDACTED
    if isinstance(value, dict):
        return {
            str(key): REDACTED if _is_sensitive(str(key)) else _safe(inner)
            for key, inner in value.items()
        }
    if isinstance(value, list | tuple | set | frozenset):
        return [_safe(inner) for inner in value]
    if value is None or isinstance(value, bool | int | float):
        return value
    text = str(value)
    if len(text) > MAX_TEXT:
        return text[:MAX_TEXT] + _TRUNCATED
    return text


def _count(event: str) -> None:
    """Tell the bound counter about an event, without ever raising.

    Args:
        event: The event name.
    """
    counter = _counter.get()
    if counter is None:
        return
    try:
        counter(event)
    except Exception as err:
        # The event was already logged. Say that counting failed, through
        # the logger directly so a broken counter can't recurse.
        _emit("audit.count_failed", {"counted": event, "error": type(err).__name__})


def _emit(event: str, fields: dict[str, Any]) -> dict[str, Any]:
    """Write one audit line.

    Args:
        event: The event name.
        fields: Its fields, not yet made safe.

    Returns:
        The record as logged.
    """
    record: dict[str, Any] = {
        key: REDACTED if _is_sensitive(key) else _safe(value)
        for key, value in fields.items()
        if key not in _RESERVED
    }
    record["event"] = event
    record["at"] = dt.datetime.now(dt.UTC).isoformat()
    record["correlation_id"] = current_correlation_id()
    _logger.info(json.dumps(record, sort_keys=True))
    return record


def audit(event: str, **fields: Any) -> dict[str, Any]:
    """Emit one audit event.

    Args:
        event: A dotted name, e.g. ``extraction.failed``.
        **fields: What happened. Never pass an image, bytes or a secret;
            if one arrives anyway it is redacted.

    Returns:
        The record as logged, for callers and tests that want to see it.
    """
    record = _emit(event, fields)
    _count(event)
    return record


class _AuditStreamHandler(logging.Handler):
    """Writes audit lines to standard error; its type marks it as ours."""

    def emit(self, record: logging.LogRecord) -> None:
        """Write one audit line.

        Args:
            record: The log record holding the JSON line.
        """
        sys.stderr.write(self.format(record) + "\n")


def configure_audit_logging() -> None:
    """Make sure audit lines are written somewhere.

    The root logger drops INFO by default, which would silently discard
    the audit trail. This gives the audit logger its own stderr handler,
    once, and stops it from also printing through the root logger.
    """
    _logger.setLevel(logging.INFO)
    if any(isinstance(handler, _AuditStreamHandler) for handler in _logger.handlers):
        return
    handler = _AuditStreamHandler()
    handler.setFormatter(logging.Formatter("%(message)s"))
    _logger.addHandler(handler)
    _logger.propagate = False


def init_observability(app: Flask, count: Callable[[str], None] | None = None) -> None:
    """Give every request a correlation id and audit every change.

    Register this before any other request hook, so a request another
    hook rejects is still identified and recorded.

    Args:
        app: The Flask application.
        count: Called with each event's name during a request.
    """
    from flask import g, request

    configure_audit_logging()

    def _begin() -> None:
        """Adopt the caller's id if it is safe, else make one."""
        supplied = request.headers.get(CORRELATION_HEADER, "")
        chosen = supplied if valid_correlation_id(supplied) else new_correlation_id()
        _correlation.set(chosen)
        _counter.set(count)
        g.correlation_id = chosen

    def _finish(response: Response) -> Response:
        """Return the id to the caller and record a state-changing request."""
        response.headers[CORRELATION_HEADER] = current_correlation_id()
        if request.method in STATE_CHANGING_METHODS:
            rule = request.url_rule
            audit(
                "http.request",
                method=request.method,
                route=rule.rule if rule is not None else "unmatched",
                status=response.status_code,
                actor=g.get("actor"),
            )
        return response

    def _end(_error: BaseException | None) -> None:
        """Unbind, so the next request on this thread starts clean."""
        _correlation.set(None)
        _counter.set(None)

    app.before_request(_begin)
    app.after_request(_finish)
    app.teardown_request(_end)
