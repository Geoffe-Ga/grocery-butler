"""Tests for grocery_butler.claude_utils module."""

from __future__ import annotations

import logging
from unittest.mock import patch

from grocery_butler.claude_utils import (
    extract_json_text,
    make_anthropic_client,
)


class TestExtractJsonText:
    """Tests for extract_json_text helper."""

    def test_plain_json(self):
        """Test plain JSON passes through."""
        assert extract_json_text('{"a": 1}') == '{"a": 1}'

    def test_strips_markdown_fences(self):
        """Test markdown code fences are removed."""
        result = extract_json_text('```json\n{"a": 1}\n```')
        assert result == '{"a": 1}'

    def test_strips_whitespace(self):
        """Test surrounding whitespace is stripped."""
        assert extract_json_text("  {}\n  ") == "{}"


class TestMakeAnthropicClient:
    """Tests for make_anthropic_client helper."""

    def test_returns_none_on_import_error(self):
        """Test returns None when anthropic import fails."""
        with patch.dict("sys.modules", {"anthropic": None}):
            result = make_anthropic_client("fake-key")
            assert result is None

    def test_logs_warning_on_failure(self, caplog):
        """Test warning is logged on failure."""
        with patch.dict("sys.modules", {"anthropic": None}):
            with caplog.at_level(logging.WARNING, logger="grocery_butler.claude_utils"):
                make_anthropic_client("fake-key")
            assert "Anthropic client unavailable" in caplog.text
