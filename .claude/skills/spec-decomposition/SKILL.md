---
name: spec-decomposition
description: >-
  Decompose a spec, backlog epic, ADR, or design doc into a sequenced set of
  GitHub epics and child issues, where each issue is a self-contained prompt
  an agent or engineer can execute end-to-end. Use when the user says
  "decompose this spec", "break this epic into issues", "file issues from the
  backlog", or "plan the implementation". In this repo the usual source is
  docs/OPENCLAW_ERP_BACKLOG.md, read with docs/adr/ and README.md. Each issue
  is shaped as a 6-component prompt (Role / Goal / Context / Format / Examples
  / Constraints), sequenced via tracer-code so the skeleton lands first and
  every later issue keeps the system demoable, and gated by stay-green
  Done-Done plus max-quality-no-shortcuts anti-bypass rules. Issue bodies are
  drafted outside the repo and filed via `gh issue create --body-file`,
  matching the file-based pattern in git-workflow. Do NOT use for single-task
  issue or PR creation (use git-workflow), iterating on PR review feedback
  (use address-feedback), or choosing between technical approaches inside a
  spec (use architectural-decisions or decide).
metadata:
  author: Geoff
  version: 1.0.0
---

# Spec Decomposition

Turn a spec into a sequenced set of GitHub epics and child issues. Each issue is a complete prompt: anyone (human or agent) can pick it up and execute it without re-reading the spec.

This skill stitches four other skills together:

- **6-component prompt frame** (defined below): every issue body uses Role / Goal / Context / Format / Examples / Constraints.
- **tracer-code**: issues are ordered so the skeleton lands first and every later issue preserves a working, demoable system.
- **stay-green**: every issue ends with explicit Done-Done gates.
- **max-quality-no-shortcuts**: every issue forbids `noqa`, `type: ignore`, and similar bypasses.

## Sources in This Repo

- **`docs/OPENCLAW_ERP_BACKLOG.md`**: the product backlog. It already defines epics (Epic 0 to Epic 8) and items with ids and sizes (for example `### P0.5 Observation ledger and derived snapshot`, size L), plus the "Safety and product principles", "Target workflow" and "Recommended delivery sequence". Reuse its epic names and item ids; don't invent a parallel numbering.
- **`docs/adr/`**: settled decisions. An issue must not contradict an accepted ADR. ADR 0001 sets the product boundary: no private retailer integrations, no checkout or payment code.
- **`README.md`**: the current architecture, API and data model, which give each issue's "state of the world".
- `plans/` is superseded history. Don't decompose from it.

Drafts go in `$DRAFTS`: the session's scratchpad directory if one is listed, otherwise `${TMPDIR:-/tmp}/git-issues/`. They stay out of the repo; the filed GitHub issues are the shared record.

## Instructions

### Step 1: Read the Spec End-to-End

Read the whole source before writing anything, plus the ADRs it touches. Note:

- Goals, non-goals, and success criteria.
- External constraints (dependencies, the deploy target, the `/api/v1` contract OpenClaw relies on).
- Open questions or under-specified areas. Flag these to the user before decomposing; do not invent answers.

If material questions remain, **stop and ask the user**. A decomposition built on guesses produces a backlog of guesses.

### Step 2: Check What Already Exists

Issues may already be filed for some backlog items, by you or another session:

```bash
gh issue list --state all --limit 200 --search "P0.5 in:title,body"
gh label list
```

Skip or link anything already filed. Never file a duplicate.

### Step 3: Identify Epics (Workstreams)

Group the surface area into 2-6 epics. When the source is the backlog, its epics are the epics. Otherwise an epic is a vertical workstream that:

- Delivers user-visible or system-visible value on its own.
- Has a single owner-shaped outcome ("Inventory observations are recorded with provenance", not "Refactor utils").
- Can be sequenced relative to other epics.

If you have one giant epic, split it. If you have ten tiny epics, merge them.

### Step 4: Sequence Issues Within Each Epic Using Tracer-Code

Inside each epic, the **first issue is the skeleton issue**. Then real-feature issues replace stubs one at a time.

| Phase | Issue Type | Done-Done Looks Like |
|-------|-----------|----------------------|
| Skeleton | Wire every endpoint, page or CLI command with stubs returning typed data | Smoke tests prove every surface returns the right shape |
| Core | Replace P0 stubs with real logic | Feature works end-to-end with realistic input |
| Edges | Validation, error paths, edge cases | Negative tests pass; meaningful errors returned |
| Polish | Logging, docs, performance | No new features; tighten what exists |

If the skeleton can't be defined in one issue, the epic is too large: split it. See `references/sequencing-patterns.md` for worked sequences.

### Step 5: Write Each Issue as a 6-Component Prompt

The issue is not a description of work; it is a **prompt** an agent can execute:

