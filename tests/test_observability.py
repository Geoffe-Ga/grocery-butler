"""Tests for audit events, correlation ids and bounded retries (ADR 0015)."""

from __future__ import annotations

import json
import logging
import os
from typing import TYPE_CHECKING, Any
from unittest.mock import patch

import pytest

from grocery_butler.app import create_app
from grocery_butler.observability import (
    AUDIT_LOGGER,
    CORRELATION_HEADER,
    REDACTED,
    STATE_CHANGING_METHODS,
    audit,
    bound,
    current_correlation_id,
    new_correlation_id,
    valid_correlation_id,
)
from grocery_butler.retries import RetriesExhaustedError, call_with_retry, is_transient
from tests.test_api_v2 import SECRET_ENV, Api

if TYPE_CHECKING:
    from collections.abc import Iterator
    from pathlib import Path

    from flask import Flask


class _ListHandler(logging.Handler):
    """Collect audit records as parsed JSON."""

    def __init__(self) -> None:
        """Start with no records."""
        super().__init__()
        self.events: list[dict[str, Any]] = []

    def emit(self, record: logging.LogRecord) -> None:
        """Parse and keep one audit line."""
        self.events.append(json.loads(record.getMessage()))


@pytest.fixture()
def events() -> Iterator[list[dict[str, Any]]]:
    """Capture every audit event emitted during the test."""
    handler = _ListHandler()
    logger = logging.getLogger(AUDIT_LOGGER)
    previous = logger.level
    logger.addHandler(handler)
    logger.setLevel(logging.INFO)
    yield handler.events
    logger.removeHandler(handler)
    logger.setLevel(previous)


class TestCorrelationIds:
    """Every piece of work can be tied to one id."""

    def test_new_ids_are_valid_and_distinct(self) -> None:
        """A generated id passes its own check and never repeats."""
        first, second = new_correlation_id(), new_correlation_id()

        assert first != second
        assert valid_correlation_id(first)

    @pytest.mark.parametrize(
        "text", ["abcdefgh", "req-2026_11_26-0001", "A" * 64, "0123456789abcdef"]
    )
    def test_reasonable_caller_ids_are_accepted(self, text: str) -> None:
        """A caller may supply its own id."""
        assert valid_correlation_id(text)

    @pytest.mark.parametrize(
        "text",
        ["", "short", "A" * 65, "has space1", "new\nline12", "semi;colon1", "é" * 8],
    )
    def test_unsafe_caller_ids_are_refused(self, text: str) -> None:
        """Anything that could forge a log line is not an id."""
        assert not valid_correlation_id(text)

    def test_bound_sets_and_restores_the_id(self) -> None:
        """Inside the block the id is the bound one; outside it is not."""
        with bound("outer-id-0001"):
            assert current_correlation_id() == "outer-id-0001"
            with bound("inner-id-0002"):
                assert current_correlation_id() == "inner-id-0002"
            assert current_correlation_id() == "outer-id-0001"

    def test_an_id_is_made_when_none_is_bound(self) -> None:
        """Work outside any request still gets an id, and keeps it."""
        with bound(None):
            first = current_correlation_id()

            assert valid_correlation_id(first)
            assert current_correlation_id() == first


