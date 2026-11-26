#!/usr/bin/env bash
# scripts/backup-common.sh - Shared by backup.sh and restore.sh. Source it.

# Fail unless the passphrase is in the environment.
require_passphrase() {
    if [[ -z "${BACKUP_PASSPHRASE:-}" ]]; then
        echo "✗ BACKUP_PASSPHRASE is not set. Set it in the environment (never on the command line)." >&2
        exit 2
    fi
}

# Fail unless gpg is installed.
require_gpg() {
    if ! command -v gpg >/dev/null 2>&1; then
        echo "✗ gpg is not installed. Install GnuPG (apt-get install gnupg, brew install gnupg)." >&2
        exit 2
    fi
}

# Run gpg with the passphrase on a private file descriptor and a throwaway
# home, so no keyring, agent cache or config on this machine is involved.
# Usage: run_gpg <work-dir> <gpg arguments...>
run_gpg() {
    local work="$1"
    shift
    mkdir -p "$work/gnupg"
    chmod 700 "$work/gnupg"
    gpg --homedir "$work/gnupg" --batch --yes --quiet --no-symkey-cache \
        --pinentry-mode loopback --passphrase-fd 3 "$@" 3<<<"$BACKUP_PASSPHRASE"
}

# Stop the throwaway agent and remove the work directory with its plaintext.
# Usage: cleanup <work-dir>
cleanup() {
    local work="$1"
    if [[ -d "$work/gnupg" ]]; then
        gpgconf --homedir "$work/gnupg" --kill gpg-agent >/dev/null 2>&1 || true
    fi
    rm -rf "$work"
}