1. **Role**: who should pick this up (e.g., "Senior Python engineer working in this repo's Flask `/api/v1` blueprint").
2. **Goal**: the specific, measurable outcome.
3. **Context**: file paths, the backlog item and ADRs, parent epic, the current state of those files.
4. **Output Format**: the deliverable (code change, tests, README or ADR update).
5. **Examples**: a minimal request and response, or a test that should pass.
6. **Constraints**: anti-bypass rules, scope fences, dependency ordering, and the design rules from `CLAUDE.md` the issue must not break.

The full body template is in `references/templates.md`.

### Step 6: Embed Done-Done Gates (stay-green)

Every issue ends with an explicit Done-Done block that points at the gates in `CLAUDE.md`: TDD, `./scripts/check-all.sh` exits 0, `./scripts/precommit-changed.sh` is clean, CI gate is green, and the review verdict allows a merge. The PR carries `Refs #<epic>` and `Closes #<this-issue>`.

### Step 7: Forbid Bypasses (max-quality-no-shortcuts)

Every issue's Constraints section includes the anti-bypass clause, copied verbatim from `references/templates.md`.

### Step 8: Write All Files to `$DRAFTS` First

Lay out the full decomposition on disk before calling `gh`, so the user can review it as a whole:

```
$DRAFTS/
├── 2026-11-26_SPEC_summary.md        # one-page restatement of the source
├── EPIC_01_observation-ledger.md
├── EPIC_01_ISSUE_01_skeleton.md
├── EPIC_01_ISSUE_02_record-observation.md
└── ...
```

`EPIC_NN` and `ISSUE_NN` are sequence numbers within the decomposition; they get replaced with real GitHub issue numbers after filing. Keep slugs short and stable.

**Show the user the full file list and at least one epic body and one issue body before filing.** Filing is hard to undo, so a dry run saves hours.

### Step 9: File Epics First, Then Children with Parent Refs

Epics must exist before children can reference them. File only after the user approves.

```bash
# 1. Create only the labels that are missing.
gh label create "epic" --description "Tracks a workstream from a spec" --color "5319e7"

# 2. File each epic and capture its number.
EPIC_01=$(gh issue create \
  --title "epic: Observation ledger" \
  --body-file "$DRAFTS/EPIC_01_observation-ledger.md" \
  --label "epic" \
  --json number --jq .number)

# 3. Substitute the real epic number into every child body, then file.
sed -i "s/EPIC_01_NUMBER/$EPIC_01/g" "$DRAFTS"/EPIC_01_ISSUE_*.md

gh issue create \
  --title "feat(api): wire observation endpoints with stubbed responses" \
  --body-file "$DRAFTS/EPIC_01_ISSUE_01_skeleton.md" \
  --label "tracer-code"
```

Then update each epic's body with its child checklist (`gh issue edit <epic> --body-file <updated-file>`).

### Step 10: Hand Off

Tell the user:

- The epics filed, with numbers.
- The child issues filed.
- The recommended **first issue**: the skeleton of the highest-priority epic, following the backlog's delivery sequence.

## Examples

### Example 1: Decomposing a Backlog Epic

Source: a backlog epic with several items, such as Epic 1's P0.4 (canonical household item identity), P0.5 (observation ledger and derived snapshot) and P0.6 (replenishment need state machine).

One GitHub epic for the backlog epic, with children sequenced by tracer-code:

1. `feat(api): wire observation and need endpoints with stubbed responses`: skeleton
2. `feat(pantry): record observations with provenance and derive the snapshot`: core (P0.5)
3. `feat(pantry): replenishment need state machine`: core (P0.6)
4. `feat(api): validate observation payloads and return clear errors`: edges
5. `docs: document the observation API in the README`: polish

Each child names its backlog item in Context, and its Constraints carry the design rule it protects (here: "evidence before mutation").

### Example 2: When the Spec Is Under-Specified

The user pastes: "Add photo inventory."

Stop. Reply with the gap analysis: who reviews a low-confidence extraction, where photos are stored and for how long, what an observation looks like, and which backlog items (Epic 3) already answer part of it. Ask about the two or three most blocking gaps.

## Troubleshooting

### An issue body is missing 3+ of the 6 components

Rewrite it. If you cannot fill Context or Examples, the issue is premature: fold it back into its parent epic until the scope is clear.

### An epic has more than ~8 child issues

Split the epic. Too many in-flight issues means the system is no longer green between them.

### You cannot define a skeleton issue for an epic

The epic is not vertically sliced. Re-scope it as a thin slice that delivers one observable behavior end-to-end, or demote it to a follow-up that runs after the vertical slices land.

### Issue numbers in cross-references are wrong after filing

You filed children before epics or skipped the `sed` substitution. Fix each issue with `gh issue edit <N> --body-file <fixed-file>`.
