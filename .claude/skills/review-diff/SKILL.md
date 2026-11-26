---
name: review-diff
description: >-
  Perform a thorough self-review of the current branch's changes against its
  issue/spec before opening a PR: logic errors, missing edge cases,
  architectural misalignment, unclear naming, and incomplete implementations
  automated tools miss. Use when the user says "review diff", "review my
  changes", "self review", "check my work", or "am I ready for PR". Do NOT use
  for reviewing someone else's PR (the automated review runs in
  `.github/workflows/code-review.yml`; use comprehensive-pr-review for a
  manual one), for running the quality-gate suite
  itself (use stay-green / `./scripts/check-all.sh`), or for a targeted test
  run (use `./scripts/test.sh`).
metadata:
  author: Geoff
  version: 1.0.0
---
# Review Diff

Perform a thorough self-review of the current branch's changes before opening a
PR. Catch issues that automated tools miss: logic errors, missing edge cases,
architectural misalignment, unclear naming, and incomplete implementations.

---

## Trigger

Activate when the user says any of:
- "review diff"
- "review my changes"
- "self review"
- "check my work"
- "am I ready for PR"

---

## Instructions

### 1. Gather the Diff

```bash
# What branch are we on?
git branch --show-current

# What's the full diff against main?
git diff main...HEAD

# What commits are included?
git log main..HEAD --oneline

# Any uncommitted changes?
git status
git diff
```

### 2. Read the Issue Specification

Identify which issue this branch implements from the branch name (e.g.,
`feature/123-observation-ledger`) or the commit messages, and read it:

```bash
gh issue view <number>
```

Load the acceptance criteria. These are your review checklist. If the issue
cites a backlog item (`docs/OPENCLAW_ERP_BACKLOG.md`) or an ADR (`docs/adr/`),
read that section too.

### 3. Review Dimensions

Evaluate the diff across each of these dimensions. For each finding, note the
file, line number, and specific concern.

#### Correctness
- Does the code actually do what the issue asks for?
- Are there off-by-one errors, wrong comparisons, inverted conditions?
- Are error cases handled? What happens on empty input, None, 0, negative?
- Are database transactions committed or rolled back on every path?
- Does it behave the same on SQLite and Postgres (`grocery_butler/db/adapter.py`)?

#### Completeness
- Is every acceptance criterion addressed?
- Are there tasks in the issue that were skipped or partially done?
- Are there new code paths missing test coverage, line or branch?
- If a new `/api/v1` endpoint was added, is it on the `api_v1` blueprint and
  documented in the README's API section?
- If the schema changed, is there a numbered migration in both dialects
  (`NNN_name.sql` and `NNN_name_pg.sql`) with a test?
- If the CLI, dashboard or API behavior changed, does the README still match?

#### Product Boundary and Design Rules
- Does the change stay inside what Grocery Butler owns (ADR 0001)? No retailer
  credentials, private retailer endpoints, checkout or payment code.
- Do model inferences and photos create proposals instead of mutating
  inventory directly?
- Do money-moving or destructive actions go through draft, confirm, execute
  (`pending_actions.py`)?
- Do business rules live in the domain services, not in `api.py`, `app.py`,
  `cli.py` or `bot.py`?

#### Types and Contracts
- Are function signatures fully typed (no implicit `Any`)?
- Do Pydantic models in `models.py` match the actual API contract?
- Are Optional fields handled correctly (not assumed to be present)?

#### Naming and Clarity
- Do variable and function names accurately describe their purpose?
- Would a new team member understand this code without explanation?
- Are there abbreviations or acronyms that should be spelled out?
- Are boolean variables named as questions (`is_active`, `has_completed`)?

#### Patterns and Consistency
- Does new code follow existing patterns in the codebase?
- Are imports organized the same way as neighboring files?
- Are error responses consistent with existing endpoint conventions?
- Is the test structure consistent with existing tests?

#### Security
- Is user input validated at the boundary?
- Are SQL queries parameterized?
- Is every new `/api/v1` route behind the HMAC bearer auth
  (`auth_middleware.py`), with tests for missing and wrongly bound tokens?
- Does the server compute or store prices, totals and quantities instead of
  trusting the client?
- Do error responses hide exception text and tracebacks?
- Are secrets kept out of code, tests, fixtures and docs?

#### Performance
- Are there N+1 query patterns in new database code?
- Are there unnecessary database round-trips or repeated Claude calls?

### 4. Check Test Quality

Read every new or modified test. For each test, verify:

- It tests behavior, not implementation details
- It would fail if the feature broke (not a tautology)
- Edge cases are covered (empty, null, boundary values, error states)
- Test names clearly describe what they verify
- Test data is minimal and purposeful (no copy-pasted blobs)

### 5. Verify the Local Gates

```bash
./scripts/check-all.sh
./scripts/precommit-changed.sh
```

If either fails, fix the root causes (see `max-quality-no-shortcuts`) and re-run until both pass before continuing the review.

### 6. Present the Review

Structure your findings as:

#### Summary
One paragraph on the overall quality and completeness of the changes.

#### Acceptance Criteria Checklist
```
- [x] Criterion 1: met (file:line)
- [x] Criterion 2: met (file:line)
- [ ] Criterion 3: NOT met, because...
```

#### Findings (if any)
Group by severity:

**Must fix**: Issues that would block a PR:
- Correctness bugs, missing acceptance criteria, security issues, product-boundary breaks

**Should fix**: Issues worth addressing before merge:
- Missing edge case tests, unclear naming, minor type issues

**Consider**: Suggestions for improvement, not blockers:
- Style preferences, alternative approaches, future considerations

#### Verdict
One of:
- **Ready for PR**: all criteria met, no must-fix issues
- **Almost ready**: minor fixes needed, list them
- **Needs more work**: significant gaps, describe what's missing

---

## Rules

- Be honest. The point of self-review is to catch problems, not to rubber-stamp.
- Reference specific files and line numbers for every finding.
- If you find a bug, fix it; don't just report it.
- If the verdict is "Ready for PR," say so with confidence.
