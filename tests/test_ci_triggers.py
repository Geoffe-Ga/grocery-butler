"""When ci.yml runs, and what a run with no base to diff against checks.

CI doesn't run on every push to ``main``: a PR's own CI tests what lands, and
a nightly ``schedule`` is the backstop for skew between merges. A scheduled
run has no "before" commit, so pre-commit must check every file, and it must
never start the six-hour mutation job. Every job also names its own
``timeout-minutes``, so a hung job can't run to GitHub's six-hour limit.
"""

from __future__ import annotations

import re
from pathlib import Path
from typing import Any

import pytest
import yaml

CI = Path(__file__).resolve().parent.parent / ".github" / "workflows" / "ci.yml"
TRIGGERS = ("pull_request", "schedule", "workflow_dispatch")
# Events ci.yml doesn't answer today, but an edit could add.
OTHER_EVENTS = ("push", "merge_group", "workflow_run")
# GitHub delays, and under load drops, schedules at the top of the hour.
BUSY_MINUTES = (0, 30)
# GitHub-hosted runners stop any job at 360 minutes, so a longer timeout means nothing.
MAX_TIMEOUT = 360
WRAPPED = re.compile(r"^\$\{\{(.+)\}\}$", re.DOTALL)
EVENT_TEST = re.compile(r"^github\.event_name\s*(==|!=)\s*'([a-z_]+)'$")
CONTEXT = re.compile(r"\$\{\{\s*([\w.]+)\s*\}\}")


class NotAnEventTestError(ValueError):
    """Part of an expression that isn't a ``github.event_name`` test."""


def _ci() -> dict[Any, Any]:
    """ci.yml, parsed."""
    workflow: dict[Any, Any] = yaml.safe_load(CI.read_text(encoding="utf-8"))
    return workflow


def _triggers() -> dict[str, Any]:
    """ci.yml's ``on:`` block. YAML 1.1 reads a bare ``on`` key as True."""
    workflow = _ci()
    triggers: dict[str, Any] = workflow["on"] if "on" in workflow else workflow[True]
    return triggers


def _holds_for(expression: str, event: str) -> bool:
    """Evaluate ``github.event_name`` tests joined by ``||`` and ``&&`` for ``event``.

    Anything else in the expression raises, so no test passes on a condition
    it didn't actually read.
    """
    wrapped = WRAPPED.match(expression.strip())
    body = wrapped.group(1) if wrapped else expression
    return any(
        all(_event_test(atom.strip(), event) for atom in clause.split("&&"))
        for clause in body.split("||")
    )


def _event_test(atom: str, event: str) -> bool:
    """Evaluate one ``github.event_name == 'x'`` or ``!= 'x'`` for ``event``."""
    match = EVENT_TEST.match(atom)
    if match is None:
        raise NotAnEventTestError(atom)
    operator, name = match.groups()
    return (event == name) == (operator == "==")


def _step_env(job: str, name: str) -> str:
    """The one value a step of ``job`` gives the environment variable ``name``."""
    steps = _ci()["jobs"][job]["steps"]
    values = [step["env"][name] for step in steps if name in step.get("env", {})]
    assert len(values) == 1, values
    value: str = values[0]
    return value


def _group(event: str, ref: str) -> str:
    """ci.yml's concurrency group for a run of ``event`` on ``ref``."""
    context = {"github.workflow": "CI", "github.event_name": event, "github.ref": ref}
    group = CONTEXT.sub(lambda m: context[m.group(1)], _ci()["concurrency"]["group"])
    assert "${{" not in group, group
    return group


def test_ci_never_runs_on_a_push() -> None:
    """A merge to main starts no CI run, and nothing reads a push's ``before``."""
    assert "push" not in _triggers()
    assert "github.event.before" not in CI.read_text(encoding="utf-8")


def test_ci_runs_on_prs_to_main_nightly_and_by_hand() -> None:
    """PRs to main, the nightly schedule and manual dispatch, and nothing else."""
    triggers = _triggers()
    assert sorted(triggers) == sorted(TRIGGERS)
    assert triggers["pull_request"] == {"branches": ["main"]}


