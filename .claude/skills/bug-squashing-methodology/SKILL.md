---
name: bug-squashing-methodology
description: >-
  Structured 6-step bug fix process with reproduction, root cause analysis,
  and TDD. Use when fixing bugs, debugging failures, or investigating defects.
  Covers reproduction, RCA documentation, TDD fix cycle, and PR workflow.
  Do NOT use for general feature development (use stay-green), CI
  environment issues (use ci-debugging), review feedback (use
  address-feedback), or general git workflow like issue creation,
  branching, and PRs (use git-workflow).
metadata:
  author: Geoff
  version: 1.0.0
---

# Bug Squashing Methodology

Systematic process for fixing bugs: Document, Understand, Fix, Verify. Never skip straight to coding.

## Instructions

### Step 1: Reproduce First

Before writing anything down, reproduce the bug with a failing test or a
direct repro command: capture the exact error message, stack trace, and
input. An RCA written from a bug report's prose (not a real reproduction) is
guesswork; the reproduction is what makes every later step trustworthy.

### Step 2: Root Cause Analysis (RCA)

Write the RCA in a scratch file (not in the repo; it becomes the issue body in Step 3) with:
- **Problem Statement**: Error message, reproduction steps
- **Root Cause**: Exact line/logic causing failure
- **Analysis**: Why it happens, what was confused/wrong
- **Impact**: Severity, scope, frequency
- **Contributing Factors**: Why wasn't it caught earlier?
- **Fix Strategy**: Options with recommendation
- **Prevention**: How to avoid similar bugs

### Step 3: File a GitHub Issue

```bash
gh issue create --title "bug(component): Brief description" \
  --body-file <path-to-rca.md> \
  --label "bug"
```

### Step 4: Branch and Fix with TDD

```bash
git checkout -b bugfix/XXX-component
```

1. **Red**: Write a test that reproduces the bug (should fail)
2. **Green**: Write minimal code to fix the bug (test passes)
3. **Refactor**: Clean up the fix while keeping tests green

### Step 5: Quality Gates

Run the gates before committing (the canonical list is in `CLAUDE.md`):

```bash
# All tests pass, including the new regression test
./scripts/test.sh --all

# Every quality check passes
./scripts/check-all.sh

# Pre-commit is clean on the changed files
./scripts/precommit-changed.sh
```

If the bug touched `grocery_butler/db/`, set `TEST_DATABASE_URL` to a
throwaway Postgres database so the Postgres-only tests run instead of
skipping.

### Step 6: Commit and PR

Use conventional commit format: `fix(component): brief description (#XXX)`

Include in commit body:
- Problem description (reference RCA)
- Solution description
- Specific file changes
- Testing confirmation (every gate green)

## Examples

### Example 1: Type Validation Bug

**Problem**: API endpoint crashes when receiving single object instead of list.

**RCA**: `src/services/business_logic.py:45` assumes input is always a list. Missing type validation causes runtime error.

**Fix**:
```python
# Red: Write test reproducing the bug
def test_process_handles_single_object():
    result = process_input({"key": "value"})  # Single object, not list
    assert result == [{"key": "value"}]

# Green: Fix the code
def process_input(data):
    if isinstance(data, dict):
        data = [data]
    return [transform(item) for item in data]
```

### Example 2: Off-by-One Boundary Error

**Problem**: Pagination returns duplicate items on page boundaries.

**RCA**: `offset = page * limit` should be `offset = (page - 1) * limit` since pages are 1-indexed.

**Fix**:
```python
# Red: Test that reproduces duplicate
def test_pagination_no_duplicates():
    page1 = get_items(page=1, limit=10)
    page2 = get_items(page=2, limit=10)
    assert not set(page1) & set(page2)  # No overlap

# Green: Fix offset calculation
def get_items(page: int, limit: int):
    offset = (page - 1) * limit  # Fixed: was page * limit
    return db.query(Item).offset(offset).limit(limit).all()
```

## Troubleshooting

### Error: Can't reproduce the bug locally
- Check environment differences (Python version, OS, dependencies)
- Add debug logging at the failure point
- Try running with the exact same data/input as the report

### Error: Fix breaks other tests
- Your fix may have changed shared behavior. Check test assumptions.
- Run the full test suite, not just the new test
- Consider if the other tests were relying on buggy behavior

### Error: RCA seems like overkill for a simple bug
- Spend 10 minutes on RCA even for "obvious" bugs - they're rarely obvious
- The documentation prevents recurrence and builds institutional knowledge
- Skip RCA only for true typos (wrong variable name, missing comma)
