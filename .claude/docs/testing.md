# Testing Strategy

**Navigation**: [Back to CLAUDE.md](../../CLAUDE.md) | [Workflow](workflow.md) | [Tools](tools.md)

---

The rules (TDD first, flaky tests, coverage floors) are in [`CLAUDE.md`](../../CLAUDE.md). This page is how to meet them here. The `testing` and `mutation-testing` skills cover general technique.

## 1. Layout and Lanes

```
tests/
├── conftest.py          # shared helpers (bearer_header mints request-bound tokens)
├── test_<module>.py     # unit tests, one file per module under grocery_butler/
└── e2e/
    ├── conftest.py      # real create_app(), real stores, a fresh database per test
    ├── test_journeys.py # whole flows: one capability feeding the next
    └── test_*.py        # marked e2e via pytestmark
```

| Lane | Marker | Command | Runs in check-all |
|------|--------|---------|-------------------|
| Unit | none | `./scripts/test.sh` (same as `--unit`) | yes |
| Integration | `integration` | `./scripts/test.sh --integration` | in the coverage step |
| End to end | `e2e` | `./scripts/test.sh --e2e` | in the coverage step |
| Everything | | `./scripts/test.sh --all` | |

`./scripts/coverage.sh` runs the whole suite, so every lane counts toward coverage and every lane must pass. Markers are registered in `pyproject.toml`, and `--strict-markers` rejects unregistered ones.

## 2. Isolate the Variable

A unit test checks one behavior and controls everything else:

- **Anthropic:** never call the real API. Inject a mock client; the e2e suite mocks only that one boundary.
- **Database:** use `tmp_path` for a real temporary SQLite file, or `":memory:"`. Never touch `mealbot.db` or a developer's database.
- **Environment:** set config with `monkeypatch.setenv` and `monkeypatch.delenv`. `load_config` loads `.env` without overriding, so `monkeypatch` always wins over a local `.env`.
- **Secrets:** use obviously fake values (`"e2e-test-secret"`), never a real key.
- **Time and randomness:** pass them in or patch them. No `sleep`; no dependence on test order.

If a test only fails sometimes, it shares state, depends on order, or reads the clock. Find which and fix it; the rule for flaky tests is in [`CLAUDE.md`](../../CLAUDE.md#critical-principles).

### The route ledger

A complete run of the end-to-end lane ends by comparing the routes it reached with the routes the app registers. A route that no end-to-end test drives must be listed, with a reason, in `tests/e2e/route_ledger.py`; a listed route that is now driven must be removed. So a new route needs an end-to-end test or a written reason, and the list only shrinks. `tests/test_route_ledger.py` also holds the docs to the app (`docs/api/v2.md` and the README's route tables) and fails if a module is imported by nothing an entry point reaches. A run narrowed to some files or by `-k` skips the comparison.

## 3. Postgres Tests

Every end-to-end test runs twice, on SQLite and on Postgres: the `db_path` fixture in `tests/e2e/conftest.py` is parametrized, and on Postgres each test gets a schema of its own that is dropped afterwards. The adapter, store and pending-action tests have Postgres variants too. All of them skip unless `TEST_DATABASE_URL` is set:

```bash
TEST_DATABASE_URL=postgresql://localhost/grocery_test ./scripts/check-all.sh
```

Point it only at a throwaway database: the tests create and drop tables. CI runs a Postgres 16 service and sets the variable, so a dialect bug that skips locally still fails CI. A schema change needs both migration files and a test that runs against both dialects.

## 4. Structure and Naming

Arrange, act, assert, with one behavior per test:

```python
def test_convert_between_dimensions_returns_none() -> None:
    """Volume and weight aren't comparable, so convert gives no number."""
    # Arrange
    quantity, from_unit, to_unit = 1.0, Unit.CUP, Unit.OZ

    # Act
    result = convert(quantity, from_unit, to_unit)

    # Assert
    assert result is None
```

Name tests `test_<unit>_<scenario>_<expected>`. Assert exact values, not just truthiness, so a mutant can't survive.

## 5. API Tests

Call `/api/v1` through the Flask test client with a token from `bearer_header` (`tests/conftest.py`), which binds it to the exact method, path and body. Test the unauthenticated, wrong-binding and expired cases next to the happy path.

## 6. Mutation Testing

```bash
./scripts/mutation.sh               # fails below 80%
python scripts/analyze_mutations.py # summarize surviving mutants
```

It is slow and runs before a release, not per PR.

---

**Navigation**: [Back to CLAUDE.md](../../CLAUDE.md) | [Workflow](workflow.md) | [Tools](tools.md)
