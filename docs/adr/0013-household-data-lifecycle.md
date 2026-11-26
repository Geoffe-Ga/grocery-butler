# ADR 0013: One JSON export is the household's portable copy and its backup

- **Status:** Accepted
- **Date:** 2026-11-26
- **Decider:** Geoff (design delegated in #171; review this before building on it)
- **Source:** [Backlog](../OPENCLAW_ERP_BACKLOG.md) P1.15; issue #171; builds on ADR 0001 and ADR 0005

## Context

The household must be able to take its data away, remove it, and get it back
after a mistake. The pilot gate asks that a backup restore and a deletion
have actually been tested, not just documented. The database is SQLite in
development and Postgres on Railway, and its schema changes with every
numbered migration.

## Decision

1. **One document.** `HouseholdData.export()` reads every household table
   into one JSON document: a format number, the dialect, the schema version,
   when it was taken, and every row. The export a person downloads and the
   backup an operator keeps are the same document.
2. **Tables are discovered, not listed.** The export reads the database's
   own catalogue. A table a new migration adds is covered the day it lands.
   Only `schema_migrations` is left out: it describes the schema, not the
   household. The archived Safeway tables are included.
3. **Restore replays history.** A restore drops every table, migrates to the
   schema version the backup was taken at, loads the rows, then applies the
   migrations written since. Old backups stay restorable, and they pass
   through the same data migrations the live database did.
4. **A committed backup is the restore drill.** `tests/fixtures/` holds one
   backup per dialect taken at schema version 17. The test suite restores
   them on every run, so a migration that can't carry old data forward fails
   CI. Add a new fixture when the format number changes; never rewrite an
   old one.
5. **Delete is total and confirmed.** `HouseholdData.delete()` removes every
   household row in one transaction and keeps the empty tables so the
   application still runs. Delete and restore each need their own exact
   phrase. They are operator commands (`grocery-butler data ...`); the API
   offers only the read-only export.
6. **Backups are encrypted outside the application.** `scripts/backup.sh`
   pipes the export through GnuPG symmetric encryption (AES-256) under a
   passphrase from the environment. `scripts/restore.sh` reverses it. The
   application never holds the passphrase.
7. **Same dialect only.** A backup restores into the dialect it came from.
   The two dialects store booleans and timestamps differently, and guessing
   a conversion is how data gets quietly changed.
8. **Dynamic names are checked, not trusted.** Table and column names can't
   be bound as SQL parameters. Every name passes `quote_identifier`, which
   accepts only plain lowercase identifiers. That holds for names read from
   a backup file as much as for names read from the catalogue.

## Consequences

**Easier:**

- Leaving is one command or one API call, in a format any tool can read.
- A restore is proven on every CI run, on both dialects.
- New tables need no lifecycle work.

**Harder:**

- A restore is several steps, not one transaction: drop, migrate, load,
  migrate. A failure in the middle leaves an empty or partly migrated
  database. The runbook restores into a fresh database for that reason, and
  the backup is checked in full before anything is dropped.
- The whole export is built in memory. That is fine for one household and
  would not be for thousands.
- Every future migration must keep old rows valid, or the fixture restore
  fails. That is the point, and it is also a constraint.

**Not covered yet:**

- Photo evidence has no storage today, only an `evidence_ref` on a batch or
  an observation. When #162 adds storage, delete must purge it and the
  export must list it. This ADR's delete is complete for what exists now.
- Railway's own volume snapshots are separate and remain worth enabling.

## Alternatives considered

| Option | Why not |
|--------|---------|
| `pg_dump` and SQLite file copies as the backup | Two formats and two restore paths, and neither is a portable export. A dump taken at an old schema also restores the old schema, with no path forward except by hand. |
| A hand-written list of tables to export | The list goes stale the first time someone forgets it, and the failure is silent: a table that isn't exported. |
| Require the backup's schema version to equal the current one | Simple, but a backup is most needed after a bad deploy, which is exactly when the versions differ. |
| Encrypt inside the application with a Python library | A new dependency and a key the application must hold. A passphrase the operator supplies to a standard tool keeps the key out of the service. |
| A delete endpoint | Destructive actions go through draft, confirm, execute. A total delete is rare enough that an operator command with a typed phrase is safer than any HTTP flow. |
