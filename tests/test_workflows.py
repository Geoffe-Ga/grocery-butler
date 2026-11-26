"""Guards over every GitHub Actions workflow in .github/workflows/.

Every job names its own ``timeout-minutes``. Without one, a hung job runs to
GitHub's six-hour limit, and every minute of it is billed.
"""

from __future__ import annotations

from pathlib import Path
from typing import Any

import pytest
import yaml

WORKFLOWS = Path(__file__).resolve().parent.parent / ".github" / "workflows"
WORKFLOW_FILES = sorted(WORKFLOWS.glob("*.yml"))
# GitHub-hosted runners stop any job at 360 minutes, so a longer timeout means nothing.
MAX_TIMEOUT = 360


def _timeout_problems(text: str) -> list[str]:
    """Name each job whose ``timeout-minutes`` is missing or not 1 to 360."""
    jobs: dict[str, Any] = yaml.safe_load(text)["jobs"]
    problems = []
    for name, job in jobs.items():
        timeout = job.get("timeout-minutes")
        usable = isinstance(timeout, int) and not isinstance(timeout, bool)
        if not usable or not 1 <= timeout <= MAX_TIMEOUT:
            problems.append(f"{name}: {timeout!r}")
    return problems


def test_there_are_workflows_to_check() -> None:
    """The glob finds the workflows, so the guard below can't pass vacuously."""
    names = {path.name for path in WORKFLOW_FILES}
    assert {"ci.yml", "code-review.yml", "claude.yml"} <= names


@pytest.mark.parametrize("path", WORKFLOW_FILES, ids=lambda path: path.name)
def test_every_job_has_a_timeout(path: Path) -> None:
    """Each job in each workflow stops itself well before GitHub's limit."""
    assert _timeout_problems(path.read_text(encoding="utf-8")) == []


@pytest.mark.parametrize(
    ("job", "problem"),
    [
        pytest.param({}, "build: None", id="missing"),
        pytest.param({"timeout-minutes": 0}, "build: 0", id="zero"),
        pytest.param({"timeout-minutes": 361}, "build: 361", id="over-the-limit"),
        pytest.param({"timeout-minutes": "10"}, "build: '10'", id="a-string"),
        pytest.param({"timeout-minutes": True}, "build: True", id="a-boolean"),
    ],
)
def test_an_unusable_timeout_is_named(job: dict[str, Any], problem: str) -> None:
    """The helper names the job and the value it found."""
    text = yaml.safe_dump({"jobs": {"build": {"runs-on": "x", **job}}})
    assert _timeout_problems(text) == [problem]


@pytest.mark.parametrize("minutes", [1, 15, MAX_TIMEOUT])
def test_a_usable_timeout_passes(minutes: int) -> None:
    """Whole minutes from 1 to 360 pass."""
    text = yaml.safe_dump({"jobs": {"build": {"timeout-minutes": minutes}}})
    assert _timeout_problems(text) == []
