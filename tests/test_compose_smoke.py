"""Tests for the compose smoke script and its workflow (issue #225).

The script is run for real with stub ``docker`` and ``curl`` first on
``PATH``, so these check the script's own logic: what it starts, what it
asks for, and that each thing going wrong fails the run and says why.
Nothing is built or started here. The real run is the workflow's.
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
SCRIPT = ROOT / "scripts" / "compose-smoke.sh"
WORKFLOW = ROOT / ".github" / "workflows" / "compose-smoke.yml"
CI = ROOT / ".github" / "workflows" / "ci.yml"
SCAN = ROOT / ".github" / "workflows" / "container-scan.yml"

DOCKER_STUB = r"""#!/usr/bin/env bash
# A fake docker: logs its arguments, and answers the few calls that matter.
echo "$*" >> "$STUB_DIR/docker.log"
case " $* " in
    *" up "*)
        [[ "$*" == *"--build"* ]] && exit "${STUB_UP:-0}"
        exit "${STUB_REUP:-0}" ;;
    *" exec "*) echo "smoke.1790000000.stubsignature"; exit "${STUB_EXEC:-0}" ;;
    *" port app "*) echo "${STUB_APP_ADDRESS:-127.0.0.1:8000}" ;;
    *" port db "*) echo "${STUB_DB_ADDRESS:-127.0.0.1:5432}" ;;
    *" logs "*) echo "stub container logs" ;;
esac
exit 0
"""

CURL_STUB = r"""#!/usr/bin/env bash
# A fake curl: answers by URL and by whether a bearer token was sent.
echo "$*" >> "$STUB_DIR/curl.log"
output=""; url=""; signed=0; method="GET"
while (($#)); do
    case "$1" in
        --output) output="$2"; shift ;;
        --request) method="$2"; shift ;;
        --header) [[ "$2" == Authorization:* ]] && signed=1; shift ;;
        --data|--max-time|--write-out) shift ;;
        http*) url="$1" ;;
    esac
    shift
done
code=200; body="ok"
case "$url" in
    */health) code="${STUB_HEALTH:-200}" ;;
    */api/v2/members)
        if ((signed == 0)); then code="${STUB_UNSIGNED:-401}"
        elif [[ "$method" == POST ]]; then code="${STUB_CREATE:-201}"
        else code=200; body="${STUB_MEMBERS:-{\"members\": [{\"key\": \"smoke\"}]}}"
        fi ;;
    */) code="${STUB_ROOT:-200}" ;;
esac
printf '%s' "$body" > "$output"
printf '%s' "$code"
"""


def _stubs(bin_dir: Path) -> None:
    """Write the fake docker and curl into a directory."""
    for name, text in (("docker", DOCKER_STUB), ("curl", CURL_STUB)):
        stub = bin_dir / name
        stub.write_text(text, encoding="utf-8")
        stub.chmod(stub.stat().st_mode | stat.S_IXUSR)


def _run(bin_dir: Path, **stub_env: str) -> subprocess.CompletedProcess[str]:
    """Run the smoke script against the stubs.

    Args:
        bin_dir: Directory holding the stubs; first on ``PATH``.
        **stub_env: ``STUB_*`` variables that make one step go wrong.

    Returns:
        The finished process.
    """
    return subprocess.run(
        [str(SCRIPT)],
        env={
            "PATH": f"{bin_dir}{os.pathsep}/usr/bin{os.pathsep}/bin",
            "HOME": os.environ.get("HOME", ""),
            "STUB_DIR": str(bin_dir),
            **stub_env,
        },
        cwd=ROOT,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


@pytest.fixture()
def stubs(tmp_path: Path) -> Path:
    """Return a directory with the fake docker and curl in it."""
    _stubs(tmp_path)
    return tmp_path


def _docker_calls(stubs: Path) -> list[str]:
    """Return the fake docker's logged command lines."""
    return (stubs / "docker.log").read_text(encoding="utf-8").splitlines()


class TestAHealthyStack:
    """Everything answers: the script passes and cleans up."""

    def test_it_passes_and_says_what_it_proved(self, stubs: Path) -> None:
        """Seven checks, then one line that sums them up."""
        result = _run(stubs)

        assert result.returncode == 0, result.stderr
        for line in (
            "Both containers are healthy",
            "The guard admits a request from this machine",
            "A signed request creates a member",
            "The same request without a token is refused",
            "The member survived a restart",
            "Both ports are bound to 127.0.0.1",
        ):
            assert f"✓ {line}" in result.stdout

    def test_it_starts_the_real_compose_file_under_its_own_project(
        self, stubs: Path
    ) -> None:
        """The deployment's file, a throwaway project, built and waited for."""
        _run(stubs)

        start = _docker_calls(stubs)[0]
        assert start.startswith("compose --project-name grocery-butler-smoke ")
        assert "--file deploy/vps/compose.yaml" in start
        assert " up --detach --build --wait " in f"{start} "
        assert "--wait-timeout" in start

    def test_it_removes_everything_it_made(self, stubs: Path) -> None:
        """Containers and the data volume go, pass or fail."""
        _run(stubs)

        assert " down --volumes --remove-orphans" in _docker_calls(stubs)[-1]

    def test_secrets_are_generated_and_never_printed(self, stubs: Path) -> None:
        """The env file is made for the run; no value reaches the output."""
        result = _run(stubs)

        start = _docker_calls(stubs)[0]
        env_file = Path(start.split("--env-file ")[1].split()[0])
        assert not env_file.exists()
        assert "POSTGRES_PASSWORD" not in result.stdout + result.stderr
        assert "RUBOTPAUL_SHARED_SECRET" not in result.stdout + result.stderr

    def test_the_token_is_minted_in_the_app_container(self, stubs: Path) -> None:
        """Where the code and the secret already are, not on the host."""
        _run(stubs)

        log = (stubs / "docker.log").read_text(encoding="utf-8")
        assert log.count(" exec -T app python -c ") == 3
        assert log.count("POST /api/v2/members") == 1
        assert log.count("GET /api/v2/members") == 2
        assert "mint_token" in log

    def test_the_app_is_restarted_before_the_last_read(self, stubs: Path) -> None:
        """Restart, wait for healthy, then read again."""
        _run(stubs)

        calls = _docker_calls(stubs)
        restart = next(i for i, call in enumerate(calls) if " restart app" in call)
        assert " up --detach --wait " in f"{calls[restart + 1]} "
        assert " exec -T app " in calls[restart + 2]