class TestAudit:
    """An audit event is one JSON line with an id and no secrets."""

    def test_event_shape(self, events: list[dict[str, Any]]) -> None:
        """The line carries the event, the id, the time and its fields."""
        with bound("shape-test-0001"):
            returned = audit("plan.built", version=3, actor="geoff@test")

        assert events == [returned]
        assert returned["event"] == "plan.built"
        assert returned["correlation_id"] == "shape-test-0001"
        assert returned["version"] == 3
        assert returned["actor"] == "geoff@test"
        assert returned["at"].endswith("+00:00")

    @pytest.mark.parametrize(
        "key",
        [
            "image",
            "image_bytes",
            "api_key",
            "token",
            "bearer_token",
            "password",
            "Authorization",
            "client_secret",
            "session_cookie",
            "backup_passphrase",
        ],
    )
    def test_sensitive_fields_are_redacted(
        self, events: list[dict[str, Any]], key: str
    ) -> None:
        """A field whose name suggests a secret or an image never gets out."""
        marker = "do-not-log-" + os.urandom(4).hex()

        audit("test.event", **{key: marker})

        assert events[0][key] == REDACTED
        assert marker not in json.dumps(events)

    def test_bytes_are_never_logged(self, events: list[dict[str, Any]]) -> None:
        """Raw bytes, under any name, are replaced."""
        audit("test.event", payload=b"\xff\xd8\xff", nested={"blob": bytearray(b"x")})

        assert events[0]["payload"] == REDACTED
        assert events[0]["nested"] == {"blob": REDACTED}

    def test_nested_sensitive_fields_are_redacted(
        self, events: list[dict[str, Any]]
    ) -> None:
        """Redaction reaches into objects and lists."""
        audit("test.event", detail={"token": "t", "ok": 1}, rows=[{"password": "p"}])

        assert events[0]["detail"] == {"token": REDACTED, "ok": 1}
        assert events[0]["rows"] == [{"password": REDACTED}]

    def test_long_text_is_cut(self, events: list[dict[str, Any]]) -> None:
        """A long value is truncated, so a pasted document can't ride along."""
        audit("test.event", notes="n" * 5000)

        assert len(events[0]["notes"]) < 600
        assert events[0]["notes"].endswith("[truncated]")

    def test_unusual_values_become_text(self, events: list[dict[str, Any]]) -> None:
        """A value JSON can't carry is logged as its text form."""
        audit("test.event", when=ValueError("boom"), amount=1.5, flag=None)

        assert events[0]["when"] == "boom"
        assert events[0]["amount"] == 1.5
        assert events[0]["flag"] is None

    def test_reserved_names_can_not_be_overwritten(
        self, events: list[dict[str, Any]]
    ) -> None:
        """A field can't replace the event's own id."""
        with bound("reserved-id-0001"):
            audit("test.event", **{"correlation_id": "forged", "at": "never"})

        assert events[0]["correlation_id"] == "reserved-id-0001"
        assert events[0]["at"] != "never"

    def test_counted_events_reach_the_counter(self) -> None:
        """The bound counter is told about every event by name."""
        seen: list[str] = []

        with bound("counter-id-0001", count=seen.append):
            audit("extraction.failed", stage="test")
            audit("plan.built")

        assert seen == ["extraction.failed", "plan.built"]

    def test_a_failing_counter_does_not_lose_the_event(
        self, events: list[dict[str, Any]]
    ) -> None:
        """If counting fails the event is still logged, with the failure."""

        def broken(_event: str) -> None:
            raise OSError

        with bound("broken-id-0001", count=broken):
            audit("extraction.failed", stage="test")

        assert [e["event"] for e in events] == [
            "extraction.failed",
            "audit.count_failed",
        ]
        assert events[1]["error"] == "OSError"


