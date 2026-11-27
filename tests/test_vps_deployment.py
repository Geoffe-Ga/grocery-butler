"""Tests for the self-hosted deployment files (ADR 0021).

``deploy/vps/compose.yaml`` runs the app and Postgres on the household's
own server. Nothing here starts a container. The tests read the file and
hold the properties the deployment's safety rests on: nothing listens on a
public interface, no secret is written down, a missing secret stops the
start, and the network boundary guard trusts exactly the address the
published port delivers requests from.
"""

from __future__ import annotations

import ipaddress
import re
import subprocess
from pathlib import Path
from typing import Any

import pytest
import yaml

from grocery_butler.network_guard import parse_allowed_cidrs

ROOT = Path(__file__).resolve().parent.parent
VPS = ROOT / "deploy" / "vps"
COMPOSE = VPS / "compose.yaml"
ENV_EXAMPLE = VPS / "env.example"
RUNBOOK = ROOT / "docs" / "operations" / "vps-deployment.md"

REQUIRED_SECRETS = ("POSTGRES_PASSWORD", "RUBOTPAUL_SHARED_SECRET", "FLASK_SECRET_KEY")
OPTIONAL_SECRETS = ("ANTHROPIC_API_KEY", "INSTACART_API_KEY")


@pytest.fixture(scope="module")
def compose() -> dict[str, Any]:
    """Load the compose file."""
    loaded: dict[str, Any] = yaml.safe_load(COMPOSE.read_text(encoding="utf-8"))
    return loaded


@pytest.fixture(scope="module")
def app(compose: dict[str, Any]) -> dict[str, Any]:
    """Return the app service."""
    service: dict[str, Any] = compose["services"]["app"]
    return service


@pytest.fixture(scope="module")
def db(compose: dict[str, Any]) -> dict[str, Any]:
    """Return the database service."""
    service: dict[str, Any] = compose["services"]["db"]
    return service


class TestNothingListensInPublic:
    """Every published port is bound to the server's loopback address."""

    def test_there_are_two_services(self, compose: dict[str, Any]) -> None:
        """The app and its database, and nothing else to keep track of."""
        assert set(compose["services"]) == {"app", "db"}

    @pytest.mark.parametrize("service", ["app", "db"])
    def test_ports_are_loopback_only(
        self, compose: dict[str, Any], service: str
    ) -> None:
        """A port without ``127.0.0.1:`` would listen on the public address."""
        ports = compose["services"][service]["ports"]

        assert ports
        for port in ports:
            assert str(port).startswith("127.0.0.1:"), port

    def test_neither_service_uses_the_host_network(
        self, compose: dict[str, Any]
    ) -> None:
        """Host networking would skip the loopback binding above."""
        for service in compose["services"].values():
            assert "network_mode" not in service


class TestSecretsStayOutOfTheFile:
    """Secrets come from the server's own ``.env``, never from the repo."""

    @pytest.mark.parametrize("name", REQUIRED_SECRETS)
    def test_a_missing_required_secret_stops_the_start(self, name: str) -> None:
        """``${NAME:?message}`` makes compose refuse to start without it."""
        text = COMPOSE.read_text(encoding="utf-8")

        assert re.search(rf"\$\{{{name}:\?[^}}]+\}}", text), name

    @pytest.mark.parametrize("name", REQUIRED_SECRETS + OPTIONAL_SECRETS)
    def test_secrets_are_only_ever_references(self, name: str) -> None:
        """Each mention of a secret's name is ``${NAME...}`` or a mapping key."""
        for line in COMPOSE.read_text(encoding="utf-8").splitlines():
            if name not in line or line.lstrip().startswith("#"):
                continue
            without_refs = re.sub(rf"\$\{{{name}[^}}]*\}}", "", line)
            assert re.fullmatch(rf"\s*{name}:\s*", without_refs) or (
                name not in without_refs
            ), line

    @pytest.mark.parametrize("name", REQUIRED_SECRETS + OPTIONAL_SECRETS)
    def test_the_example_env_file_holds_no_values(self, name: str) -> None:
        """The example names every secret and gives none of them a value."""
        lines = ENV_EXAMPLE.read_text(encoding="utf-8").splitlines()

        assert f"{name}=" in lines

    def test_the_real_env_file_is_ignored_by_git(self) -> None:
        """The file that does hold values can't be committed by accident."""
        result = subprocess.run(
            ["git", "check-ignore", "--quiet", "deploy/vps/.env"],
            cwd=ROOT,
            check=False,
        )

        assert result.returncode == 0

    def test_the_real_env_file_is_kept_out_of_the_image(self) -> None:
        """The build context leaves the whole deploy directory behind."""
        ignored = (ROOT / ".dockerignore").read_text(encoding="utf-8").splitlines()

        assert "deploy/" in ignored


