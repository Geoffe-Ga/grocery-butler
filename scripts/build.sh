#!/usr/bin/env bash
# scripts/build.sh - Build the Python package and check its metadata.
#
# CI's build job and ./scripts/check-all.sh both run this, so a packaging
# problem (for example setuptools picking up a stray directory as a package) fails
# locally first.
#
# Usage: ./scripts/build.sh [--verbose]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
cd "$(dirname "$SCRIPT_DIR")"

[[ "${1:-}" == "--verbose" ]] && set -x

echo "=== Build (sdist + wheel) ==="
OUT="$(mktemp -d)"
trap 'rm -rf "$OUT"' EXIT

python3 -m build --outdir "$OUT" >/dev/null || { echo "✗ Build failed" >&2; exit 1; }
python3 -m twine check --strict "$OUT"/* || { echo "✗ twine check failed" >&2; exit 1; }
echo "✓ Build passed"
