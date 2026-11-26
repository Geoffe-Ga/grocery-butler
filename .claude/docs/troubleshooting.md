# Common Pitfalls and Troubleshooting

**Navigation**: [Back to CLAUDE.md](../../CLAUDE.md) | [Tools](tools.md)

---

## 1. The Wrong Fix and the Right One

| Problem | Wrong response | Right response |
|---------|----------------|----------------|
| A test fails | Comment it out, skip it, retry it | Fix the code, or the test if its expectation is wrong |
| A test is flaky | `@pytest.mark.skip`, a rerun, a longer sleep | Find the shared state, order dependence or clock and remove it |
| A lint rule fires | `# noqa` | Refactor until the rule is satisfied |
| mypy complains | `# type: ignore` | Fix the annotation or the code |
| Coverage is low | An `omit` entry or a lower `fail_under` | Write the missing tests, error paths first |
| Complexity is high | Delete error handling | Extract helpers; replace branching with a lookup table |
| A hook is slow | `git commit --no-verify` | Let it run |

### Complexity: extract, don't delete

```python
# Wrong: drop the validation to get under the limit
def apply_update(item, update):
    item.status = update.status
    ...

# Right: keep every check, move each into a named helper
def apply_update(item: InventoryItem, update: InventoryUpdate) -> InventoryItem:
    _require_known_item(item)
    _require_valid_status(update.status)
    return _merged(item, update)
```

### Types: say what's true

```python
# Wrong
value = row["quantity"]  # type: ignore[index]

# Right: narrow the type so mypy and the reader agree
if row is None:
    raise LookupError(f"No inventory row for {name!r}")
value = row["quantity"]
```

### Placeholders

Don't ship `return []` or `return True` as a stand-in. Implement it, or raise `NotImplementedError` naming the issue that tracks it.

## 2. Common Failures

### Coverage below the floor

```bash
./scripts/coverage.sh --html   # open htmlcov/index.html
```

Branch coverage has its own floor, checked from `coverage.xml` after the line check. Look for the `else` you never exercised and the error path you never forced.

### Passes locally, fails in CI

- **Postgres:** CI sets `TEST_DATABASE_URL`, so tests that skip on your machine run there. Set it locally against a throwaway Postgres database and re-run.
- **Python version:** CI runs 3.11, 3.12 and 3.13. Reproduce with the failing version.
- **Changed files:** CI runs pre-commit only on the PR's changed files, unless the hook config, `requirements-dev.txt` or `pyproject.toml` changed. Run `./scripts/precommit-changed.sh` to see the same set.
- **Your `.env`:** a value in your local `.env` can mask a missing default. Tests must set what they need with `monkeypatch`.

The `ci-debugging` skill has the full protocol. Don't call a failure pre-existing until you have reproduced it on `origin/main`.

### "No virtualenv found"

The scripts look for `.venv` at the repo root, then the shared venv a worktree uses. Create one (see [Tools](tools.md#1-setup)).

### A migration works on SQLite but not Postgres

Every migration needs both `NNN_name.sql` and `NNN_name_pg.sql`. Run `tests/test_migrate.py` and the adapter tests with `TEST_DATABASE_URL` set.

### pip-audit fails

Upgrade the pinned package if a fixed release exists (`cve-remediation`). Only when none exists, record the ID in `.pip-audit-known-vulnerabilities` with its reason and tracking issue.

### A commit is rejected

- `no-commit-to-branch`: you are on `main`. Branch first.
- `conventional-pre-commit`: use `type(scope): subject`.
- `detect-secrets`: remove the secret. If it is a genuine false positive, such as a fake test value, mark it with `# pragma: allowlist secret` or update `.secrets.baseline`, and say why in the PR.

---

**Navigation**: [Back to CLAUDE.md](../../CLAUDE.md) | [Tools](tools.md)