def test_ci_runs_once_a_day_off_the_busy_minutes() -> None:
    """One daily cron, at a set minute and hour other than :00 or :30."""
    schedule = _triggers()["schedule"]
    assert len(schedule) == 1, schedule
    minute, hour, *days = schedule[0]["cron"].split()
    assert days == ["*", "*", "*"]
    assert hour.isdigit() and 0 <= int(hour) <= 23
    assert minute.isdigit() and int(minute) not in BUSY_MINUTES


@pytest.mark.parametrize("event", TRIGGERS)
def test_only_a_pr_checks_just_the_changed_files(event: str) -> None:
    """A nightly or manual run has no base to diff against, so it checks every file."""
    assert _holds_for(_step_env("pre-commit", "FULL"), event) == (
        event != "pull_request"
    )


@pytest.mark.parametrize("event", TRIGGERS + OTHER_EVENTS)
def test_mutation_runs_only_when_dispatched(event: str) -> None:
    """The mutation job (up to six hours) never runs nightly or on a PR."""
    condition = _ci()["jobs"]["mutation"]["if"]
    assert _holds_for(condition, event) == (event == "workflow_dispatch")


def test_a_nightly_run_starts_every_gate_job() -> None:
    """Only mutation's ``if`` reads the event, so every gate job runs nightly."""
    jobs = _ci()["jobs"]
    by_event = [
        name for name, job in jobs.items() if "event_name" in str(job.get("if"))
    ]
    assert by_event == ["mutation"]


@pytest.mark.parametrize(
    "job", sorted(yaml.safe_load(CI.read_text(encoding="utf-8"))["jobs"])
)
def test_every_job_names_its_own_timeout(job: str) -> None:
    """A whole number of minutes from 1 to 360, the hosted-runner maximum."""
    timeout = _ci()["jobs"][job].get("timeout-minutes")
    assert isinstance(timeout, int), timeout
    assert 1 <= timeout <= MAX_TIMEOUT


def test_the_gate_needs_every_other_job_but_mutation() -> None:
    """The one check to require fails whenever a job it should guard failed."""
    jobs = _ci()["jobs"]
    gate = jobs["gate"]
    assert sorted(gate["needs"]) == sorted(set(jobs) - {"gate", "mutation"})
    assert gate["if"] == "always()"


def test_a_nightly_run_and_a_dispatch_never_cancel_each_other() -> None:
    """Both run on refs/heads/main, so the group must tell their events apart.

    Otherwise the nightly run would cancel a dispatched mutation run hours in,
    and a dispatch would cancel the nightly backstop.
    """
    main = "refs/heads/main"
    assert _group("schedule", main) != _group("workflow_dispatch", main)
    assert _ci()["concurrency"]["cancel-in-progress"] is True


def test_a_prs_newer_push_still_cancels_its_older_run() -> None:
    """Pushes to one PR share a group; two PRs never do.

    ``_group`` fails on any context but workflow, event and ref, so a group it
    builds is the same for every push to one PR.
    """
    pr5 = _group("pull_request", "refs/pull/5/merge")
    assert pr5 == _group("pull_request", "refs/pull/5/merge")
    assert pr5 != _group("pull_request", "refs/pull/6/merge")


@pytest.mark.parametrize(
    ("expression", "event", "holds"),
    [
        ("github.event_name == 'schedule'", "schedule", True),
        ("${{ github.event_name != 'pull_request' }}", "pull_request", False),
        ("github.event_name == 'a' || github.event_name == 'b'", "b", True),
        ("github.event_name != 'a' && github.event_name != 'b'", "b", False),
    ],
)
def test_event_reader_evaluates_tests_joined_by_and_and_or(
    expression: str, event: str, holds: bool
) -> None:
    """The helper above reads ``==``, ``!=``, ``||`` and ``&&`` as Actions does."""
    assert _holds_for(expression, event) is holds


def test_event_reader_refuses_anything_else() -> None:
    """An expression it can't read fails loudly instead of passing a test."""
    with pytest.raises(
        NotAnEventTestError, match=r"^github\.ref == 'refs/heads/main'$"
    ):
        _holds_for("github.ref == 'refs/heads/main'", "schedule")
