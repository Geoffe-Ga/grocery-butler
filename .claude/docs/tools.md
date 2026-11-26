# Tool Usage

**Navigation**: [Back to CLAUDE.md](../../CLAUDE.md) | [Testing](testing.md) | [Troubleshooting](troubleshooting.md)

---

## 1. Setup

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt -e .
pre-commit install --install-hooks -t pre-commit -t commit-msg
cp .env.example .env   # then fill in your own values; never commit it
```

`requirements-dev.txt` pins every tool exactly and includes `requirements.txt`. The scripts activate `.venv` themselves (`scripts/activate-venv.sh`), including the shared venv a git worktree uses. To upgrade a tool, bump its pin, run `./scripts/check-all.sh`, and commit both.

## 2. Scripts

Always use the script, never the bare tool:

| Task | Never | Always |
|------|-------|--------|
| All checks | each tool by hand | `./scripts/check-all.sh` |
| Pre-commit as CI runs it | `pre-commit run --all-files` | `./scripts/precommit-changed.sh` |
| Lint and docstring coverage | `ruff check .` | `./scripts/lint.sh` (`--fix` to fix) |
| Format | `ruff format .` | `./scripts/format.sh` (`--check` to check only) |
| Auto-fix lint and format | `ruff check --fix .` | `./scripts/fix-all.sh` |
| Types | `mypy .` | `./scripts/typecheck.sh` |
| Tests | `pytest` | `./scripts/test.sh` (`--unit`, `--integration`, `--e2e`, `--all`, `--coverage`) |
| Coverage gate | `pytest --cov` | `./scripts/coverage.sh` (`--html` writes `htmlcov/`) |
| Security | `bandit -r .` | `./scripts/security.sh` |
| Complexity | `radon cc .` | `./scripts/complexity.sh` |
| Package build | `python -m build` | `./scripts/build.sh` |
| Mutation testing | `mutmut run` | `./scripts/mutation.sh` (`--min-score N`) |

Every script takes `--help` and `--verbose`.

### `./scripts/check-all.sh`

Runs, in order, and reports every failure rather than stopping at the first:

1. Lint: Ruff, then interrogate (docstring coverage)
2. Format: Ruff format and import order
3. Types: mypy on `grocery_butler/`
4. Security: bandit, and pip-audit on the pinned requirements
5. Complexity: radon reports, xenon enforces
6. Unit tests
7. Coverage: the whole suite, line and branch floors
8. Build: sdist and wheel, `twine check --strict`

Exit 0 means every check passed. Mutation testing isn't in it, because it is slow.

### `./scripts/precommit-changed.sh`

Runs pre-commit on the files the branch changed since its merge-base with `origin/main` (it fetches first), exactly as CI's pre-commit job does. It checks every file when `.pre-commit-config.yaml`, `requirements-dev.txt` or `pyproject.toml` changed, when there is no usable base, or when `FULL=true`. CI sets `BASE_REF` to the PR's base. Extra arguments pass through to `pre-commit run`.

A whole-tree `pre-commit run --all-files` is worth it only after a wide rename or a hook config change, and the script already does that for a config change.

### Known vulnerabilities

When pip-audit flags a vulnerability with no fixed release, list its ID in `.pip-audit-known-vulnerabilities` with the package, the reason and a tracking issue. `scripts/security.sh` passes each one to `--ignore-vuln`. Remove the entry as soon as a fix ships (`cve-remediation`).

## 3. When Direct Invocation Is Fine

Only for fast feedback while you iterate, with `.venv` active:

```bash
pytest tests/test_units.py -v
pytest tests/test_api.py::TestInventoryEndpoint -vv
mypy grocery_butler/units.py
ruff check grocery_butler/api.py --select TRY
```

Run the scripts before you commit.

## 4. Running the App

```bash
python -m grocery_butler.db.migrate            # apply migrations (DATABASE_URL or the default SQLite file)
gunicorn 'grocery_butler.app:create_app()' --bind 0.0.0.0:5000
python -m grocery_butler --help                # CLI
```

README "Running the System" covers the rest, including the local-only Discord bot.

## 5. Code Style

- Ruff formats and sorts imports, line length 88, target Python 3.11.
- PEP 8 names; type hints on every signature.
- Google-style docstrings:

```python
def convert(quantity: float, from_unit: Unit, to_unit: Unit) -> float | None:
    """Convert a quantity from one unit to another.

    Args:
        quantity: The numeric quantity in ``from_unit``.
        from_unit: The unit ``quantity`` is currently expressed in.
        to_unit: The unit to convert to.

    Returns:
        The converted quantity, or ``None`` if the two units are not
        comparable.
    """
```

---

**Navigation**: [Back to CLAUDE.md](../../CLAUDE.md) | [Testing](testing.md) | [Troubleshooting](troubleshooting.md)
