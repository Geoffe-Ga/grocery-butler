---
name: stay-green
description: >-
  2-gate TDD development workflow: Gate 1 is Red-Green-Refactor testing,
  Gate 2 is the local quality checks (check-all and pre-commit). Use when implementing features,
  fixing bugs, or doing any development work. Ensures code is never
  committed without passing tests and quality checks.
  Do NOT use for bug-specific debugging (use bug-squashing-methodology).
metadata:
  author: Geoff
  version: 1.0.0
---

# Stay Green

Write tests first, then code. Never declare work finished until all checks pass.

## Instructions

### Gate 1: TDD (Red-Green-Refactor)

1. **Red** - Write a failing test describing the behavior you want
   ```bash
   ./scripts/test.sh --all  # Should fail
   ```

2. **Green** - Write just enough code to make the test pass
   ```bash
   ./scripts/test.sh --all  # Should pass
   ```

3. **Refactor** - Clean up while keeping tests green
   ```bash
   ./scripts/test.sh --all  # Should still pass
   ```

Repeat for each small piece of functionality. Write tests incrementally, not all at once.

### Gate 2: Local Quality Checks

```bash
./scripts/check-all.sh          # lint, docstrings, format, types, security, complexity, tests, coverage, build
./scripts/precommit-changed.sh  # file hygiene, secrets, shellcheck, dead code, on the changed files
```

When checks fail: read errors, fix issues, run again. Repeat until all green. `./scripts/fix-all.sh` applies Ruff's automatic fixes.

The thresholds are in `CLAUDE.md` (Quality Standards): line and branch coverage >=90%, docstring coverage >=95%, complexity <=10 per function.

### Work is DONE when:
1. All tests pass (Gate 1 complete)
2. `./scripts/check-all.sh` exits 0 and pre-commit is clean (Gate 2 complete)

No exceptions.

### After Push: Await the Reviewer

This repo runs the Claude reviewer (`.github/workflows/code-review.yml`), so "done" extends past local gates: the latest verdict for HEAD must allow a merge, as `CLAUDE.md` (Review Verdicts) defines. Don't poll, don't `sleep`, and don't wait on "CI green" as a proxy: the PR webhook does **not** deliver CI passes, only comments and CI failures. Use `await-claude-review` to subscribe and end the turn; the bot's verdict comment wakes the session via `<github-webhook-activity>`. From there, `address-feedback` handles the merge gate or the fix loop, including calling `mcp__github__unsubscribe_pr_activity` once the PR merges or closes.

## Examples

### Example 1: Adding a New Function

```python
# Gate 1 - Red: Write failing test
def test_scale_quantity_doubles_for_twice_the_servings():
    result = scale_quantity(quantity=2.0, servings=8, base_servings=4)
    assert result == 4.0

# Gate 1 - Green: Make it pass
def scale_quantity(quantity: float, servings: int, base_servings: int) -> float:
    return quantity * servings / base_servings

# Gate 1 - Refactor: (already clean, move on)
# Gate 2: ./scripts/check-all.sh and ./scripts/precommit-changed.sh -> All passed!
```

### Example 2: Fixing a Formatting Failure

```bash
# Gate 2 fails on formatting
$ ./scripts/precommit-changed.sh
ruff (format, pinned)....Failed

# Auto-fix and re-run
$ ./scripts/format.sh
$ ./scripts/precommit-changed.sh
# All passed!
```

## Troubleshooting

### Error: Coverage below 90% (line or branch)
```bash
./scripts/coverage.sh --html  # See what's not covered (htmlcov/index.html)
# Add tests for uncovered lines and branches, then re-run ./scripts/check-all.sh
```

### Error: Complexity above 10
```bash
./scripts/complexity.sh  # Find complex functions
# Extract helper functions, simplify branching
# Then verify: ./scripts/complexity.sh && ./scripts/precommit-changed.sh
```

### Error: Type errors from MyPy
```bash
./scripts/typecheck.sh  # See specific errors
# Add/fix type annotations
# Then verify: ./scripts/typecheck.sh && ./scripts/precommit-changed.sh
```
