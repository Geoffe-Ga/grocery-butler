"""Anti-drift guard tests for the manual E2E pre-ship checklist.

Keeps ``docs/MANUAL_E2E_CHECKLIST.md`` truthful by cross-checking every
``/api/v1/...`` endpoint and ``python -m grocery_butler <subcommand>``
invocation documented there against the real Flask ``url_map`` and the
real CLI argument parser. The README has already drifted from the real
CLI once (``stock show``, ``order review``, ``recipes list``, and
``pantry list`` are not real subcommands argparse accepts) -- these
tests exist so the new checklist cannot repeat that mistake.
"""

from __future__ import annotations

import argparse
import re
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from flask import Flask

from grocery_butler.app import create_app
from grocery_butler.cli import _build_parser

#: Path to the checklist doc, resolved relative to this test file so it
#: works regardless of the current working directory.
DOC_PATH = Path(__file__).resolve().parent.parent / "docs" / "MANUAL_E2E_CHECKLIST.md"

#: Fenced code blocks: ```lang\n...\n``` (language tag optional).
_FENCED_BLOCK_RE = re.compile(r"```[a-zA-Z0-9_+-]*\n(.*?)```", re.DOTALL)

#: Inline code spans: `...`
_CODE_SPAN_RE = re.compile(r"`([^`\n]+)`")

#: Concrete /api/v1/... path tokens embedded in code text.
_API_PATH_RE = re.compile(r"/api/v1/[A-Za-z0-9_\-/{}<>:]*")

#: `python -m grocery_butler <subcommand> ...` or `grocery-butler <subcommand> ...`
_CLI_INVOCATION_RE = re.compile(
    r"(?:python -m grocery_butler|grocery-butler)\s+([a-zA-Z][\w-]*)"
)

#: A path segment that stands in for a dynamic parameter (e.g. <int:id>,
#: {id}, :id, or a bare numeric example id) rather than a literal token.
_PLACEHOLDER_SEGMENT_RE = re.compile(r"^[<{:]|[>}]$|^\d+$")

#: Required-content strings the checklist must positively document: the
#: staged-confirmation API surface, dry-run support, and the blocker
#: issues gating ship, per the Architecture Plan for issue #32.
REQUIRED_CONTENT_STRINGS: tuple[str, ...] = (
    "/api/v1",
    "order/preview",
    "order/submit",
    "actions/confirm",
    "actions/deny",
    "pending_actions",
    "--dry-run",
    "#57",
    "#58",
    "#59",
    "#60",
    "#64",
    "#73",
)


# ---------------------------------------------------------------------------
# Extraction helpers
# ---------------------------------------------------------------------------


def _read_doc() -> str:
    """Read the checklist doc text, failing clearly if it does not exist.

    Returns:
        The full text content of docs/MANUAL_E2E_CHECKLIST.md.
    """
    if not DOC_PATH.exists():
        pytest.fail(
            f"{DOC_PATH} does not exist yet. Create the manual E2E "
            "pre-ship checklist (issue #32) before this guard can pass."
        )
    return DOC_PATH.read_text(encoding="utf-8")


def _code_snippets(text: str) -> list[str]:
    """Extract fenced code block bodies and inline code span contents.

    Restricting extraction to code text (rather than scanning the whole
    document with a loose regex) keeps this guard tolerant of prose that
    mentions endpoints or commands descriptively without asserting they
    are literally invocable.

    Args:
        text: Full markdown document text.

    Returns:
        List of code snippet strings found in the document.
    """
    snippets = _FENCED_BLOCK_RE.findall(text)
    snippets.extend(_CODE_SPAN_RE.findall(text))
    return snippets


def _normalize_segments(path: str) -> tuple[str, ...]:
    """Normalize a URL path into segments, collapsing dynamic ones to '*'.

    Args:
        path: A URL path, possibly with a query string, trailing slash,
            or Flask/doc-style dynamic segments (``<int:id>``, ``{id}``,
            ``:id``, or a bare numeric example like ``1``).

    Returns:
        Tuple of normalized path segments.
    """
    clean = path.split("?", 1)[0].rstrip("/")
    segments = [seg for seg in clean.split("/") if seg]
    return tuple(
        "*" if _PLACEHOLDER_SEGMENT_RE.search(seg) else seg for seg in segments
    )


def _extract_api_paths(text: str) -> set[str]:
    """Extract every distinct /api/v1/... path token from code snippets.

    Args:
        text: Full markdown document text.

    Returns:
        Set of raw /api/v1/... path strings found in code text.
    """
    paths: set[str] = set()
    for snippet in _code_snippets(text):
        for match in _API_PATH_RE.findall(snippet):
            trimmed = match.rstrip(".,;:)")
            if trimmed and trimmed != "/api/v1":
                paths.add(trimmed)
    return paths


