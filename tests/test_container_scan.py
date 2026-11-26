"""Tests for the container scan script and its workflow (issue #138).

The script is run for real with a stub ``docker`` first on ``PATH``, so
these check the script's own logic: what it builds, how it scans, and what
it does when either step fails. No image is built and nothing is scanned.
"""

from __future__ import annotations

import os
import shutil
import stat
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

ROOT = Path(__file__).resolve().parent.parent
SCRIPT = ROOT / "scripts" / "container-scan.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "container-scan.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"


def _stub_docker(bin_dir: Path, *, build: int = 0, scan: int = 0) -> Path:
    """Write a fake ``docker`` that logs its arguments and exits as told."""
    log = bin_dir / "docker.log"
    stub = bin_dir / "docker"
    stub.write_text(
        "#!/usr/bin/env bash\n"
        f'echo "$*" >> "{log}"\n'
        f'if [[ "$1" == "build" ]]; then exit {build}; fi\n'
        f'if [[ "$1" == "run" ]]; then exit {scan}; fi\n'
        "exit 0\n",
        encoding="utf-8",
    )
    stub.chmod(stub.stat().st_mode | stat.S_IXUSR)
    return log


def _run(path: str) -> subprocess.CompletedProcess[str]:
    """Run the scan script with a given PATH."""
    return subprocess.run(
        [str(SCRIPT)],
        env={"PATH": path, "HOME": os.environ.get("HOME", "")},
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


def _with_stub(tmp_path: Path) -> str:
    """Return a PATH with the stub directory first."""
    return f"{tmp_path}{os.pathsep}/usr/bin{os.pathsep}/bin"


class TestScript:
    """What the script builds, how it scans, and how it fails."""

    def test_a_clean_scan_passes(self, tmp_path: Path) -> None:
        """Build, then scan, then say so."""
        log = _stub_docker(tmp_path)

        result = _run(_with_stub(tmp_path))

        calls = log.read_text().splitlines()
        assert result.returncode == 0, result.stderr
        assert "No fixable HIGH,CRITICAL findings" in result.stdout
        assert len(calls) == 2
        assert calls[0].startswith("build ")
        assert "--tag grocery-butler:container-scan" in calls[0]
        assert calls[1].startswith("run --rm ")

    def test_the_scan_fails_on_fixable_high_and_critical_only(
        self, tmp_path: Path
    ) -> None:
        """The scanner is told the severities, to skip unfixed, and to exit 1."""
        log = _stub_docker(tmp_path)

        _run(_with_stub(tmp_path))

        scan = log.read_text().splitlines()[1]
        assert " image " in scan
        assert "--severity HIGH,CRITICAL" in scan
        assert "--ignore-unfixed" in scan
        assert "--exit-code 1" in scan
        assert scan.endswith("grocery-butler:container-scan")

    def test_the_scanner_is_pinned_to_a_version(self, tmp_path: Path) -> None:
        """Never ``latest``: the scan must be the same on every machine."""
        log = _stub_docker(tmp_path)

        _run(_with_stub(tmp_path))

        scan = log.read_text().splitlines()[1]
        image = next(part for part in scan.split() if "trivy" in part)
        tag = image.rsplit(":", 1)[1]
        assert image.startswith("ghcr.io/aquasecurity/trivy:")
        assert tag != "latest"
        assert tag.replace(".", "").isdigit()

    def test_findings_fail_the_script(self, tmp_path: Path) -> None:
        """A scanner that exits non-zero fails the run with a clear line."""
        _stub_docker(tmp_path, scan=1)

        result = _run(_with_stub(tmp_path))

        assert result.returncode == 1
        assert "fixable HIGH,CRITICAL findings" in result.stderr
        assert "No fixable" not in result.stdout

    def test_a_failed_build_is_not_scanned(self, tmp_path: Path) -> None:
        """If the image doesn't build there is nothing to scan."""
        log = _stub_docker(tmp_path, build=1)

        result = _run(_with_stub(tmp_path))

        assert result.returncode == 1
        assert "The image did not build" in result.stderr
        assert len(log.read_text().splitlines()) == 1

    def test_missing_docker_is_a_clear_error(self, tmp_path: Path) -> None:
        """Without docker the script says what to install, and never passes."""
        for tool in ("bash", "dirname"):
            found = shutil.which(tool)
            assert found is not None
            (tmp_path / tool).symlink_to(found)

        result = _run(str(tmp_path))

        assert result.returncode == 2
        assert "docker is not installed" in result.stderr


class TestWorkflow:
    """The scan runs nightly and on image changes, and never gates a merge."""

    @pytest.fixture()
    def workflow(self) -> dict[Any, Any]:
        """Load the container scan workflow."""
        loaded: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        return loaded

    def test_it_runs_nightly_by_hand_and_on_image_changes(
        self, workflow: dict[Any, Any]
    ) -> None:
        """Three triggers, and the pull request one is limited by path."""
        triggers = workflow[True]

        assert set(triggers) == {"pull_request", "schedule", "workflow_dispatch"}
        assert {"Dockerfile", "requirements.txt", "scripts/container-scan.sh"} <= set(
            triggers["pull_request"]["paths"]
        )
        assert "push" not in triggers

    def test_the_nightly_run_avoids_the_busy_minutes(
        self, workflow: dict[Any, Any]
    ) -> None:
        """Once a day, off :00 and :30, and not when the CI nightly starts."""
        (entry,) = workflow[True]["schedule"]
        minute, hour, *rest = entry["cron"].split()
        ci_cron = yaml.safe_load(CI.read_text(encoding="utf-8"))[True]["schedule"][0][
            "cron"
        ]

        assert rest == ["*", "*", "*"]
        assert minute not in {"0", "00", "30"}
        assert hour.isdigit()
        assert entry["cron"] != ci_cron

    def test_the_job_runs_the_script_and_nothing_else(
        self, workflow: dict[Any, Any]
    ) -> None:
        """The rule lives in the script, not in the workflow."""
        (job,) = workflow["jobs"].values()
        runs = [step["run"] for step in job["steps"] if "run" in step]

        assert runs == ["./scripts/container-scan.sh"]
        assert isinstance(job["timeout-minutes"], int)
        assert workflow["permissions"] == {"contents": "read"}

    def test_the_ci_gate_does_not_depend_on_the_scan(self) -> None:
        """A new CVE in the base image must not block unrelated merges."""
        ci = yaml.safe_load(CI.read_text(encoding="utf-8"))

        assert "scan" not in ci["jobs"]
        assert "container-scan" not in CI.read_text(encoding="utf-8")
        assert ci["jobs"]["gate"]["needs"] == ["python", "pre-commit"]
