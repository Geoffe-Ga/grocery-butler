"""Behavior of the shell and helper scripts the quality gates run.

Each test copies a script into a throwaway project and puts stub tools first
on ``PATH``, so it checks the script's own logic and nothing else: which IDs
pip-audit is told to ignore, which files pre-commit is asked to check, and
where the branch-coverage floor sits.
"""

from __future__ import annotations

import importlib.util
import json
import os
import shutil
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path
from typing import TYPE_CHECKING

import pytest

if TYPE_CHECKING:
    from types import ModuleType

SCRIPTS = Path(__file__).resolve().parent.parent / "scripts"
BASH = shutil.which("bash") or "/bin/bash"
GIT = shutil.which("git") or "/usr/bin/git"


def _stub(bin_dir: Path, name: str, body: str) -> None:
    """Write an executable stub command ``name`` that runs ``body``."""
    path = bin_dir / name
    path.write_text(f"#!/usr/bin/env bash\n{body}\n", encoding="utf-8")
    path.chmod(path.stat().st_mode | stat.S_IXUSR)


def _project(tmp_path: Path, *scripts: str) -> Path:
    """A throwaway project with copies of ``scripts`` in its scripts/ folder."""
    project = tmp_path / "project"
    (project / "scripts").mkdir(parents=True)
    for name in scripts:
        shutil.copy2(SCRIPTS / name, project / "scripts" / name)
    return project


def _env(bin_dir: Path) -> dict[str, str]:
    """An environment with the stubs first on PATH and a venv already active."""
    env = dict(os.environ)
    env["PATH"] = f"{bin_dir}{os.pathsep}{env['PATH']}"
    env["VIRTUAL_ENV"] = str(bin_dir)
    return env


def _run(
    project: Path, script: str, env: dict[str, str]
) -> subprocess.CompletedProcess[str]:
    """Run ``scripts/<script>`` from ``project``."""
    return subprocess.run(
        [BASH, str(project / "scripts" / script)],
        cwd=project,
        env=env,
        capture_output=True,
        text=True,
        check=False,
    )


# ---------------------------------------------------------------------------
# security.sh: the known-vulnerabilities file
# ---------------------------------------------------------------------------


def _security(
    tmp_path: Path, known: str | None, audit_exit: int = 0
) -> tuple[subprocess.CompletedProcess[str], str]:
    """Run security.sh with stub tools; return the result and pip-audit's argv."""
    project = _project(tmp_path, "security.sh", "activate-venv.sh")
    (project / "requirements-dev.txt").write_text("", encoding="utf-8")
    if known is not None:
        (project / ".pip-audit-known-vulnerabilities").write_text(
            known, encoding="utf-8"
        )
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir()
    args_file = tmp_path / "pip-audit-args"
    _stub(bin_dir, "bandit", "exit 0")
    _stub(bin_dir, "pip-audit", f'echo "$*" > "{args_file}"\nexit {audit_exit}')
    result = _run(project, "security.sh", _env(bin_dir))
    args = args_file.read_text(encoding="utf-8").strip() if args_file.exists() else ""
    return result, args


def test_security_ignores_each_listed_id_and_skips_comments(tmp_path: Path) -> None:
    """IDs are trimmed, inline comments dropped, and comment lines skipped."""
    result, args = _security(
        tmp_path,
        "# header comment\n\n  GHSA-aaaa-bbbb   # why it can't be fixed\nPYSEC-1\n",
    )
    assert result.returncode == 0, result.stderr
    assert args == (
        "-r requirements-dev.txt --ignore-vuln GHSA-aaaa-bbbb --ignore-vuln PYSEC-1"
    )


@pytest.mark.parametrize("known", [None, "# nothing known\n"])
def test_security_audits_the_pins_with_no_ignores(
    tmp_path: Path, known: str | None
) -> None:
    """With no file, or one holding only comments, nothing is ignored."""
    result, args = _security(tmp_path, known)
    assert result.returncode == 0, result.stderr
    assert args == "-r requirements-dev.txt"


def test_security_fails_when_pip_audit_finds_something(tmp_path: Path) -> None:
    """A pip-audit finding fails the script."""
    result, _ = _security(tmp_path, None, audit_exit=1)
    assert result.returncode == 1
    assert "pip-audit found issues" in result.stderr


# ---------------------------------------------------------------------------
# precommit-changed.sh: which files pre-commit checks
# ---------------------------------------------------------------------------


def _git(repo: Path, *args: str) -> str:
    """Run git in ``repo`` and return its stdout."""
    return subprocess.run(
        [GIT, *args],
        cwd=repo,
        capture_output=True,
        text=True,
        check=True,
    ).stdout.strip()


@pytest.fixture
def repo(tmp_path: Path) -> Path:
    """A git repo whose first commit is the base a change is diffed against."""
    project = _project(tmp_path, "precommit-changed.sh")
    for name in (".pre-commit-config.yaml", "requirements-dev.txt", "pyproject.toml"):
        (project / name).write_text("v1\n", encoding="utf-8")
    (project / "app.py").write_text("x = 1\n", encoding="utf-8")
    _git(project, "init", "-q", "-b", "main")
    _git(project, "config", "user.email", "test@example.com")
    _git(project, "config", "user.name", "Test")
    _git(project, "add", "-A")
    _git(project, "commit", "-q", "-m", "base")
    _git(project, "branch", "base")
    return project


