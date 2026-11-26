#!/usr/bin/env bash
# scripts/backup.sh - Write an encrypted backup of all household data.
#
# Exports every household table (`grocery-butler data export`) and encrypts
# the result with AES-256 under a passphrase, so the file is safe to keep off
# the machine. Restore it with ./scripts/restore.sh. Runbook:
# docs/operations/data-lifecycle.md.
#
# Usage: BACKUP_PASSPHRASE=... ./scripts/backup.sh <output-file>
#
# The database comes from DATABASE_URL, then DATABASE_PATH, as for the app.
# The passphrase is read from the environment only; it is never an argument,
# so it can't show up in a process list or shell history.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/backup-common.sh
source "$SCRIPT_DIR/backup-common.sh"

OUTPUT="${1:-}"
if [[ -z "$OUTPUT" || "$OUTPUT" == -* ]]; then
    echo "Usage: BACKUP_PASSPHRASE=... $0 <output-file>" >&2
    exit 2
fi
require_passphrase
require_gpg

WORK="$(mktemp -d)"
trap 'cleanup "$WORK"' EXIT

# Export and encrypt into the work directory first: a failed export must not
# leave a half-written file where a good backup is expected.
python3 -m grocery_butler data export --output "$WORK/export.json" >/dev/null \
    || { echo "✗ Export failed; no backup was written." >&2; exit 1; }
run_gpg "$WORK" --symmetric --cipher-algo AES256 \
    --output "$WORK/backup.gpg" "$WORK/export.json" \
    || { echo "✗ Encryption failed; no backup was written." >&2; exit 1; }

mv "$WORK/backup.gpg" "$OUTPUT"
chmod 600 "$OUTPUT"
echo "✓ Encrypted backup written to $OUTPUT"
