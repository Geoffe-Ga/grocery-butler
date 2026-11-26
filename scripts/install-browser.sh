#!/usr/bin/env bash
# scripts/install-browser.sh - Install the Chromium the browser tests drive.
#
# tests/browser runs the dashboard in a real headless Chromium (ADR 0022).
# Playwright, pinned in requirements-dev.txt, downloads the exact build it
# was released with. Run this once after installing the dev requirements,
# and again whenever the playwright pin changes.
#
# Usage: ./scripts/install-browser.sh [--with-deps]
#
#   --with-deps   Also install the system libraries Chromium needs, with the
#                 system package manager. CI passes this; on your own machine
#                 leave it off unless the browser fails to start.
#
# A machine that can't reach Playwright's download host can set
# BROWSER_TESTS_CHROMIUM to an existing Chromium executable instead.

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
# shellcheck source=scripts/activate-venv.sh
source "$SCRIPT_DIR/activate-venv.sh"

ARGS=(install)
case "${1:-}" in
    --with-deps) ARGS+=(--with-deps) ;;
    "") ;;
    *)
        echo "Usage: $0 [--with-deps]" >&2
        exit 2
        ;;
esac

if ! python3 -c "import playwright" 2>/dev/null; then
    echo "✗ playwright is not installed. Run: pip install -r requirements-dev.txt" >&2
    exit 2
fi

echo "=== Installing Chromium for the browser tests ==="
python3 -m playwright "${ARGS[@]}" chromium \
    || { echo "✗ Chromium did not install. See above." >&2; exit 1; }
echo "✓ Chromium is installed"