class TestRetries:
    """A call is retried a bounded number of times, and every try is logged."""

    def test_success_first_time(self, events: list[dict[str, Any]]) -> None:
        """One try, one attempt event, no sleep."""
        slept: list[float] = []

        result = call_with_retry(lambda: "ok", what="test.call", sleep=slept.append)

        assert result == "ok"
        assert slept == []
        assert [(e["event"], e["attempt"], e["outcome"]) for e in events] == [
            ("call.attempt", 1, "ok")
        ]

    def test_second_try_succeeds_and_both_are_logged(
        self, events: list[dict[str, Any]]
    ) -> None:
        """A transient failure is retried; both attempts are in the log."""
        outcomes: list[Any] = [TimeoutError("slow"), "ok"]
        slept: list[float] = []

        def flaky() -> str:
            outcome = outcomes.pop(0)
            if isinstance(outcome, Exception):
                raise outcome
            return str(outcome)

        result = call_with_retry(
            flaky, what="adapter.fake", sleep=slept.append, base_delay=0.5
        )

        assert result == "ok"
        assert slept == [0.5]
        assert [(e["attempt"], e["outcome"]) for e in events] == [
            (1, "failed"),
            (2, "ok"),
        ]
        assert events[0]["what"] == "adapter.fake"
        assert events[0]["error"] == "TimeoutError"
        assert events[0]["will_retry"] is True

    def test_gives_up_after_the_last_attempt(
        self, events: list[dict[str, Any]]
    ) -> None:
        """Three tries, then the last error is raised inside a clear one."""
        calls: list[int] = []
        slept: list[float] = []

        def always_down() -> str:
            calls.append(1)
            raise ConnectionError("down")

        with pytest.raises(RetriesExhaustedError, match="3 attempts") as raised:
            call_with_retry(
                always_down,
                what="model.test",
                attempts=3,
                base_delay=1.0,
                max_delay=1.5,
                sleep=slept.append,
            )

        assert len(calls) == 3
        assert slept == [1.0, 1.5]
        assert isinstance(raised.value.__cause__, ConnectionError)
        assert raised.value.attempts == 3
        assert [e["will_retry"] for e in events] == [True, True, False]

    def test_a_permanent_error_is_not_retried(
        self, events: list[dict[str, Any]]
    ) -> None:
        """A bug or a bad request fails at once, as itself."""
        calls: list[int] = []

        message = "bad input"

        def wrong() -> str:
            calls.append(1)
            raise ValueError(message)

        with pytest.raises(ValueError, match=message):
            call_with_retry(wrong, what="model.test", sleep=lambda _s: None)

        assert len(calls) == 1
        assert events[0]["will_retry"] is False

    @pytest.mark.parametrize("attempts", [0, -1])
    def test_attempts_must_be_positive(self, attempts: int) -> None:
        """Zero tries is a programming error, not a quiet no-op."""
        with pytest.raises(ValueError, match="at least 1"):
            call_with_retry(lambda: "ok", what="test.call", attempts=attempts)

    @pytest.mark.parametrize(
        ("error", "transient"),
        [
            (TimeoutError("t"), True),
            (ConnectionError("c"), True),
            (ConnectionResetError("r"), True),
            (ValueError("v"), False),
            (KeyError("k"), False),
            (RuntimeError("r"), False),
        ],
    )
    def test_is_transient_by_type(self, error: Exception, transient: bool) -> None:
        """Network trouble is transient; a logic error is not."""
        assert is_transient(error) is transient

    @pytest.mark.parametrize(
        ("status", "transient"),
        [
            (408, True),
            (429, True),
            (500, True),
            (503, True),
            (599, True),
            (400, False),
            (401, False),
            (403, False),
            (404, False),
            (422, False),
        ],
    )
    def test_is_transient_by_status(self, status: int, transient: bool) -> None:
        """A provider's status code decides: retry 408, 429 and 5xx only."""

        class ProviderError(Exception):
            status_code = status

        assert is_transient(ProviderError("x")) is transient

    def test_anthropic_connection_errors_are_transient(self) -> None:
        """The SDK's own connection error is recognised."""
        import anthropic

        # Built without its constructor: the test is about the type, and
        # the constructor's arguments belong to the SDK's HTTP library.
        error = anthropic.APIConnectionError.__new__(anthropic.APIConnectionError)
        timeout = anthropic.APITimeoutError.__new__(anthropic.APITimeoutError)

        assert is_transient(timeout) is True
        assert is_transient(error) is True


