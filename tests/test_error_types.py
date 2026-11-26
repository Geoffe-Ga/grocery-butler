"""The small exception classes that carry their own messages.

Each one replaced a ``raise Builtin("long message")`` (Ruff's TRY003). Callers
and older tests catch the builtin and match the text, so every class must keep
both: these tests hold the base class, the exact message, and the path that
raises it.
"""

from __future__ import annotations

from pathlib import Path
from unittest import mock

import pytest

from grocery_butler import auth_middleware, pantry_manager, prompt_loader
from grocery_butler.auth_middleware import MissingSecretError
from grocery_butler.db import adapter
from grocery_butler.db.adapter import PostgresDriverMissingError
from grocery_butler.pantry_manager import MissingRowIdError, NotAJsonArrayError
from grocery_butler.pending_actions import VanishedActionError
from grocery_butler.prompt_loader import PromptNotFoundError


@pytest.mark.parametrize(
    ("error", "base", "message"),
    [
        (
            MissingSecretError("SOME_SECRET"),
            RuntimeError,
            "SOME_SECRET not set; refusing to start auth-protected service",
        ),
        (
            PostgresDriverMissingError(),
            ImportError,
            "PostgreSQL support requires psycopg2-binary. "
            "Install it with: pip install psycopg2-binary",
        ),
        (MissingRowIdError(), RuntimeError, "Insert did not return a row id"),
        (NotAJsonArrayError(), ValueError, "Expected a JSON array"),
        (
            VanishedActionError("abc123"),
            RuntimeError,
            "pending action abc123 vanished after insert",
        ),
        (
            PromptNotFoundError("missing", Path("/p/missing.txt")),
            FileNotFoundError,
            "Prompt template not found: missing (looked in /p/missing.txt)",
        ),
    ],
)
def test_each_error_keeps_its_builtin_base_and_message(
    error: Exception, base: type[Exception], message: str
) -> None:
    """A caller catching the builtin still catches it, with the same text."""
    assert isinstance(error, base)
    assert str(error) == message


def test_an_unset_secret_raises_missing_secret_error(
    monkeypatch: pytest.MonkeyPatch,
) -> None:
    """The auth service refuses to start without its shared secret."""
    monkeypatch.delenv(auth_middleware.SECRET_ENV_VAR, raising=False)
    with pytest.raises(MissingSecretError, match=auth_middleware.SECRET_ENV_VAR):
        auth_middleware._shared_secret()


def test_a_postgres_url_without_psycopg2_raises_driver_missing() -> None:
    """A Postgres URL with no driver installed names the package to install."""
    with (
        mock.patch.object(adapter, "_HAS_PSYCOPG2", new=False),
        pytest.raises(PostgresDriverMissingError),
    ):
        adapter.create_connection("postgresql://u:p@localhost/db")


def test_a_json_object_reply_raises_not_a_json_array() -> None:
    """Claude's inventory reply must be an array, not an object."""
    with pytest.raises(NotAJsonArrayError):
        pantry_manager._parse_claude_response('{"item": "milk"}')


def test_a_missing_template_raises_prompt_not_found(tmp_path: Path) -> None:
    """The error names both the prompt and the path it looked in."""
    with (
        mock.patch.object(prompt_loader, "PROMPTS_DIR", tmp_path),
        pytest.raises(PromptNotFoundError) as caught,
    ):
        prompt_loader.load_prompt("nope")
    assert str(caught.value) == (
        f"Prompt template not found: nope (looked in {tmp_path / 'nope.txt'})"
    )
