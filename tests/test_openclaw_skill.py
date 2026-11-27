"""Tests for the OpenClaw skill and its signing client.

``openclaw/grocery-butler`` is what OpenClaw reads to operate Grocery
Butler: a ``SKILL.md`` playbook and ``gb.py``, a standard-library script
that signs each request. The script is run here as OpenClaw would run it,
against the real app on a real socket, and its tokens are held to the
published vectors. The playbook is held to the API contract, so a route
can't be added or renamed without the skill hearing about it.
"""

from __future__ import annotations

import importlib.util
import json
import os
import re
import secrets
import subprocess
import sys
import threading
from pathlib import Path
from typing import TYPE_CHECKING, Any

import pytest
from werkzeug.serving import make_server

from grocery_butler.app import create_app
from grocery_butler.auth_middleware import SECRET_ENV_VAR

if TYPE_CHECKING:
    from collections.abc import Iterator
    from types import ModuleType

ROOT = Path(__file__).resolve().parent.parent
SKILL_DIR = ROOT / "openclaw" / "grocery-butler"
SKILL = SKILL_DIR / "SKILL.md"
CLIENT = SKILL_DIR / "gb.py"
VECTORS: dict[str, Any] = json.loads(
    (ROOT / "docs" / "api" / "auth-token-vectors.json").read_text(encoding="utf-8")
)
V2_DOC = (ROOT / "docs" / "api" / "v2.md").read_text(encoding="utf-8")


@pytest.fixture(scope="module")
def client_module() -> ModuleType:
    """Load ``gb.py`` as a module, the way a test can call its functions."""
    spec = importlib.util.spec_from_file_location("gb_client", CLIENT)
    assert spec is not None
    assert spec.loader is not None
    module = importlib.util.module_from_spec(spec)
    sys.modules["gb_client"] = module
    spec.loader.exec_module(module)
    return module


class TestTheClientSignsLikeTheServer:
    """The script's own token code matches the published vectors."""

    @pytest.mark.parametrize(
        "case", VECTORS["cases"], ids=[case["name"] for case in VECTORS["cases"]]
    )
    def test_it_mints_exactly_the_published_token(
        self, client_module: ModuleType, case: dict[str, str]
    ) -> None:
        """Written apart from the server's code, held to the same data."""
        token = client_module.mint_token(
            VECTORS["hmac_key_utf8"],
            VECTORS["caller_id"],
            case["method"],
            case["path"],
            case["body_utf8"].encode("utf-8"),
            float(VECTORS["timestamp"]),
        )

        assert token == case["token"]

    def test_it_imports_nothing_from_the_app(self) -> None:
        """It must run where only Python is installed."""
        source = CLIENT.read_text(encoding="utf-8")

        assert "grocery_butler" not in re.sub(r'""".*?"""', "", source, flags=re.DOTALL)
        assert "import requests" not in source


@pytest.fixture()
def secret() -> str:
    """Return a signing secret made for this test."""
    return secrets.token_hex(16)


@pytest.fixture()
def server(
    tmp_path: Path, secret: str, monkeypatch: pytest.MonkeyPatch
) -> Iterator[str]:
    """Serve the real app on a loopback port the operating system picks."""
    monkeypatch.setenv(SECRET_ENV_VAR, secret)
    app = create_app(db_path=str(tmp_path / "skill.db"))
    running = make_server("127.0.0.1", 0, app, threaded=True)
    thread = threading.Thread(target=running.serve_forever, daemon=True)
    thread.start()
    try:
        yield f"http://127.0.0.1:{running.server_port}"
    finally:
        running.shutdown()
        thread.join(timeout=10)
        running.server_close()


def _gb(
    *args: str, url: str, secret: str | None, **extra: str
) -> subprocess.CompletedProcess[str]:
    """Run the client as OpenClaw would: a bare process, settings in env."""
    env = {"PATH": os.environ["PATH"], "GROCERY_BUTLER_URL": url, **extra}
    if secret is not None:
        env[SECRET_ENV_VAR] = secret
    return subprocess.run(
        [sys.executable, "-I", str(CLIENT), *args],
        env=env,
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
    )


