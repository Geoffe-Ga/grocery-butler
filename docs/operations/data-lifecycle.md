# Household data: export, delete, backup and restore

How to take the household's data out of Grocery Butler, remove it, back it
up and bring it back. The design is
[ADR 0013](../adr/0013-household-data-lifecycle.md).

## What is stored, and for how long

Everything lives in one database: SQLite in development, Railway Postgres in
production. Nothing is deleted on a timer today. This table says what each
kind of record is and when it stops mattering.

| Data | Kept | Notes |
|------|------|-------|
| Household members, items, aliases, recipes, staples, preferences, hard constraints | Until deleted | The household's standing setup. |
| Inventory observations and item, need and plan history | Until deleted | Append-only audit trail (ADR 0007). Old observations stop counting as current after the category's freshness window; they stay in the ledger. |
| Observation batches and their proposals | Until deleted | A batch nobody reviews expires after 72 hours and can no longer be applied. The record stays. |
| Approval envelopes | Until deleted | An approval expires 30 minutes after it is drafted. The record stays. |
| API idempotency records | Until deleted | A key is only honoured for 30 days. |
| Photos | Not stored | Only a reference is recorded today. Photo storage arrives with #162, kept until the batch closes (ADR 0005). |
| Archived Safeway tables | Until deleted | Left from before ADR 0001. Read by nothing. Included in export and delete. |

Secrets (the API token, the Anthropic key, the backup passphrase) are
environment variables. They are never in the database and never in an
export.

## Who else handles the data

| Service | What it sees | Why |
|---------|--------------|-----|
| Railway | The whole database, encrypted in transit | Hosts the app and Postgres. |
| Anthropic API | The text of a recipe, or of an inventory update, being parsed | Claude turns free text into structured rows. It holds no state for us. |
| Tailscale | Network metadata only; traffic is end to end encrypted | The dashboard and API are reachable only on the tailnet. |
| OpenClaw (RubotPaul) | Whatever it reads and writes through `/api/v1` and `/api/v2` | The household's own assistant. |

No retailer receives anything from Grocery Butler (ADR 0001). A shopping
list leaves only when a person exports it and takes it somewhere.

## Export

```bash
python -m grocery_butler data export --output household.json
```

or `GET /api/v2/household/export` with the API token. Both give the same
JSON document: every household table, with the schema version it was taken
at. The file is created readable only by you. It holds the whole household
in plain text, so treat it like the database itself.

The database is `DATABASE_URL`, then `DATABASE_PATH`, then `mealbot.db`;
`--database` overrides all three.

## Delete

```bash
python -m grocery_butler data delete --confirm "delete all household data"
```

This removes every household row in one transaction. It can't be undone
except from a backup. Run it without `--confirm` and it changes nothing and
tells you the phrase. The empty tables stay, so the app keeps running.

Deleting does not reach copies that already left: exports and backups you
made, and anything OpenClaw kept. Delete those where they are.

## Back up

Requirements: `gpg` (GnuPG) on the machine that runs the script, and a
passphrase kept in a password manager. Lose the passphrase and the backups
are unreadable; that is what the encryption is for.

```bash
export BACKUP_PASSPHRASE   # set its value from your password manager
DATABASE_URL="<production url>" ./scripts/backup.sh "backups/$(date -u +%Y-%m-%d).json.gpg"
```

The script exports, encrypts with AES-256, and writes the file only if both
steps worked. Nothing unencrypted is left behind. On Railway, run it from
your own machine with `railway run ./scripts/backup.sh <file>`, which
supplies `DATABASE_URL`.

**How often.** Weekly during the dogfood, and always before a deploy that
includes a migration. Keep at least the last four. Store them somewhere
other than Railway.

## Restore

A restore **replaces everything** in the target database. It rebuilds the
schema the backup was taken at, loads the rows, then applies the migrations
written since. It is several steps, not one transaction, so follow the order
below.

1. **Stop the app** so nothing writes during the restore. On Railway, scale
   the service to zero or remove the deployment.
2. **Prefer a fresh database.** Create a new, empty Postgres database and
   point `DATABASE_URL` at it. If the restore fails, the old database is
   untouched.
3. **If you must restore in place, take a backup first**, even of a
   database you believe is broken.
4. Restore:

   ```bash
   export BACKUP_PASSPHRASE   # set its value from your password manager
   DATABASE_URL="<target url>" ./scripts/restore.sh backups/2026-11-26.json.gpg "replace all household data"
   ```

   A wrong passphrase, a damaged file, or a missing phrase changes nothing.
5. **Check it.** `python -m grocery_butler data export | head` should show
   today's schema version and your tables. Open the dashboard and look at
   the inventory.
6. **Start the app.**

**If a restore fails after it started,** the target is empty or partly
migrated. Nothing is lost that wasn't already in the backup: fix the cause
and run the same restore again. It starts by dropping whatever is there.

**Limits.** A backup restores only into the dialect it came from (a
Postgres backup into Postgres). A backup from a newer release than the code
doing the restore is refused; upgrade first.

## The restore drill

CI restores a committed backup on every run: `tests/fixtures/backup_v17_*.json`
through `tests/test_data_lifecycle.py`, on SQLite and Postgres, and the
encrypted round trip through `tests/test_backup_scripts.py`. That proves the
mechanism. It does not prove *your* backup: before relying on one, restore
it into a scratch database once and look at it.
