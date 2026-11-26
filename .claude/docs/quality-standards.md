# Quality Standards

**Navigation**: [Back to CLAUDE.md](../../CLAUDE.md) | [Principles](principles.md) | [Workflow](workflow.md)

---

The summary table is in [`CLAUDE.md`](../../CLAUDE.md#quality-standards). This page says where each rule is set and what it means. If this page and the config disagree, the config wins: fix this page.

## 1. Where Each Rule Lives

| Rule | Value | Set in |
|------|-------|--------|
| Line coverage | ≥90% | `fail_under` in `[tool.coverage.report]`, `--cov-fail-under=90` in `scripts/coverage.sh` |
| Branch coverage | ≥90% | the branch-rate check in `scripts/coverage.sh` (reads `coverage.xml`) |
| Coverage scope | `grocery_butler/` | `source` in `[tool.coverage.run]` |
| Docstring coverage | ≥95% | `[tool.interrogate]` (`fail-under = 95`) |
| Complexity | ≤10 per function; modules and the average at rank A | `xenon --max-absolute B --max-modules A --max-average A` in `scripts/complexity.sh` |
| Types | 0 errors | `[tool.mypy]` (strict flags such as `disallow_untyped_defs` and `warn_unused_ignores`), scope in `scripts/typecheck.sh` |
| Lint | 0 violations | `[tool.ruff.lint]` `select` |
| Format | Ruff, line length 88 | `[tool.ruff]` |
| Security | 0 findings | `scripts/security.sh`: bandit on `grocery_butler/`, pip-audit on `requirements-dev.txt` |
| Package build | sdist and wheel pass `twine check --strict` | `scripts/build.sh` |
| Mutation score | ≥80%, before a release | `MIN_SCORE` in `scripts/mutation.sh`; scope in `[tool.mutmut]` |
| Tool versions | exact pins | `requirements.txt`, `requirements-dev.txt` |

## 2. Code Requirements

- **Types:** every function parameter and return value is annotated. Tests may omit annotations (a mypy override allows it).
- **Docstrings:** Google style for public modules, classes and functions, with Args, Returns and Raises sections where they apply.
- **Errors:** catch specific exceptions; never a bare `except:` or a silent `pass`. Ruff's `TRY` rules enforce the rest. User-facing error text follows the `user-facing-error-messages` skill.
- **Logging:** use `logging` for diagnostics; `print` is for the CLI's own output in `cli.py`. Never log a secret or a bearer token.
- **Database:** go through `grocery_butler/db/adapter.py` with parameterized queries. A schema change is a numbered migration pair, `NNN_name.sql` and `NNN_name_pg.sql`, in `grocery_butler/db/migrations/`. Obsolete tables are archived, not dropped (ADR 0001).
- **Prompts:** Claude prompt templates live in `grocery_butler/prompts/` and load through `prompt_loader.py`, not inline strings.
- **ADRs:** a significant decision gets an ADR in `docs/adr/` and a row in its README index.

## 3. Forbidden Patterns

Each of these hides a problem instead of fixing it. The fix for each is in [Troubleshooting](troubleshooting.md).

1. A suppression comment (`# noqa`, `# type: ignore`, `# pragma: no cover`, `# nosec`) added to get green
2. A test that is commented out, skipped, retried or quarantined to get green
3. A coverage `omit` entry or a lowered threshold
4. A `TODO` with no issue number
5. A literal API key, token or password anywhere in the repo
6. Retailer credentials, private retailer endpoints, or checkout and payment code (ADR 0001)
7. A model inference or photo that mutates inventory directly instead of creating a proposal

The only accepted suppressions are the genuine exceptions in the `max-quality-no-shortcuts` skill, each with its reason, a reference and a review date.

## 4. Security Baseline

The `security` skill holds the general patterns. In this repo:

- **Secrets** come from the environment through `grocery_butler/config.py`. With `APP_ENV=production`, `create_app()` refuses to boot without `FLASK_SECRET_KEY` and `RUBOTPAUL_SHARED_SECRET`. Keep it that way.
- **`/api/v1`** uses HMAC bearer tokens bound to the method, path and body (`auth_middleware.py`), and auth is on by default. A new endpoint is authenticated unless an ADR says otherwise.
- **Network boundary:** `network_guard.py` keeps the API on the tailnet. README "Network boundary guard" explains it.
- **Server-side truth:** never trust a price, total or quantity the client sends when the server can compute or store it.
- **Errors:** log the detail server-side and return a generic message. Never return an exception's text or a traceback.
- **Templates:** rely on Jinja2 autoescaping; never mark user input `|safe`.

---

**Navigation**: [Back to CLAUDE.md](../../CLAUDE.md) | [Principles](principles.md) | [Workflow](workflow.md)