class TestTheClientAgainstTheRealApp:
    """Each call goes through a socket, the guard and the auth hook."""

    def test_a_read_returns_json_and_exits_zero(self, server: str, secret: str) -> None:
        """A signed GET, with a query string that is not part of the signature."""
        result = _gb(
            "GET", "/api/v2/items?include_inactive=true", url=server, secret=secret
        )

        assert result.returncode == 0, result.stderr
        assert "items" in json.loads(result.stdout)
        assert "HTTP 200" in result.stderr

    def test_a_change_is_signed_keyed_and_attributed(
        self, server: str, secret: str
    ) -> None:
        """Bootstrap a member, then act as them: body, key and member all go."""
        first = _gb(
            "POST",
            "/api/v2/members",
            "--data",
            '{"key": "geoff", "display_name": "Geoff"}',
            url=server,
            secret=secret,
        )
        item = _gb(
            "POST",
            "/api/v2/items",
            "--member",
            "geoff",
            "--data",
            '{"name": "jalapeño"}',
            url=server,
            secret=secret,
        )

        assert first.returncode == 0, first.stdout
        assert item.returncode == 0, item.stdout
        assert json.loads(item.stdout)["name"] == "jalapeño"
        assert re.search(
            r"^Idempotency-Key: oc-[0-9a-f]{32}$", item.stderr, re.MULTILINE
        )

    def test_repeating_a_change_with_its_key_changes_nothing(
        self, server: str, secret: str
    ) -> None:
        """The same key gives the first answer back instead of a second need."""
        _gb(
            "POST",
            "/api/v2/members",
            "--data",
            '{"key": "geoff", "display_name": "Geoff"}',
            url=server,
            secret=secret,
        )
        made = _gb(
            "POST",
            "/api/v2/items",
            "--member",
            "geoff",
            "--data",
            '{"name": "oat milk"}',
            url=server,
            secret=secret,
        )
        item_id = json.loads(made.stdout)["id"]
        ask = (
            "POST",
            "/api/v2/needs",
            "--member",
            "geoff",
            "--key",
            "skill-test-0001",
            "--data",
            json.dumps({"item_id": item_id}),
        )

        first = _gb(*ask, url=server, secret=secret)
        again = _gb(*ask, url=server, secret=secret)
        needs = _gb("GET", "/api/v2/needs", url=server, secret=secret)

        assert (first.returncode, again.returncode) == (0, 0)
        assert json.loads(again.stdout) == json.loads(first.stdout)
        assert len(json.loads(needs.stdout)["needs"]) == 1

    def test_a_refusal_prints_the_error_and_exits_one(
        self, server: str, secret: str
    ) -> None:
        """The coded error is on standard output, where OpenClaw reads it."""
        result = _gb("GET", "/api/v2/items/99999", url=server, secret=secret)

        assert result.returncode == 1
        assert json.loads(result.stdout)["error"]["code"] == "item_not_found"

    def test_the_wrong_secret_is_refused(self, server: str) -> None:
        """A token signed with another secret is a 401, not a silent success."""
        result = _gb("GET", "/api/v2/items", url=server, secret="not-the-secret")

        assert result.returncode == 1
        assert "HTTP 401" in result.stderr

    def test_the_secret_can_come_from_a_file(
        self, server: str, secret: str, tmp_path: Path
    ) -> None:
        """Read from the deployment's env file, so it is written down once."""
        env_file = tmp_path / "deploy.env"
        env_file.write_text(
            f"POSTGRES_PASSWORD=unused\n{SECRET_ENV_VAR}={secret}\n", encoding="utf-8"
        )

        result = _gb(
            "GET",
            "/api/v2/items",
            url=server,
            secret=None,
            GROCERY_BUTLER_ENV_FILE=str(env_file),
        )

        assert result.returncode == 0, result.stderr

    def test_without_a_secret_nothing_is_sent(
        self, server: str, tmp_path: Path
    ) -> None:
        """No secret in the environment or the file: exit 2, one line why."""
        result = _gb(
            "GET",
            "/api/v2/items",
            url=server,
            secret=None,
            GROCERY_BUTLER_ENV_FILE=str(tmp_path / "missing.env"),
        )

        assert result.returncode == 2
        assert "No signing secret" in result.stderr
        assert result.stdout == ""

    def test_a_full_url_is_not_a_path(self, server: str, secret: str) -> None:
        """The destination comes from the setting, never from the argument."""
        result = _gb(
            "GET", "http://example.invalid/api/v2/items", url=server, secret=secret
        )

        assert result.returncode == 2
        assert "PATH must be a path" in result.stderr

    def test_an_address_that_is_not_http_is_refused(self, secret: str) -> None:
        """Only http and https addresses are ever contacted."""
        result = _gb("GET", "/api/v2/items", url="ftp://127.0.0.1", secret=secret)

        assert result.returncode == 2
        assert "must look like" in result.stderr

    def test_an_unreachable_server_exits_three(self, secret: str) -> None:
        """A closed port is reported as unreachable, not as a refusal."""
        result = _gb("GET", "/api/v2/items", url="http://127.0.0.1:9", secret=secret)

        assert result.returncode == 3
        assert "could not reach" in result.stderr