class TestRequests:
    """Every state-changing request emits one audit event with an id."""

    @pytest.fixture()
    def app(self, tmp_path: Path) -> Flask:
        """Create the app on a temporary database."""
        with patch.dict(os.environ, SECRET_ENV):
            application = create_app(db_path=str(tmp_path / "obs.db"))
        application.config["TESTING"] = True
        application.config["WEB_CSRF_ENABLED"] = False
        return application

    def _requests(self, events: list[dict[str, Any]]) -> list[dict[str, Any]]:
        """Return only the request events."""
        return [e for e in events if e["event"] == "http.request"]

    def test_every_response_carries_an_id(self, app: Flask) -> None:
        """Reads and writes both answer with a correlation id."""
        response = app.test_client().get("/health")

        assert valid_correlation_id(response.headers[CORRELATION_HEADER])

    def test_a_caller_id_is_kept(
        self, app: Flask, events: list[dict[str, Any]]
    ) -> None:
        """OpenClaw can pass its own id and find it in the audit trail."""
        response = app.test_client().post(
            "/api/v2/members",
            json={"key": "geoff", "display_name": "Geoff"},
            headers={CORRELATION_HEADER: "openclaw-run-0042"},
        )

        assert response.headers[CORRELATION_HEADER] == "openclaw-run-0042"
        assert self._requests(events)[0]["correlation_id"] == "openclaw-run-0042"

    def test_an_unsafe_caller_id_is_replaced(self, app: Flask) -> None:
        """A forged id is dropped for a generated one."""
        response = app.test_client().get(
            "/health", headers={CORRELATION_HEADER: "bad id with spaces"}
        )

        assert response.headers[CORRELATION_HEADER] != "bad id with spaces"
        assert valid_correlation_id(response.headers[CORRELATION_HEADER])

    def test_reads_are_not_audited(
        self, app: Flask, events: list[dict[str, Any]]
    ) -> None:
        """A GET changes nothing and emits no request event."""
        app.test_client().get("/health")

        assert self._requests(events) == []

    def test_every_state_changing_route_is_audited(
        self, app: Flask, events: list[dict[str, Any]]
    ) -> None:
        """Each POST, PUT, PATCH and DELETE rule in the app emits one event.

        The requests are unauthenticated and mostly rejected. That is the
        point: the audit hook sits in front of every route, whatever the
        route then does.
        """
        client = app.test_client()
        rules = [
            (method, rule)
            for rule in app.url_map.iter_rules()
            for method in sorted((rule.methods or set()) & STATE_CHANGING_METHODS)
        ]
        assert len(rules) > 20

        for method, rule in rules:
            before = len(self._requests(events))
            path = rule.rule
            for argument in rule.arguments:
                path = path.replace(f"<int:{argument}>", "1")
                path = path.replace(f"<{argument}>", "x")
                path = path.replace(f"<path:{argument}>", "x")
            with patch.dict(os.environ, SECRET_ENV):
                response = client.open(path, method=method)
            emitted = self._requests(events)[before:]

            assert len(emitted) == 1, f"{method} {rule.rule}"
            assert emitted[0]["method"] == method
            assert emitted[0]["route"] == rule.rule
            assert emitted[0]["status"] == response.status_code
            assert emitted[0]["correlation_id"] == response.headers[CORRELATION_HEADER]

    def test_the_actor_is_recorded(
        self, app: Flask, events: list[dict[str, Any]], tmp_path: Path
    ) -> None:
        """A v2 change records the member who made it, never the token."""
        from grocery_butler.members import MemberStore

        MemberStore(str(tmp_path / "obs.db")).add("geoff", "Geoff", created_by="test")

        response = Api(app).call("POST", "/items", {"name": "oat milk"})
        event = self._requests(events)[-1]

        assert response.status_code == 201
        assert event["actor"].startswith("geoff@")
        assert event["route"] == "/api/v2/items"
        assert event["status"] == 201
        assert "Bearer" not in json.dumps(events)
        assert SECRET_ENV[next(iter(SECRET_ENV))] not in json.dumps(events)

    def test_an_unmatched_path_is_audited_without_echoing_it(
        self, app: Flask, events: list[dict[str, Any]]
    ) -> None:
        """A POST to nowhere is recorded, but its raw path is not."""
        app.test_client().post("/no/such/place-with-data-in-it")
        event = self._requests(events)[-1]

        assert event["route"] == "unmatched"
        assert event["status"] == 404
        assert "place-with-data" not in json.dumps(event)

    def test_ids_do_not_leak_between_requests(self, app: Flask) -> None:
        """Each request gets its own id."""
        client = app.test_client()

        first = client.get("/health").headers[CORRELATION_HEADER]
        second = client.get("/health").headers[CORRELATION_HEADER]

        assert first != second
