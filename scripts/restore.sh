#!/usr/bin/env bash
# scripts/restore.sh - Rebuild the database from an encrypted backup.
#
# Decrypts a file ./scripts/backup.sh wrote and restores it
# (`grocery-butler data restore`). This REPLACES everything in the database,
# so it needs the confirmation phrase. Runbook:
# docs/operations/data-lifecycle.md.
#
# Usage: BACKUP_PASSPHRASE=... ./scripts/restore.sh <backup-file> "<phrase>"
#
# Run it without the phrase to be told what the phrase is. The database comes
# from DATABASE_URL, then DATABASE_PATH, as for the app.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/backup-common.sh
source "$SCRIPT_DIR/backup-common.sh"

BACKUP="${1:-}"
PHRASE="${2:-}"
if [[ -z "$BACKUP" || "$BACKUP" == -* ]]; then
    echo "Usage: BACKUP_PASSPHRASE=... $0 <backup-file> \"<phrase>\"" >&2
    exit 2
fi
if [[ ! -f "$BACKUP" ]]; then
    echo "✗ $BACKUP is not a file." >&2
    exit 1
fi
require_passphrase
require_gpg

WORK="$(mktemp -d)"
trap 'cleanup "$WORK"' EXIT

# Decrypt fully before touching the database: a wrong passphrase or a
# damaged file must change nothing.
run_gpg "$WORK" --decrypt --output "$WORK/export.json" "$BACKUP" \
    || { echo "✗ Could not decrypt $BACKUP. Check BACKUP_PASSPHRASE. Nothing was changed." >&2; exit 1; }

python3 -m grocery_butler data restore "$WORK/export.json" --confirm "$PHRASE" \
    || exit 1
echo "✓ Restored from $BACKUP"