class TestEachFailureStopsTheRun:
    """One thing wrong is enough, and the output says which."""

    @pytest.mark.parametrize(
        ("stub_env", "message"),
        [
            ({"STUB_UP": "1"}, "The stack did not become healthy"),
            ({"STUB_HEALTH": "000"}, "/health answers: expected 200, got 000"),
            (
                {"STUB_ROOT": "403"},
                "The guard admits a request from this machine: expected 200, got 403",
            ),
            ({"STUB_EXEC": "1"}, "Could not mint a token"),
            (
                {"STUB_CREATE": "500"},
                "A signed request creates a member: expected 201, got 500",
            ),
            (
                {"STUB_UNSIGNED": "201"},
                "The same request without a token is refused: expected 401, got 201",
            ),
            ({"STUB_MEMBERS": '{"members": []}'}, "The member was not stored"),
            ({"STUB_REUP": "1"}, "The app did not become healthy after a restart"),
            (
                {"STUB_APP_ADDRESS": "0.0.0.0:8000"},
                "app is published on 0.0.0.0:8000, not on 127.0.0.1",
            ),
            (
                {"STUB_DB_ADDRESS": "0.0.0.0:5432"},
                "db is published on 0.0.0.0:5432, not on 127.0.0.1",
            ),
        ],
        ids=[
            "never-healthy",
            "health-unreachable",
            "guard-rejects",
            "no-token",
            "create-fails",
            "unsigned-accepted",
            "member-missing",
            "unhealthy-after-restart",
            "app-port-public",
            "db-port-public",
        ],
    )
    def test_the_failure_is_named_with_logs_and_cleanup(
        self, stubs: Path, stub_env: dict[str, str], message: str
    ) -> None:
        """Exit 1, the reason, the container logs, and nothing left behind."""
        result = _run(stubs, **stub_env)

        assert result.returncode == 1
        assert f"✗ {message}" in result.stderr
        assert "stub container logs" in result.stderr
        assert "The deployment starts" not in result.stdout
        assert " down --volumes --remove-orphans" in _docker_calls(stubs)[-1]

    def test_a_missing_tool_is_a_clear_error(self, tmp_path: Path) -> None:
        """Without docker the script says what to install, and never passes."""
        for tool in ("bash", "dirname", "curl", "openssl"):
            found = shutil.which(tool)
            assert found is not None
            (tmp_path / tool).symlink_to(found)

        result = subprocess.run(
            [str(SCRIPT)],
            env={"PATH": str(tmp_path), "HOME": os.environ.get("HOME", "")},
            cwd=ROOT,
            capture_output=True,
            text=True,
            check=False,
            timeout=60,
        )

        assert result.returncode == 2
        assert "docker is not installed" in result.stderr


class TestWorkflow:
    """It runs where the container scan runs, and never gates a merge."""

    @pytest.fixture()
    def workflow(self) -> dict[Any, Any]:
        """Load the smoke workflow."""
        loaded: dict[Any, Any] = yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))
        return loaded

    def test_it_runs_nightly_by_hand_and_when_the_deployment_changes(
        self, workflow: dict[Any, Any]
    ) -> None:
        """The scan's triggers, plus the compose file and this script."""
        triggers = workflow[True]
        scan_paths = yaml.safe_load(SCAN.read_text(encoding="utf-8"))[True][
            "pull_request"
        ]["paths"]
        paths = set(triggers["pull_request"]["paths"])

        assert set(triggers) == {"pull_request", "schedule", "workflow_dispatch"}
        assert {"Dockerfile", "docker-entrypoint.sh", "requirements.txt"} <= paths
        assert {"deploy/vps/**", "scripts/compose-smoke.sh"} <= paths
        assert set(scan_paths) & paths

    def test_the_nightly_run_has_a_minute_of_its_own(
        self, workflow: dict[Any, Any]
    ) -> None:
        """Off :00 and :30, and not when CI or the scan start."""
        (entry,) = workflow[True]["schedule"]
        minute = entry["cron"].split()[0]
        others = {
            yaml.safe_load(path.read_text(encoding="utf-8"))[True]["schedule"][0][
                "cron"
            ]
            for path in (CI, SCAN)
        }

        assert minute not in {"0", "00", "30"}
        assert entry["cron"] not in others

    def test_the_job_runs_the_script_and_nothing_else(
        self, workflow: dict[Any, Any]
    ) -> None:
        """The rule lives in the script; the workflow only calls it."""
        (job,) = workflow["jobs"].values()
        runs = [step["run"] for step in job["steps"] if "run" in step]

        assert runs == ["./scripts/compose-smoke.sh"]
        assert job["timeout-minutes"] <= 15
        assert workflow["permissions"] == {"contents": "read"}

    def test_the_ci_gate_does_not_depend_on_it(self) -> None:
        """Nightly and non-gating, as chosen for the container scan (#138)."""
        ci = yaml.safe_load(CI.read_text(encoding="utf-8"))

        assert ci["jobs"]["gate"]["needs"] == ["python", "pre-commit"]