def _precommit(repo: Path, tmp_path: Path, **env_vars: str) -> str:
    """Run precommit-changed.sh with a stub pre-commit; return its argv."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    _stub(bin_dir, "pre-commit", 'echo "$*"')
    env = _env(bin_dir) | {"BASE_REF": "base", "FULL": "false"} | env_vars
    result = _run(repo, "precommit-changed.sh", env)
    assert result.returncode == 0, result.stderr
    return result.stdout.strip().splitlines()[-1]


def _change(repo: Path, name: str) -> None:
    """Commit a change to ``name`` on top of the base."""
    (repo / name).write_text("v2\n", encoding="utf-8")
    _git(repo, "commit", "-q", "-am", f"change {name}")


def test_a_code_change_checks_only_the_changed_files(
    repo: Path, tmp_path: Path
) -> None:
    """pre-commit gets the merge-base and HEAD, not --all-files."""
    _change(repo, "app.py")
    base = _git(repo, "rev-parse", "base")
    assert _precommit(repo, tmp_path) == f"run --from-ref {base} --to-ref HEAD"


@pytest.mark.parametrize(
    "name", [".pre-commit-config.yaml", "requirements-dev.txt", "pyproject.toml"]
)
def test_a_hook_config_or_tool_change_checks_every_file(
    repo: Path, tmp_path: Path, name: str
) -> None:
    """The hooks' config, tool pins and tool settings can change any file's verdict."""
    _change(repo, name)
    assert _precommit(repo, tmp_path) == "run --all-files"


def test_full_checks_every_file(repo: Path, tmp_path: Path) -> None:
    """A nightly or manual run (FULL=true) has no base and checks everything."""
    _change(repo, "app.py")
    assert _precommit(repo, tmp_path, FULL="true") == "run --all-files"


def test_an_unknown_base_checks_every_file(repo: Path, tmp_path: Path) -> None:
    """With no usable base, it falls back to every file rather than none."""
    _change(repo, "app.py")
    assert _precommit(repo, tmp_path, BASE_REF="no-such-ref") == "run --all-files"


# ---------------------------------------------------------------------------
# check_branch_coverage.py: the branch-coverage floor
# ---------------------------------------------------------------------------


def _load(name: str) -> ModuleType:
    """Import ``scripts/<name>.py`` by path (scripts/ isn't a package)."""
    spec = importlib.util.spec_from_file_location(name, SCRIPTS / f"{name}.py")
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    return module


def _coverage_json(tmp_path: Path, covered: int, branches: int) -> Path:
    """A minimal pytest-cov JSON report with the given branch counts."""
    path = tmp_path / "coverage.json"
    totals = {"covered_branches": covered, "num_branches": branches}
    path.write_text(json.dumps({"totals": totals}), encoding="utf-8")
    return path


def test_the_branch_floor_is_ninety_percent() -> None:
    """CLAUDE.md's standard: 90% branch coverage."""
    assert _load("check_branch_coverage").MIN_BRANCH_COVERAGE == 90.0


@pytest.mark.parametrize(
    ("covered", "branches", "passes"),
    [(900, 1000, True), (950, 1000, True), (8999, 10000, False), (0, 0, True)],
)
def test_branch_coverage_passes_at_or_above_the_floor(
    tmp_path: Path, covered: int, branches: int, passes: bool
) -> None:
    """At the floor passes; a hair below fails; no branches at all passes."""
    result = subprocess.run(
        [
            sys.executable,
            str(SCRIPTS / "check_branch_coverage.py"),
            str(_coverage_json(tmp_path, covered, branches)),
        ],
        capture_output=True,
        text=True,
        check=False,
    )
    assert (result.returncode == 0) is passes, result.stdout + result.stderr


# ---------------------------------------------------------------------------
# analyze_mutations.py: the file filter
# ---------------------------------------------------------------------------


@pytest.fixture
def mutmut_cache(tmp_path: Path) -> Path:
    """A tiny mutmut cache: two files, one killed and one survived mutant each."""
    path = tmp_path / ".mutmut-cache"
    with sqlite3.connect(path) as conn:
        conn.executescript(
            """
            CREATE TABLE SourceFile (id INTEGER PRIMARY KEY, filename TEXT);
            CREATE TABLE Line (
                id INTEGER PRIMARY KEY, sourcefile INT, line TEXT, line_number INT
            );
            CREATE TABLE Mutant (
                id INTEGER PRIMARY KEY, line INT, status TEXT, idx INT
            );
            INSERT INTO SourceFile VALUES (1, 'grocery_butler/cli.py'),
                                          (2, 'grocery_butler/api.py');
            INSERT INTO Line VALUES (1, 1, 'a', 1), (2, 2, 'b', 2);
            INSERT INTO Mutant VALUES (1, 1, 'ok_killed', 0), (2, 2, 'bad_survived', 0),
                                      (3, 2, 'ok_killed', 1);
            """
        )
    return path


def test_analyze_mutations_counts_every_file_without_a_filter(
    mutmut_cache: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """No filter: all three mutants, from both files."""
    _load("analyze_mutations").analyze_cache(mutmut_cache)
    out = capsys.readouterr().out
    assert "Total mutants: 3" in out
    assert "grocery_butler/api.py" in out


def test_analyze_mutations_filters_by_file_name(
    mutmut_cache: Path, capsys: pytest.CaptureFixture[str]
) -> None:
    """A filter keeps only mutants in files whose path ends with it."""
    _load("analyze_mutations").analyze_cache(mutmut_cache, filter_file="cli.py")
    out = capsys.readouterr().out
    assert "Total mutants: 1" in out
    assert "bad_survived" not in out
