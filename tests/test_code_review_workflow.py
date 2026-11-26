"""Tests for .github/workflows/code-review.yml: what the review job posts.

The "Post review" step turns the reviewer's structured output into the PR
comment that is a merge gate (CLAUDE.md, Review Verdicts). These tests run
that step's script with a fake ``gh`` on PATH, so the guard that refuses a
placeholder review is checked without GitHub.
"""

from __future__ import annotations

import json
import os
import shutil
import subprocess  # nosec B404 - runs the workflow's own step script in a temp dir
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "code-review.yml"
BASH = shutil.which("bash") or "bash"

FAKE_GH = """#!/usr/bin/env bash
# Records every call; answers the two reads the step makes.
printf '%s\\n' "$*" >> "$GH_LOG"
if [ "$1 $2" = "pr diff" ]; then exit 0; fi
if [ "$1 $2" = "pr comment" ]; then
  shift 2
  while [ $# -gt 0 ]; do
    if [ "$1" = "--body-file" ]; then cp "$2" "$GH_BODY"; fi
    shift
  done
fi
exit 0
"""

REAL_REVIEW = "\n".join(
    [
        "## Overview",
        "",
        "Adds a small, well-tested change to the pantry's quantity parsing.",
        "",
        "## What's done well",
        "",
        "- Boundary tests at one byte over and under the cap.",
        "- Docs updated in the same change.",
        "",
        "## Critical issues blocking merge",
        "",
        "None found.",
    ]
)


def _steps() -> list[dict[str, Any]]:
    """Return the review job's steps from the workflow file."""
    workflow = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
    steps: list[dict[str, Any]] = workflow["jobs"]["claude-review"]["steps"]
    return steps


def _step(name: str) -> dict[str, Any]:
    """Return the step called ``name``."""
    return next(step for step in _steps() if step.get("name") == name)


def _post(
    tmp_path: Path, structured: dict[str, Any]
) -> subprocess.CompletedProcess[str]:
    """Run the "Post review" script against ``structured`` with a fake gh."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    gh = bin_dir / "gh"
    gh.write_text(FAKE_GH, encoding="utf-8")
    gh.chmod(0o755)
    env = {
        **os.environ,
        "PATH": f"{bin_dir}{os.pathsep}{os.environ['PATH']}",
        "GH_LOG": str(tmp_path / "gh.log"),
        "GH_BODY": str(tmp_path / "body.md"),
        "PR_NUMBER": "205",
        "REPO": "owner/repo",
        "STRUCTURED": json.dumps(structured),
    }
    return subprocess.run(  # nosec B603 - fixed argv; the script is the repo's own
        [BASH, "-e", "-c", _step("Post review")["run"]],
        cwd=tmp_path,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


def _calls(tmp_path: Path) -> str:
    """Return every gh call the step made, one per line."""
    log = tmp_path / "gh.log"
    return log.read_text(encoding="utf-8") if log.exists() else ""


def _output(**fields: Any) -> dict[str, Any]:
    """Return a structured review result, overriding ``fields``."""
    base = {
        "reviewed_pr_number": 205,
        "verdict": "LGTM",
        "verdict_rationale": "The change is tested at its boundaries and documented.",
        "review_markdown": REAL_REVIEW,
    }
    return {**base, **fields}


def test_a_real_review_is_posted_with_its_marker_and_verdict(tmp_path: Path) -> None:
    """A review with sections and a rationale becomes the gate comment."""
    result = _post(tmp_path, _output())
    assert result.returncode == 0, result.stdout + result.stderr
    body = (tmp_path / "body.md").read_text(encoding="utf-8")
    assert body.startswith("<!-- grocery-butler-review pr=205 -->\n")
    assert "## Overview" in body
    assert "\n## Verdict: LGTM\n" in body


@pytest.mark.parametrize(
    "fields",
    [
        pytest.param(
            {"review_markdown": "test", "verdict_rationale": "test"}, id="placeholder"
        ),
        pytest.param({"review_markdown": "x" * 600}, id="no-section-headings"),
        pytest.param({"review_markdown": "## Overview\n\nFine."}, id="too-short"),
        pytest.param({"verdict_rationale": "ok"}, id="no-rationale"),
    ],
)
def test_a_placeholder_review_fails_the_job_and_posts_nothing(
    tmp_path: Path, fields: dict[str, Any]
) -> None:
    """Output that isn't a review is a reviewer error: fail loudly, never post it."""
    result = _post(tmp_path, _output(**fields))
    assert result.returncode == 1
    assert "not a review" in result.stdout + result.stderr
    assert "pr comment" not in _calls(tmp_path)


def test_a_review_of_another_pr_still_fails(tmp_path: Path) -> None:
    """The existing wrong-PR guard is unchanged."""
    result = _post(tmp_path, _output(reviewed_pr_number=204))
    assert result.returncode == 1
    assert "pr comment" not in _calls(tmp_path)


def test_the_reviewer_can_read_a_large_diff_one_file_at_a_time() -> None:
    """Big PRs need per-file diffs: the base parent and `git diff` are available."""
    checkout = _step("Checkout repository")["with"]
    assert checkout["fetch-depth"] == 2
    args = _step("Run Claude Code Review")["with"]["claude_args"]
    assert "Bash(git diff:*)" in args
    prompt = _step("Run Claude Code Review")["with"]["prompt"]
    assert "git diff HEAD^1 HEAD -- <path>" in prompt
