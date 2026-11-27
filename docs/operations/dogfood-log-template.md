# Dogfood log: week N

Copy this file to `docs/operations/dogfood/week-N.md`. Counts and short
notes only: no photos, no receipts, no shopping history. What each field
means and which pilot gate it feeds is in
[`dogfood-protocol.md`](dogfood-protocol.md).

- **Week:** N of 4
- **Dates:** YYYY-MM-DD to YYYY-MM-DD
- **Filled in by:**

## One-time setup (week 1 only)

- [ ] Deployed, tailnet boundary confirmed (#110)
- [ ] Household members added
- [ ] Hard constraints entered and checked
- [ ] `backup_taken`: first backup stored off the server
- [ ] `restore_drill_done`: restored into a scratch database and inspected
- [ ] `deletion_test_done`: scratch database deleted and confirmed empty

## Kitchen scan

| Field | Value |
|-------|-------|
| `scan_done` | yes / no |
| `scan_review_surface` | conversation / phone screen |
| `scan_proposals` | |
| `scan_confirmed` | |
| `scan_corrected` | |
| `scan_rejected` | |
| `scan_missed` (on the shelf, never proposed) | |
| `scan_review_seconds` | |

Notes:

## Ad hoc updates

| Field | Value |
|-------|-------|
| `adhoc_updates` | |
| `adhoc_wrong` (wrong item or wrong state) | |

Notes:

## Shopping plan and handoff

| Field | Value |
|-------|-------|
| `plan_version` | |
| `plan_lines` | |
| `plan_false_suggestions` | |
| `plan_missing_items` | |
| `plan_review_minutes` | |
| `handoff_path` | list export / Instacart link |
| `unenforced_hard_constraints_shown` | yes / no / none on the plan |

Notes:

## Reconciliation

| Field | Value |
|-------|-------|
| `reconcile_done` | yes / no |
| `reconcile_minutes` | |
| `substitutions` | |
| `skipped_items` | |
| `received_without_evidence` (must be 0) | |

Notes:

## Metrics (`GET /api/v2/operations/metrics`)

| Field | Value |
|-------|-------|
| `extraction_failures` (last 7 days) | |
| `adapter_failures` (last 7 days) | |
| `correction_rate` | |
| `stale_inventory` | |
| `reconciliation_gaps` | |
| `handoffs.created` / `opened` / `abandoned` / `reconciled` | |

## Weekly checks

| Field | Value |
|-------|-------|
| `unsupported_retailer_api_check` | pass / fail |
| `changes_total` | |
| `changes_with_actor_and_source` | |
| `backup_taken` (copied off the server; give the file's date) | yes, dated ____ / no |
| `restore_drill_done` (weeks 1 and 4) | yes / no / not this week |
| `support_minutes` (time spent fixing or working around the tool) | |

## In the household's words

`avoided_mental_work`: what didn't you have to think about this week?

What was annoying?

Did anyone not trust a suggestion? Which one, and why?