def _extract_cli_subcommands(text: str) -> set[str]:
    """Extract every distinct CLI subcommand invoked in code snippets.

    Args:
        text: Full markdown document text.

    Returns:
        Set of subcommand name strings found in code text.
    """
    subcommands: set[str] = set()
    for snippet in _code_snippets(text):
        subcommands.update(_CLI_INVOCATION_RE.findall(snippet))
    return subcommands


def _real_api_path_segment_sets(app: Flask) -> set[tuple[str, ...]]:
    """Return normalized segment tuples for every real /api/v1 rule.

    Args:
        app: Flask application instance built by create_app.

    Returns:
        Set of normalized segment tuples, one per registered /api/v1 rule.
    """
    return {
        _normalize_segments(rule.rule)
        for rule in app.url_map.iter_rules()
        if rule.rule.startswith("/api/v1")
    }


def _cli_subcommand_choices() -> set[str]:
    """Return the set of registered top-level CLI subcommand names.

    Returns:
        Set of subcommand strings registered on the real CLI parser.
    """
    parser = _build_parser()
    for action in parser._actions:
        if isinstance(action, argparse._SubParsersAction):
            return set(action.choices)
    return set()  # pragma: no cover - _build_parser always registers subcommands


# ---------------------------------------------------------------------------
# Fixtures
# ---------------------------------------------------------------------------


@pytest.fixture()
def app(tmp_path: Path) -> Flask:
    """Create a Flask app bound to a temp DB path so no real DB file is made.

    Args:
        tmp_path: Pytest-provided temporary directory.

    Returns:
        Configured Flask application instance.
    """
    db_path = str(tmp_path / "checklist_guard.db")
    application = create_app(db_path=db_path)
    application.config["TESTING"] = True
    return application


# ---------------------------------------------------------------------------
# TestDocExists
# ---------------------------------------------------------------------------


class TestDocExists:
    """The checklist file must exist and contain real content before ship."""

    def test_checklist_file_exists_and_nonempty(self) -> None:
        """Test docs/MANUAL_E2E_CHECKLIST.md exists and is non-empty."""
        assert DOC_PATH.exists(), (
            f"{DOC_PATH} is missing. Create the manual E2E pre-ship "
            "checklist (issue #32) before this guard can validate it."
        )
        text = DOC_PATH.read_text(encoding="utf-8")
        assert text.strip(), f"{DOC_PATH} exists but is empty."


# ---------------------------------------------------------------------------
# TestApiEndpointsMatchUrlMap
# ---------------------------------------------------------------------------


class TestApiEndpointsMatchUrlMap:
    """Every /api/v1 path mentioned in the doc must be a real Flask rule."""

    def test_every_documented_endpoint_exists_on_url_map(self, app: Flask) -> None:
        """Test each documented /api/v1 path matches a real url_map rule."""
        text = _read_doc()
        documented_paths = _extract_api_paths(text)
        assert documented_paths, (
            "No /api/v1/... paths found in code spans/blocks -- expected "
            "the checklist to document at least one real endpoint."
        )
        real_segment_sets = _real_api_path_segment_sets(app)
        unmatched = sorted(
            path
            for path in documented_paths
            if _normalize_segments(path) not in real_segment_sets
        )
        assert not unmatched, (
            "Doc references /api/v1 paths that do not exist on the real "
            f"Flask url_map: {unmatched}. Real routes are defined in "
            "grocery_butler/api.py."
        )


# ---------------------------------------------------------------------------
# TestCliSubcommandsAreReal
# ---------------------------------------------------------------------------


class TestCliSubcommandsAreReal:
    """Every CLI subcommand mentioned in the doc must be real argparse."""

    def test_every_documented_subcommand_is_registered(self) -> None:
        """Test each `python -m grocery_butler <cmd>` subcommand is real."""
        text = _read_doc()
        documented_subcommands = _extract_cli_subcommands(text)
        assert documented_subcommands, (
            "No `python -m grocery_butler <subcommand>` invocations found "
            "-- expected the checklist to document real CLI usage."
        )
        real_subcommands = _cli_subcommand_choices()
        unmatched = sorted(documented_subcommands - real_subcommands)
        assert not unmatched, (
            f"Doc references CLI subcommands argparse does not register: "
            f"{unmatched}. Real subcommands: {sorted(real_subcommands)} "
            "(see grocery_butler/cli.py _build_parser)."
        )


# ---------------------------------------------------------------------------
# TestRequiredContentPresent
# ---------------------------------------------------------------------------


class TestRequiredContentPresent:
    """The checklist must positively document the new ship-gate surface."""

    @pytest.mark.parametrize("required", REQUIRED_CONTENT_STRINGS)
    def test_doc_contains_required_string(self, required: str) -> None:
        """Test the checklist mentions each required ship-gate string."""
        text = _read_doc()
        assert required in text, (
            f"docs/MANUAL_E2E_CHECKLIST.md is missing required content: {required!r}"
        )
