# Claude Code Project Context: Grocery Butler

---

## What This Project Is

**Grocery Butler** is the household's replenishment memory: what we keep, what we probably have, what we need, and why. It is the durable system of record for that work. **OpenClaw** (today through RubotPaul, the household's OpenClaw assistant) is its conversational and visual operator, and people stay in control.

It is a Python-only small monolith: a Flask web dashboard and the `/api/v1` blueprint OpenClaw calls, a CLI, and a Discord bot that is retired from production but kept in the codebase. One domain layer sits behind those channels, and one database: SQLite in development, Postgres in production, both through `grocery_butler/db/adapter.py`. Claude (the Anthropic API) parses recipes and inventory updates; it never owns state.

**It recently pivoted** ([ADR 0001](docs/adr/0001-pivot-from-private-safeway-api-to-authorized-fulfillment-adapters.md)). The private Safeway ordering runtime is gone: no retailer credentials, no private retailer HTTP calls, no checkout. Fulfillment goes through retailer-neutral adapters with declared capabilities, starting with a plain list export, and no adapter is ever built from undocumented endpoints. A human decides what gets purchased, every time, in the retailer's own checkout.

Read before designing anything:

- [`README.md`](README.md): what it does, architecture, ownership boundary, key design rules, API, data model, deployment.
- [`docs/adr/`](docs/adr/README.md): settled decisions. Record each new architecture decision as an ADR (use `architectural-decisions`).
- [`docs/OPENCLAW_ERP_BACKLOG.md`](docs/OPENCLAW_ERP_BACKLOG.md): product direction, epics, safety and product principles, delivery sequence.

`plans/` holds historical build plans that ADR 0001 supersedes. Don't build from them.

**Design rules that never bend** (full list: README "Key Design Rules"): inventory status overrides pantry staples; Claude is a parser, not a planner; evidence before mutation; draft, confirm, execute for anything money-moving or destructive; retailers are adapters; never claim a purchase without evidence.

---

## Critical Principles

**Details: [.claude/docs/principles.md](.claude/docs/principles.md)**

1. **Use project scripts, not direct tools.** Run `./scripts/*`.
2. **Don't duplicate content (DRY).** Link to the canonical source.
3. **No shortcuts.** Fix root causes and never bypass quality checks.
4. **Never add `# noqa`, `# type: ignore` or other suppressions to get green; fix the root cause.**
5. **A flaky test is worse than no test.** Fix its root cause, decouple what it tests, or delete it with a stated reason. Never retry, quarantine or skip it to get green. Unit tests isolate their variable.
6. **Never put a literal API key or token in any file.** Secrets reach the code only through environment variables.
7. **Stay Green.** Never request review with failing checks.
8. **Work from the project root** and use relative paths.

**The gates** (this list is canonical; other docs link here):

