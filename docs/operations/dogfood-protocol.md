# Four-week household dogfood protocol

Backlog P1.17, issue #173. Geoff's household uses Grocery Butler for four
weeks and writes down what happened. The record decides whether to invite
design partners. The gates it has to satisfy are the "Pilot gates" in
[the backlog](../OPENCLAW_ERP_BACKLOG.md#success-metrics-and-gates).

Each week has a log: copy [`dogfood-log-template.md`](dogfood-log-template.md)
to `docs/operations/dogfood/week-N.md` and fill it in. The log holds counts
and short notes only. No photos, no receipts, no item-by-item shopping
history.

## Before week 1

Do these once. Each is a checkbox in the week 1 log.

1. **Deploy** and confirm the tailnet boundary on the live service (#110).
2. **Add the household's members** (`POST /api/v2/members`), so every change
   is attributed to a person.
3. **Enter the hard constraints**: allergens and dietary rules
   (`POST /api/v2/constraints`). Check that the one imported from the old
   dietary text says what you mean.
4. **Take a first backup** and store it off Railway
   ([runbook](data-lifecycle.md#back-up)).
5. **Run the restore drill**: restore that backup into a scratch database
   and look at it ([runbook](data-lifecycle.md#restore)).
6. **Run the deletion test** on the scratch database:
   `python -m grocery_butler data delete --confirm "delete all household data"`,
   then export and confirm every table is empty. Never on the live database.

Steps 4 to 6 are the evidence for the gate "backup restore and evidence
deletion have been tested".

## Every week

### 1. One guided kitchen scan

Walk the pantry and fridge with OpenClaw. OpenClaw reads what it sees and
submits it as an observation batch; nothing is recorded until a member
confirms, corrects or rejects each proposal.

- Start a timer when the review starts and stop it when the batch is
  resolved. Log the seconds and the number of proposals.
- Log how many proposals were confirmed, corrected and rejected. The
  `capture.batch_resolved` audit event has the same three numbers.
- Log anything on the shelf that was not proposed at all.

Guided capture sessions (#167) and the phone review screen (#166) are not
built yet. Until they are, the scan is a conversation with OpenClaw and the
review happens through it. Say so in the log, because review time through a
conversation is not the review time the gate is about.

### 2. Ad hoc updates, as they happen

When something runs low or out, tell OpenClaw. Don't save them up. At the
end of the week, log how many updates were made and how many OpenClaw got
wrong (wrong item, wrong state).

### 3. One shopping plan and one handoff

1. Ask for the plan. Review it: approve, defer or dismiss each need.
2. Log every line that should not have been there (a false suggestion) and
   everything you had to add (a missing item).
3. Hand it off. Until the Instacart adapter has a production key
   ([ADR 0014](../adr/0014-instacart-developer-platform-as-first-handoff-adapter.md)),
   the handoff is the list export. Log which path was used.
4. Shop and pay yourself, in the store or the retailer's own checkout.

### 4. Reconcile after the shop

Tell Grocery Butler what actually came home. Until receipt reconciliation
(#150) exists this is manual: go down the plan with OpenClaw and say what
was bought, substituted or skipped. Log how long it took, and whether
anything was marked received that you can't back with a receipt or your own
eyes. That number must be zero.

### 5. Read the numbers

Once a week, read `GET /api/v2/operations/metrics` and copy the five
numbers into the log. Then answer the weekly checks in the template.

### 6. Back up

Take the weekly backup. In week 4, repeat the restore drill with that
week's backup.

## How each pilot gate maps to the log

| Pilot gate | Log field | How to get it |
|------------|-----------|---------------|
| No unsupported retailer API remains reachable | `unsupported_retailer_api_check` | Pass when `main`'s CI gate is green (it runs the import-boundary and network-guard tests) and no retailer credential or host is in the deployment's variables. |
| 90% of state changes have provenance and actor attribution | `changes_total`, `changes_with_actor_and_source` | From an export: count `inventory_observations` rows, and those whose `actor` and `source` are set and whose `actor` is not `system`. |
| A 20-item photo batch has median review time under 60 seconds | `scan_proposals`, `scan_review_seconds`, `scan_review_surface` | Timed by hand during the scan. Only batches reviewed on the phone screen (#166) count toward the gate. |
| No known path can purchase or claim purchase without explicit confirmation or evidence | `received_without_evidence` | Counted during reconciliation. Must be 0 every week. |
| Backup restore and evidence deletion have been tested | `backup_taken`, `restore_drill_done`, `deletion_test_done` | The runbook steps above. Restore drill in weeks 1 and 4; deletion test once. |

Fields that feed the later product decisions rather than a gate:

| What P1.17 asks to log | Log field |
|------------------------|-----------|
| Corrections | `scan_corrected`, `adhoc_wrong`, `correction_rate` |
| Missing items | `scan_missed`, `plan_missing_items` |
| False suggestions | `plan_false_suggestions` |
| Time spent | `scan_review_seconds`, `plan_review_minutes`, `reconcile_minutes`, `support_minutes` |
| Avoided mental work | `avoided_mental_work` (a sentence or two, in the household's words) |

## After week 4

Write `docs/operations/dogfood/summary.md`:

1. Each pilot gate: met, not met, or not measurable yet, with the weeks'
   numbers.
2. Each "pause or narrow" signal from the backlog: seen or not.
3. What the person who carries the grocery load says, in their words.
4. The decision: invite design partners, run four more weeks, or narrow.

A gate that could not be measured because its feature isn't built (the
60-second photo review, for one) is "not measurable yet". It is not "met".

## What this protocol does not cover

The design-partner pilot (P2.7), pricing (P2.8), and anything that needs
more than one household.
