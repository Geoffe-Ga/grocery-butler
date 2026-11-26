"""Routes, docs and services agree with each other (issue #229).

Three ways a capability can exist and still not be usable, each caught
here by name:

- a route that is registered but not documented, or documented but gone;
- a route no end-to-end test drives (the comparison itself runs at the end
  of the e2e lane, in ``tests/e2e/conftest.py``; its rule is tested here);
- a module that nothing reachable from a channel imports.
"""

from __future__ import annotations

import ast
import re
from pathlib import Path

import pytest

from grocery_butler.app import create_app
from tests.e2e.route_ledger import (
    NOT_YET_END_TO_END,
    Route,
    problems,
    registered_routes,
)

ROOT = Path(__file__).resolve().parent.parent
PACKAGE = ROOT / "grocery_butler"
README = (ROOT / "README.md").read_text(encoding="utf-8")
V2_DOC = (ROOT / "docs" / "api" / "v2.md").read_text(encoding="utf-8")

_PARAMETER = re.compile(r"<[^>]+>|\{[^}]+\}")


def _shape(path: str) -> str:
    """Reduce a path to its shape, so ``<int:id>`` and ``{id}`` compare equal."""
    return _PARAMETER.sub("{}", path)


@pytest.fixture(scope="module")
def routes(tmp_path_factory: pytest.TempPathFactory) -> set[Route]:
    """Return every route the real app registers, by shape."""
    app = create_app(db_path=str(tmp_path_factory.mktemp("ledger") / "ledger.db"))
    return {(method, _shape(rule)) for method, rule in registered_routes(app)}


class TestEveryRouteIsDocumented:
    """What OpenClaw and a person are told matches what the app serves."""

    def test_api_v2_matches_its_contract_document(self, routes: set[Route]) -> None:
        """Every v2 route has a row in docs/api/v2.md, and every row a route."""
        documented = {
            (method, _shape(f"/api/v2{path}"))
            for method, path in re.findall(
                r"^\| `(GET|POST|PATCH|DELETE) (/[^`?]*)`", V2_DOC, re.MULTILINE
            )
        }
        registered = {route for route in routes if route[1].startswith("/api/v2/")}

        assert registered - documented == set(), "registered but not in docs/api/v2.md"
        assert documented - registered == set(), "in docs/api/v2.md but not registered"

    def test_dashboard_routes_match_the_readme_tables(self, routes: set[Route]) -> None:
        """Every page and form has a row in the README, and every row a route."""
        documented = {
            (method, _shape(path))
            for method, path in re.findall(
                r"^\| `(GET|POST|PATCH|DELETE)` \| `(/[^`?]*)`", README, re.MULTILINE
            )
        }
        registered = {route for route in routes if not route[1].startswith("/api/")}

        assert registered - documented == set(), "registered but not in the README"
        assert documented - registered == set(), "in the README but not registered"

    def test_every_api_v1_path_is_named_in_the_readme(self, routes: set[Route]) -> None:
        """The v1 section names each path it serves."""
        named = {
            _shape(path) for path in re.findall(r"`(?:[A-Z]+ )?(/[^`\s?]*)`", README)
        }
        paths = {
            path.removeprefix("/api/v1")
            for _, path in routes
            if path.startswith("/api/v1/")
        }

        assert paths - named == set(), "served under /api/v1 but not in the README"


class TestTheLedgerRule:
    """How reached, registered and excused routes are compared."""

    A: Route = ("GET", "/a")
    B: Route = ("POST", "/b")

    def test_it_balances_when_every_route_is_reached_or_excused(self) -> None:
        """The real list, with everything else reached, has no problems."""
        excused = set(NOT_YET_END_TO_END)

        assert problems({self.A, *excused}, {self.A}) == []

    def test_an_unreached_unlisted_route_is_named(self) -> None:
        """A new route with no end-to-end test and no reason."""
        (line,) = problems({self.A, self.B, *NOT_YET_END_TO_END}, {self.A})

        assert line.startswith("POST /b: no end-to-end test reaches it")

    def test_a_listed_route_that_is_now_reached_must_be_removed(self) -> None:
        """The list can only shrink."""
        listed = next(iter(NOT_YET_END_TO_END))

        (line,) = problems(set(NOT_YET_END_TO_END), {listed})

        assert line.startswith(f"{listed[0]} {listed[1]}: an end-to-end test reaches")

    def test_a_listed_route_that_no_longer_exists_must_be_removed(self) -> None:
        """A stale entry is a problem too."""
        gone = next(iter(NOT_YET_END_TO_END))

        (line,) = problems(set(NOT_YET_END_TO_END) - {gone}, set())

        assert line.endswith("the app has no such route. Remove the entry.")

    def test_every_excused_route_has_a_reason_and_exists(self, tmp_path: Path) -> None:
        """No blank reasons, and nothing excused that the app doesn't serve."""
        app = create_app(db_path=str(tmp_path / "ledger.db"))

        assert all(reason.strip() for reason in NOT_YET_END_TO_END.values())
        assert set(NOT_YET_END_TO_END) <= registered_routes(app)


#: Entry points: what a person, OpenClaw or the container actually starts.
CHANNELS = ("api", "api_v2", "app", "cli", "bot", "__main__", "db.migrate")

#: Modules loaded by file name rather than by import, with the reason.
LOADED_BY_NAME = {
    "db.migrate_unit_enum": "Imported by migration 003's hook, which the"
    " migration runner loads from the migrations directory by file name.",
}


def _module_name(path: Path) -> str:
    """Return a module's dotted name inside the package."""
    parts = path.relative_to(PACKAGE).with_suffix("").parts
    return ".".join(part for part in parts if part != "__init__")


def _imports(path: Path) -> set[str]:
    """Return the package-internal modules a file imports, dotted."""
    found: set[str] = set()
    for node in ast.walk(ast.parse(path.read_text(encoding="utf-8"))):
        if isinstance(node, ast.ImportFrom) and node.module:
            names = [node.module, *(f"{node.module}.{a.name}" for a in node.names)]
        elif isinstance(node, ast.Import):
            names = [alias.name for alias in node.names]
        else:
            continue
        found.update(
            name.removeprefix("grocery_butler").lstrip(".")
            for name in names
            if name.startswith("grocery_butler")
        )
    return found


def test_every_module_is_reachable_from_an_entry_point() -> None:
    """A module nothing imports is a capability nobody can use.

    Starting from the entry points, follow imports inside the package.
    Whatever is left over was built and never wired in.
    """
    files = {
        _module_name(path): path
        for path in PACKAGE.rglob("*.py")
        if "migrations" not in path.parts
    }
    reached: set[str] = set()
    waiting = list(CHANNELS)
    while waiting:
        name = waiting.pop()
        if name in reached or name not in files:
            continue
        reached.add(name)
        for imported in _imports(files[name]):
            waiting.extend({imported, imported.rpartition(".")[0]})

    unwired = set(files) - reached - set(LOADED_BY_NAME) - {""}

    assert unwired == set(), "built but imported by nothing an entry point reaches"
