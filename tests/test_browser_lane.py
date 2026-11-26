"""The browser tests are wired into the gate, and can't quietly drop out.

``tests/browser`` drives the dashboard in a real Chromium (ADR 0022). They
gate merges, so three things must hold: the tool is pinned, CI installs
the browser before the gate runs, and a missing browser fails the run
instead of skipping it.
"""

from __future__ import annotations

import os
import re
import subprocess
import sys
from pathlib import Path

import yaml

ROOT = Path(__file__).resolve().parent.parent
REQUIREMENTS = (ROOT / "requirements-dev.txt").read_text(encoding="utf-8")
TEST_SCRIPT = (ROOT / "scripts" / "test.sh").read_text(encoding="utf-8")
INSTALL_SCRIPT = ROOT / "scripts" / "install-browser.sh"
CI = yaml.safe_load((ROOT / ".github" / "workflows" / "ci.yml").read_text("utf-8"))


def test_playwright_is_pinned_to_an_exact_version() -> None:
    """One browser build for everyone: the package decides which Chromium."""
    assert re.search(r"^playwright==\d+\.\d+\.\d+$", REQUIREMENTS, re.MULTILINE)


def test_ci_installs_the_browser_before_the_gate_runs() -> None:
    """The Python gate has the install step, and it comes first."""
    runs = [step.get("run", "") for step in CI["jobs"]["python"]["steps"]]
    install = next(i for i, run in enumerate(runs) if "install-browser.sh" in run)
    gate = next(i for i, run in enumerate(runs) if "check-all.sh" in run)

    assert install < gate
    assert runs[install] == "./scripts/install-browser.sh --with-deps"


def test_the_unit_lane_leaves_the_browser_tests_to_the_full_run() -> None:
    """Quick local runs need no browser; the coverage step runs everything."""
    assert '-m "not integration and not e2e and not browser"' in TEST_SCRIPT
    assert '-m "browser"' in TEST_SCRIPT


def test_the_install_script_rejects_an_unknown_argument() -> None:
    """A typo is a usage error, not an install of something unexpected."""
    result = subprocess.run(
        [str(INSTALL_SCRIPT), "--everything"],
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )

    assert result.returncode == 2
    assert "Usage:" in result.stderr


def test_a_missing_browser_fails_the_run_and_says_how_to_fix_it(
    tmp_path: Path,
) -> None:
    """Pointed at a browser that isn't there, the lane fails; it never skips."""
    result = subprocess.run(
        [
            sys.executable,
            "-m",
            "pytest",
            "tests/browser",
            "-q",
            "--no-cov",
            "-p",
            "no:cacheprovider",
            "-k",
            "test_the_menu_button_toggles_the_links",
        ],
        cwd=ROOT,
        env={
            **os.environ,
            "BROWSER_TESTS_CHROMIUM": str(tmp_path / "no-such-chromium"),
        },
        capture_output=True,
        text=True,
        check=False,
        timeout=120,
    )

    assert result.returncode == 1, result.stdout
    assert "./scripts/install-browser.sh" in result.stdout
    assert "skipped" not in result.stdout
    assert " passed" not in result.stdout
