"""Shared utilities for Claude API response parsing and common helpers.

Functions used across multiple modules: Claude response parsing and
Anthropic client creation.
"""

from __future__ import annotations

import logging
import time
from typing import TYPE_CHECKING, Any

from grocery_butler.observability import audit
from grocery_butler.retries import call_with_retry

if TYPE_CHECKING:
    from collections.abc import Callable

logger = logging.getLogger(__name__)


def extract_json_text(raw: str) -> str:
    """Extract JSON from a Claude response, stripping markdown fences.

    Args:
        raw: Raw text from Claude's response.

    Returns:
        Cleaned string ready for JSON parsing.
    """
    text = raw.strip()
    if text.startswith("```"):
        first_newline = text.index("\n")
        text = text[first_newline + 1 :]
    text = text.removesuffix("```")
    return text.strip()


def make_anthropic_client(api_key: str) -> object | None:
    """Create an Anthropic client from the given API key.

    Args:
        api_key: Anthropic API key string.

    Returns:
        Anthropic client instance or None on import failure.
    """
    try:
        import anthropic

        # Retries are ours (create_message), so each attempt is bounded and
        # shows in the audit trail. The SDK's own are turned off to avoid
        # multiplying the two.
        client = anthropic.Anthropic(api_key=api_key, max_retries=0)
    except Exception:
        logger.warning("Anthropic client unavailable; Claude features disabled")
        return None
    else:
        return client


def create_message(
    client: Any,
    *,
    what: str,
    sleep: Callable[[float], None] = time.sleep,
    **request: Any,
) -> Any:
    """Send one Messages API request, retrying a passing fault.

    Args:
        client: An Anthropic SDK client.
        what: A name for the audit trail, e.g. ``model.meal_parse``.
        sleep: Waits between tries; tests pass a recorder.
        **request: Arguments for ``client.messages.create``.

    Returns:
        The SDK's response.

    Raises:
        RetriesExhaustedError: If every attempt failed with a passing fault.
        Exception: Any other failure, as the SDK raised it.
    """
    return call_with_retry(
        lambda: client.messages.create(**request), what=what, sleep=sleep
    )


def extraction_failed(stage: str, reason: str) -> None:
    """Record that a model reply could not be turned into structured rows.

    Writes an ``extraction.failed`` audit event, which the metrics count.
    Only the stage and a short reason are recorded, never the prompt or
    the reply.

    Args:
        stage: Which extraction, e.g. ``meal_parse``.
        reason: Why it failed, e.g. ``invalid_json`` or ``call_failed``.
    """
    audit("extraction.failed", stage=stage, reason=reason)
