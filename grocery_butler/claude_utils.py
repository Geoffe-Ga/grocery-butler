"""Shared utilities for Claude API response parsing and common helpers.

Functions used across multiple modules: Claude response parsing and
Anthropic client creation.
"""

from __future__ import annotations

import logging

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

        client = anthropic.Anthropic(api_key=api_key)
    except Exception:
        logger.warning("Anthropic client unavailable; Claude features disabled")
        return None
    else:
        return client
