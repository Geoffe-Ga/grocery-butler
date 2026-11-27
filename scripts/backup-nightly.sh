#!/usr/bin/env bash
# scripts/backup-nightly.sh - Take tonight's encrypted backup and keep the
# newest fourteen.
#
# Runs ./scripts/backup.sh into BACKUP_DIR as YYYY-MM-DD.json.gpg (UTC), then
# removes the oldest dated backups beyond KEEP. It prunes only after tonight's
# backup succeeded, so a failed night can never cost an old backup, and it
# only ever removes files it named itself.
#
# These backups live on the server. They protect against a mistake or a bad
# migration, not against losing the server: copy one off it every week
# (docs/operations/vps-deployment.md).
#
# Usage: BACKUP_DIR=... BACKUP_PASSPHRASE=... ./scripts/backup-nightly.sh
#
# The database comes from DATABASE_URL, then DATABASE_PATH, as for backup.sh.
# deploy/vps/systemd/ holds the timer that runs this.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
# shellcheck source=scripts/activate-venv.sh
source "$SCRIPT_DIR/activate-venv.sh"

# How many dated backups to keep. Two weeks of nights.
KEEP=14

if [[ -z "${BACKUP_DIR:-}" ]]; then
    echo "✗ BACKUP_DIR is not set. Set it to the directory that holds the backups." >&2
    exit 2
fi

mkdir -p "$BACKUP_DIR"
chmod 700 "$BACKUP_DIR"

TODAY="$(date -u +%Y-%m-%d)"
TARGET="$BACKUP_DIR/$TODAY.json.gpg"

"$SCRIPT_DIR/backup.sh" "$TARGET"

# Dated names sort by age, so the newest KEEP are the last KEEP.
mapfile -t DATED < <(
    find "$BACKUP_DIR" -maxdepth 1 -type f \
        -name '[0-9][0-9][0-9][0-9]-[0-9][0-9]-[0-9][0-9].json.gpg' | sort
)
EXTRA=$(( ${#DATED[@]} - KEEP ))
if (( EXTRA > 0 )); then
    for old in "${DATED[@]:0:EXTRA}"; do
        rm -f -- "$old"
    done
fi

echo "✓ Backed up to $TODAY.json.gpg; keeping the newest $KEEP in $BACKUP_DIR"