class TestTheApp:
    """The app is built from this checkout and talks to the database."""

    def test_it_is_built_from_the_repository_dockerfile(
        self, app: dict[str, Any]
    ) -> None:
        """The same image CI scans, not a second definition."""
        build = app["build"]

        assert (VPS / build["context"]).resolve() == ROOT
        assert build.get("dockerfile", "Dockerfile") == "Dockerfile"

    def test_it_waits_for_a_healthy_database(self, app: dict[str, Any]) -> None:
        """The entrypoint migrates on boot, so the database must be up."""
        assert app["depends_on"] == {"db": {"condition": "service_healthy"}}

    def test_its_database_url_points_at_the_db_service(
        self, app: dict[str, Any], db: dict[str, Any]
    ) -> None:
        """User and database name match what the db service creates."""
        url = app["environment"]["DATABASE_URL"]
        user = db["environment"]["POSTGRES_USER"]
        name = db["environment"]["POSTGRES_DB"]

        assert url == f"postgresql://{user}:${{POSTGRES_PASSWORD}}@db:5432/{name}"

    @pytest.mark.parametrize("service", ["app", "db"])
    def test_services_come_back_after_a_reboot(
        self, compose: dict[str, Any], service: str
    ) -> None:
        """A restarted server brings the household's data back by itself."""
        assert compose["services"][service]["restart"] == "unless-stopped"

    @pytest.mark.parametrize("service", ["app", "db"])
    def test_logs_are_rotated(self, compose: dict[str, Any], service: str) -> None:
        """Audit events are log lines (ADR 0015); they must not fill the disk."""
        options = compose["services"][service]["logging"]["options"]

        assert options["max-size"]
        assert int(options["max-file"]) >= 2


class TestTheGuardMatchesTheNetwork:
    """The boundary guard trusts the one address requests arrive from.

    A request to the loopback-published port reaches the app from the
    compose network's gateway, so that address, and no wider range, is on
    the allow-list.
    """

    @pytest.fixture()
    def network(self, compose: dict[str, Any]) -> dict[str, str]:
        """Return the pinned subnet and gateway of the one compose network."""
        (definition,) = compose["networks"].values()
        (config,) = definition["ipam"]["config"]
        pinned: dict[str, str] = config
        return pinned

    def test_the_gateway_is_inside_the_pinned_subnet(
        self, network: dict[str, str]
    ) -> None:
        """A fixed subnet, so the allow-list can name a fixed address."""
        subnet = ipaddress.ip_network(network["subnet"])

        assert subnet.is_private
        assert ipaddress.ip_address(network["gateway"]) in subnet

    def test_the_allow_list_is_loopback_plus_the_gateway(
        self, app: dict[str, Any], network: dict[str, str]
    ) -> None:
        """Exactly loopback and the gateway as a single address."""
        allowed = parse_allowed_cidrs(app["environment"]["TAILNET_GUARD_ALLOWED_CIDRS"])

        assert set(allowed) == {
            ipaddress.ip_network("127.0.0.0/8"),
            ipaddress.ip_network("::1/128"),
            ipaddress.ip_network(f"{network['gateway']}/32"),
        }

    def test_the_guard_is_not_switched_off(self, app: dict[str, Any]) -> None:
        """Nothing in the file disables the guard."""
        assert "TAILNET_GUARD_ENABLED" not in app["environment"]


class TestTheDatabase:
    """Postgres keeps its data in a named volume, on the version CI tests."""

    def test_it_runs_the_major_version_ci_tests_against(
        self, db: dict[str, Any]
    ) -> None:
        """One Postgres version to reason about."""
        ci = yaml.safe_load(
            (ROOT / ".github" / "workflows" / "ci.yml").read_text(encoding="utf-8")
        )
        ci_image = ci["jobs"]["python"]["services"]["postgres"]["image"]

        assert db["image"] == ci_image

    def test_its_data_lives_in_a_named_volume(
        self, compose: dict[str, Any], db: dict[str, Any]
    ) -> None:
        """Rebuilding or removing the container keeps the household's data."""
        (mount,) = db["volumes"]
        volume, _, path = str(mount).partition(":")

        assert path == "/var/lib/postgresql/data"
        assert volume in compose["volumes"]

    def test_it_reports_its_own_health(self, db: dict[str, Any]) -> None:
        """The app's ``depends_on`` needs something to wait for."""
        assert "pg_isready" in " ".join(db["healthcheck"]["test"])


class TestTheRunbook:
    """The runbook exists and covers the steps a deploy can't skip."""

    @pytest.mark.parametrize(
        "phrase",
        [
            "deploy/vps/compose.yaml",
            "tailscale serve",
            "network_guard_rejected",
            "./scripts/backup.sh",
            "grocery-butler-backup.timer",
            "loginctl enable-linger",
            "chmod 600",
            "http://127.0.0.1:8000/api/v1",
        ],
    )
    def test_it_names_each_step(self, phrase: str) -> None:
        """Start, expose on the tailnet, verify the guard, back up, protect."""
        assert phrase in RUNBOOK.read_text(encoding="utf-8")

    def test_the_signing_secret_is_copied_from_openclaw_not_generated(self) -> None:
        """A fresh value here is one OpenClaw doesn't hold: every call gets 401."""
        text = RUNBOOK.read_text(encoding="utf-8")
        step = text[
            text.index("**Write the secrets file.**") : text.index("**Start it.**")
        ]

        assert "already holds" in step
        assert "bad signature" in text
        assert "sha256sum" in text

    def test_the_example_env_file_says_to_reuse_openclaws_secret(self) -> None:
        """The comment beside the variable gives the same instruction."""
        text = ENV_EXAMPLE.read_text(encoding="utf-8")
        comment = text[: text.index("RUBOTPAUL_SHARED_SECRET=")].rsplit("\n\n", 1)[-1]

        assert "already" in comment
