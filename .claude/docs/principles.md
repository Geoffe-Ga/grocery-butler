# Critical Principles

**Navigation**: [Back to CLAUDE.md](../../CLAUDE.md) | [Quality Standards](quality-standards.md)

---

The short list and the gates are in [`CLAUDE.md`](../../CLAUDE.md#critical-principles). This page explains each principle.

## 1. Use Project Scripts, Not Direct Tools

Invoke tools through `./scripts/*`. The scripts carry the flags, scope and thresholds, and CI runs the same scripts, so a check can't pass on a laptop and fail in CI. [Tools](tools.md) lists every script and the few cases where calling a tool directly is fine.

## 2. DRY: One Source of Truth

Each fact lives in one place, and everything else links to it.

- Gates: `CLAUDE.md`.
- Thresholds: `pyproject.toml` and `scripts/*.sh`, summarized in [Quality Standards](quality-standards.md).
- Product direction: `docs/OPENCLAW_ERP_BACKLOG.md`. Decisions: `docs/adr/`. Architecture and API: `README.md`.

Duplicated docs drift, and then one of them is wrong.

## 3. No Shortcuts: Fix Root Causes

When a check fails, fix why it fails. Forbidden:

- Commenting out, skipping or deleting a failing test to get green
- Adding `# noqa`, `# type: ignore`, `# pragma: no cover` or any other suppression to get green
- Lowering a threshold, or adding a coverage `omit` rule for code that's hard to test
- `git commit --no-verify`
- Deleting error handling or validation to cut complexity

Refactor instead: extract helpers, add the missing types, write the missing tests. The `max-quality-no-shortcuts` skill covers the rare genuine exception (a third-party bug, for example) and what such a comment must document. Getting green is never one of them.

## 4. Flaky Tests

A flaky test is worse than no test, because a red that means nothing teaches everyone to ignore red. Fix its root cause, decouple what it tests, or delete it with a stated reason. Never retry, quarantine or skip it to get green.

Unit tests isolate their variable: one behavior per test, with time, randomness, the network and the Anthropic client under the test's control. [Testing](testing.md) shows how.

## 5. Secrets Never Live in Files

Never put a literal API key or token in any file: code, tests, docs, fixtures, prompts or commit messages. Secrets reach the code only through environment variables, read by `grocery_butler/config.py`: a gitignored `.env` locally (start from `.env.example`, which holds placeholders only) and Railway variables in production. Tests set obviously fake values with `monkeypatch.setenv`. The `detect-secrets` hook guards the rest.

## 6. Stay Green

Never request review with failing checks, and never merge past a verdict that doesn't allow it. The gates are in `CLAUDE.md`; [Workflow](workflow.md) walks through them.

## 7. Quality First

Thresholds are fixed. When code doesn't meet them, improve the code. [Quality Standards](quality-standards.md) has the numbers.

## 8. Work from the Project Root

Run everything from the repository root with relative paths: `./scripts/check-all.sh`, `pytest tests/test_consolidator.py`. The scripts `cd` to the root themselves, and CI runs from the root.

## 9. Respect the Product Boundary

Grocery Butler owns household items, inventory evidence, needs, plans, approvals and audit history. It doesn't own a retailer's catalog, prices, cart, payment or checkout ([ADR 0001](../../docs/adr/0001-pivot-from-private-safeway-api-to-authorized-fulfillment-adapters.md)). A change that blurs that line, or breaks one of the README's "Key Design Rules", needs an ADR before code.

---

**Navigation**: [Back to CLAUDE.md](../../CLAUDE.md) | [Quality Standards](quality-standards.md)