1. **TDD:** a failing test first, then the code.
2. **`./scripts/check-all.sh` exits 0.** It runs lint and docstring coverage, format, mypy, security, complexity, unit tests, coverage and the package build.
3. **Pre-commit is clean on the changed files:** `./scripts/precommit-changed.sh`. The commit hook already does this for staged files.
4. **CI is green.** One job, **CI gate**, is the check to require (`main` has no branch protection yet). It passes only when the Python gate (`./scripts/check-all.sh` on 3.11, 3.12 and 3.13, with a Postgres service) and pre-commit on the changed files both pass. CI runs on pull requests, nightly on `main`, and on manual dispatch. A merge to `main` starts no run.
5. **The review bot passes.** See [Review Verdicts](#review-verdicts).

**Mutation testing is recommended before a release, not a merge gate.** Run `./scripts/mutation.sh`, or dispatch CI by hand; aim for ≥80%.

---

## Quality Standards

**Details: [.claude/docs/quality-standards.md](.claude/docs/quality-standards.md)**

| Metric | Threshold | Tool |
|--------|-----------|------|
| Line coverage | ≥90% | pytest-cov |
| Branch coverage | ≥90% | pytest-cov (`scripts/coverage.sh`) |
| Docstring coverage | ≥95% | interrogate |
| Cyclomatic complexity | ≤10 per function | radon / xenon |
| Type errors | 0 | mypy (strict flags in `pyproject.toml`) |
| Lint and format | 0 violations | Ruff |
| Security findings | 0 | bandit, pip-audit |
| Mutation score (before a release; not a merge gate) | ≥80% | mutmut |

**One set of rules.** CI runs the same scripts with the exact tool versions pinned in `requirements-dev.txt`. Thresholds and scope live in `pyproject.toml` and `scripts/`, never in `ci.yml`. Change a rule there, once.

---

## Layout

```
grocery-butler/
├── grocery_butler/         # The package
│   ├── api.py              # /api/v1 blueprint (OpenClaw); HMAC bearer auth via auth_middleware.py
│   ├── api_v2.py           # /api/v2 capability blueprint (contract: docs/api/v2.md); members.py, idempotency.py
│   ├── app.py              # Flask app factory and HTML dashboard
│   ├── cli.py              # argparse CLI
│   ├── bot.py              # Discord bot (local only; retired from production)
│   ├── meal_parser.py, consolidator.py, pantry_manager.py, household_items.py, inventory_ledger.py,
│   │   replenishment.py, observation_batches.py, approvals.py, shopping_plans.py,
│   │   constraints.py, data_lifecycle.py, reconciliation.py, check_reminders.py,
│   │   recipe_store.py, shopping_list_store.py, pending_actions.py   # Domain services
│   ├── fulfillment/        # Adapter interface (base.py), registry.py, runner.py, export.py (list export),
│   │                       # handoffs.py (draft, approve, execute), instacart/ (only registry.py may import it)
│   ├── observability.py, metrics.py, retries.py   # Audit events, operational metrics, bounded retries
│   ├── models.py, units.py, config.py, network_guard.py
│   ├── claude_utils.py, prompt_loader.py, prompts/   # Anthropic wrapper and prompt templates
│   ├── db/                 # adapter.py (SQLite/Postgres), migrate.py, migrations/
│   ├── templates/          # Jinja2 templates
│   └── static/             # Pico CSS and assets
├── tests/                  # pytest suite; tests/e2e/ wires real components
├── scripts/                # Quality gate scripts; graph/ for the code graph
├── docs/adr/               # Architecture decision records
├── docs/OPENCLAW_ERP_BACKLOG.md   # Product backlog
├── plans/                  # Historical plans (superseded by ADR 0001)
├── .claude/docs/           # Detailed dev guidance (principles, workflow, testing, tools)
├── .claude/skills/         # Project skills (<name>/SKILL.md)
├── .claude/agents/         # Subagent profiles
├── .github/workflows/      # ci.yml, code-review.yml, claude.yml, graph-build.yml
├── Dockerfile, docker-entrypoint.sh   # Production image: migrate, then gunicorn
├── deploy/vps/             # compose.yaml for the household's server (ADR 0021)
├── pyproject.toml          # Tool configuration
└── requirements*.txt       # Pinned runtime and dev dependencies
```

Business rules live in the domain services, never in `api.py`, `app.py`, `cli.py` or `bot.py`. Schema changes are numbered migrations in both dialects (`NNN_name.sql` and `NNN_name_pg.sql`).

---

## Development Workflow

**Details: [.claude/docs/workflow.md](.claude/docs/workflow.md)** · commands: [.claude/docs/tools.md](.claude/docs/tools.md) · tests: [.claude/docs/testing.md](.claude/docs/testing.md)

```bash
python3 -m venv .venv && . .venv/bin/activate
pip install -r requirements-dev.txt -e .
pre-commit install --install-hooks -t pre-commit -t commit-msg

git checkout -b feature/<issue>-<slug>
# TDD: failing test, minimum code, refactor
./scripts/format.sh
./scripts/check-all.sh          # must exit 0
./scripts/precommit-changed.sh  # must pass
git commit -m "feat(pantry): track observation provenance (#123)"
gh pr create --fill
```

Postgres-only tests skip unless `TEST_DATABASE_URL` points at a throwaway Postgres database. CI sets it.

---

## Common Mistakes

**Details: [.claude/docs/troubleshooting.md](.claude/docs/troubleshooting.md)**

1. Skipping local checks, or committing with `--no-verify`
2. Lowering thresholds or adding coverage `omit` rules instead of writing tests
3. Invoking tools directly instead of `./scripts/*`
4. Commenting out, skipping or retrying a failing test
5. Adding a suppression comment to get green
6. **Domain-specific:** reintroducing retailer credentials, private retailer endpoints or checkout code (ADR 0001), letting a model inference mutate inventory without a proposal, or putting business rules in a channel adapter

---

## Skills

Project skills live in `.claude/skills/<name>/SKILL.md`.

| Stage | Skills |
|-------|--------|
| Decide | [decide](.claude/skills/decide/SKILL.md) (interactive decision sheet), [architectural-decisions](.claude/skills/architectural-decisions/SKILL.md) (trade-offs, then an ADR) |
| Plan | [spec-decomposition](.claude/skills/spec-decomposition/SKILL.md) (backlog or spec into epics and issues), [tracer-code](.claude/skills/tracer-code/SKILL.md), [backlog-grooming](.claude/skills/backlog-grooming/SKILL.md), [prompt-engineering](.claude/skills/prompt-engineering/SKILL.md), [file-naming-conventions](.claude/skills/file-naming-conventions/SKILL.md) |
| Build | [stay-green](.claude/skills/stay-green/SKILL.md), [testing](.claude/skills/testing/SKILL.md), [mutation-testing](.claude/skills/mutation-testing/SKILL.md), [max-quality-no-shortcuts](.claude/skills/max-quality-no-shortcuts/SKILL.md), [bug-squashing-methodology](.claude/skills/bug-squashing-methodology/SKILL.md), [error-handling](.claude/skills/error-handling/SKILL.md), [concurrency](.claude/skills/concurrency/SKILL.md), [documentation](.claude/skills/documentation/SKILL.md), [vibe](.claude/skills/vibe/SKILL.md), [frontend-aesthetics](.claude/skills/frontend-aesthetics/SKILL.md), [user-facing-error-messages](.claude/skills/user-facing-error-messages/SKILL.md), [skill-craft](.claude/skills/skill-craft/SKILL.md) |
| Secure | [security](.claude/skills/security/SKILL.md), [cve-remediation](.claude/skills/cve-remediation/SKILL.md) |
| Ship | [git-workflow](.claude/skills/git-workflow/SKILL.md), [review-diff](.claude/skills/review-diff/SKILL.md) (self-review before the PR), [comprehensive-pr-review](.claude/skills/comprehensive-pr-review/SKILL.md), [await-claude-review](.claude/skills/await-claude-review/SKILL.md), [address-feedback](.claude/skills/address-feedback/SKILL.md), [ci-debugging](.claude/skills/ci-debugging/SKILL.md) |

### Review Verdicts

`.github/workflows/code-review.yml` posts one comment per review as `github-actions[bot]`. The comment starts with `<!-- grocery-butler-review pr=N -->` and ends with `## Verdict: LGTM | CHANGES_REQUESTED | COMMENTS`. Trust a verdict only from that author, with that marker, unedited by anyone else, and posted after the current HEAD. A PR that edits `code-review.yml` gets no automated verdict (the action skips itself), so it needs a manual review.

- `LGTM`: merge on green CI.
- `CHANGES_REQUESTED`: fix with TDD, push once, then wait for the new verdict.
- `COMMENTS`: **never iterate.** File each actionable finding as a follow-up issue labeled `P0` through `P3` by severity, then merge on green CI. `P0` covers security, data loss, crashes and privacy leaks. `P3` covers nits.
- Any other value: don't merge; surface it to the user.

The procedure is in `address-feedback`. The workflow, `await-claude-review` and `address-feedback` must change together.

---

## Knowledge Graph (graphify)

A code graph of the repo. When `graphify-out/graph.json` exists, prefer it to broad grep and read sweeps:

- Get it with `./scripts/graph/restore.sh` (the published graph of `main`) or `./scripts/graph/build.sh`.
- Ask `graphify query "<question>"` first; use `graphify path "A" "B"` for relationships, `graphify explain "X"` for a concept, and `graphify affected "X"` for change impact.
- Quote each node's `source_location` when you cite it.
- After changing code, refresh with `./scripts/graph/update.sh`.
- If there's no graph, proceed without it.

---

## Appendix: Delegating to Subagents

Give each subagent:

1. **The task and why it matters.** Name the ADR, backlog item (for example P0.5) or issue it serves.
2. **Context:** the files, related issues and ADRs, and the design rules above that apply.
3. **Safety and privacy impact:** whether the change touches household data, inventory evidence or photos, approvals and `pending_actions`, `/api/v1` auth, or a retailer handoff, checked against the backlog's "Safety and product principles".
4. **Acceptance criteria:** the gates above pass, public APIs have docstrings, and an ADR exists if an architectural decision was made.
5. **Open questions:** any product decision the task depends on that the backlog or an ADR leaves open, flagged explicitly rather than silently picked.

**Run only parallelizable work at once.** A lane is a subagent or session that opens a PR.

- Before giving a lane a task, list the files it will likely change: the issue's named files, plus a quick grep of the code it names.
- Compare them with the files each unmerged branch changes, pushed or still local: `git diff --name-only $(git merge-base origin/main <head>) <head>`.
- On any overlap, the task waits. Leave the lane empty instead: each conflict between in-flight PRs forces a re-push, and every push re-runs CI.
- Shared files count as overlap: `grocery_butler/models.py`, `grocery_butler/api.py`, `grocery_butler/db/adapter.py`, and the next migration number in `grocery_butler/db/migrations/`.
- If two in-flight branches overlap anyway, sequence them. The later one waits until the earlier one merges, then merges `main` locally and pushes once.