SKILL_TEXT = SKILL.read_text(encoding="utf-8") if SKILL.exists() else ""
_PARAMETER = re.compile(r"<[^>]+>|\{[^}]+\}")


def _shape(path: str) -> str:
    """Reduce a path to its shape, so differently named parameters compare equal."""
    return _PARAMETER.sub("{}", path)


class TestThePlaybookMatchesTheContract:
    """The skill names every capability, and nothing that doesn't exist."""

    def test_it_has_the_front_matter_openclaw_reads(self) -> None:
        """A name and a description that says when to use it and when not to."""
        front, _, _body = SKILL_TEXT.removeprefix("---\n").partition("\n---\n")
        fields = dict(line.split(": ", 1) for line in front.splitlines())

        assert SKILL_TEXT.startswith("---\n")
        assert fields["name"] == "grocery-butler"
        assert "Use when" in fields["description"]
        assert "Do NOT use" in fields["description"]

    def test_every_v2_capability_is_in_the_playbook(self) -> None:
        """A route added to the contract must be taught to OpenClaw too."""
        documented = {
            (method, _shape(f"/api/v2{path}"))
            for method, path in re.findall(
                r"^\| `(GET|POST|PATCH|DELETE) (/[^`?]*)`", V2_DOC, re.MULTILINE
            )
        }
        taught = {
            (method, _shape(path))
            for method, path in re.findall(
                r"`(GET|POST|PATCH|DELETE) (/api/v2/[^`?\s]*)", SKILL_TEXT
            )
        }

        assert documented - taught == set(), "in docs/api/v2.md, not in the skill"
        assert taught - documented == set(), "in the skill, not in docs/api/v2.md"

    def test_every_v1_call_it_names_exists(self, tmp_path: Path) -> None:
        """The older calls it mentions are real routes."""
        app = create_app(db_path=str(tmp_path / "routes.db"))
        served = {
            (method, _shape(rule.rule))
            for rule in app.url_map.iter_rules()
            for method in (rule.methods or set()) - {"HEAD", "OPTIONS"}
        }
        named = {
            (method, _shape(path))
            for method, path in re.findall(
                r"`(GET|POST|PATCH|DELETE) (/api/v1/[^`?\s]*)`", SKILL_TEXT
            )
        }

        assert named
        assert named - served == set()

    def test_every_json_example_is_valid_json(self) -> None:
        """An example OpenClaw copies must at least parse."""
        examples = re.findall(r"--data '(\{.*?\})'", SKILL_TEXT, re.DOTALL)

        assert len(examples) >= 8
        for example in examples:
            json.loads(example.replace("<the draft's payload_hash>", "0" * 64))

    @pytest.mark.parametrize(
        "rule",
        [
            "Never say anything was ordered",
            "A person decides every purchase",
            "Never approve on their behalf",
            "Read hard constraints to the member",
            "Never guess an item, a substitution or a merge",
            "same `--key`",
            "Never print the secret",
        ],
    )
    def test_it_states_the_rules_that_keep_people_in_control(self, rule: str) -> None:
        """The safety rules are in the text, not left to be inferred."""
        assert rule in SKILL_TEXT
