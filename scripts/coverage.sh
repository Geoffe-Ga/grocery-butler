#!/usr/bin/env bash
# scripts/coverage.sh - Run tests with coverage report
# Usage: ./scripts/coverage.sh [--html] [--xml] [--verbose] [--help]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
# shellcheck source=scripts/activate-venv.sh
source "$SCRIPT_DIR/activate-venv.sh"

HTML_REPORT=false
VERBOSE=false

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --html)
            HTML_REPORT=true
            shift
            ;;
        --xml)
            # coverage.xml is always written; the flag is accepted for older callers.
            shift
            ;;
        --verbose)
            VERBOSE=true
            shift
            ;;
        --help)
            cat << EOF
Usage: $(basename "$0") [OPTIONS]

Run tests with coverage report.

OPTIONS:
    --html      Generate HTML coverage report
    --xml       Accepted for older callers; coverage.xml is always written
    --verbose   Show detailed output
    --help      Display this help message

EXIT CODES:
    0           Coverage threshold met
    1           Coverage below threshold
    2           Error running coverage

EXAMPLES:
    $(basename "$0")          # Run coverage with terminal report
    $(basename "$0") --html   # Generate HTML report
    $(basename "$0") --xml    # Generate XML report for CI
EOF
            exit 0
            ;;
        *)
            echo "Error: Unknown option: $1" >&2
            exit 2
            ;;
    esac
done

cd "$PROJECT_ROOT"

# Set verbosity
if $VERBOSE; then
    set -x
fi

echo "=== Coverage Report ==="

# Build pytest arguments
PYTEST_ARGS=(
    -v
    --cov=grocery_butler
    --cov-branch
    --cov-report=term-missing
    --cov-fail-under=90
    --cov-report=xml
)

# Add HTML report if requested
if $HTML_REPORT; then
    PYTEST_ARGS+=(--cov-report=html)
    echo "HTML report will be generated in htmlcov/"
fi

# coverage.xml is always written: the branch-coverage check below reads it.
# --xml is kept so older callers still work.

# Run tests with coverage
pytest "${PYTEST_ARGS[@]}" tests/ || {
    echo "✗ Coverage below threshold" >&2
    exit 1
}

# Branch coverage has its own floor (CLAUDE.md: >= 85%). --cov-fail-under above
# checks the combined number, so read the branch rate on its own.
python3 - <<'PY' || exit 1
import sys
import xml.etree.ElementTree as ET

rate = float(ET.parse("coverage.xml").getroot().get("branch-rate", "0")) * 100
print(f"Branch coverage: {rate:.1f}% (minimum 85%)")
if rate < 85:
    print("✗ Branch coverage below 85%", file=sys.stderr)
    sys.exit(1)
PY

echo "✓ Coverage threshold met"

exit 0
