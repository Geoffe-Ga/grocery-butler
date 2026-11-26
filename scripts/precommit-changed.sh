#!/usr/bin/env bash
# scripts/precommit-changed.sh - Run pre-commit on the files this change touches.
#
# The one way to run pre-commit on changed files, locally and in CI,
# so both always check the same file set.
#
# Base: $BASE_REF if set (CI passes a PR's base; its other runs set FULL=true),
# else origin/main after a quiet fetch. Files are diffed from the merge-base.
# Falls back to every file when no usable base exists, when $FULL=true, or
# when .pre-commit-config.yaml itself changed.
#
# Usage: ./scripts/precommit-changed.sh [extra pre-commit args]

set -euo pipefail

if [[ "${1:-}" == "--help" ]]; then
    sed -n '2,13p' "$0" | sed 's/^# \{0,1\}//'
    exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

base_ref="${BASE_REF:-}"
if [[ -z "$base_ref" ]]; then
    # A stale origin/main would diff a different file set than CI does.
    git fetch --quiet origin main 2>/dev/null || true
    base_ref="origin/main"
fi

full="${FULL:-false}"
base=""
if [[ "$full" != "true" ]] && git rev-parse --verify --quiet "${base_ref}^{commit}" >/dev/null; then
    base="$(git merge-base "$base_ref" HEAD 2>/dev/null || true)"
fi

if [[ -z "$base" ]]; then
    full=true
elif ! git diff --quiet "$base" HEAD -- .pre-commit-config.yaml; then
    echo "Hook config changed: checking every file."
    full=true
fi

if [[ "$full" == "true" ]]; then
    exec pre-commit run --all-files "$@"
fi
exec pre-commit run --from-ref "$base" --to-ref HEAD "$@"
