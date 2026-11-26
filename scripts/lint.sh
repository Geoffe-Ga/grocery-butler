#!/usr/bin/env bash
# scripts/lint.sh - Run linting checks with Ruff
# Usage: ./scripts/lint.sh [--fix] [--check] [--verbose] [--help]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
# shellcheck source=scripts/activate-venv.sh
source "$SCRIPT_DIR/activate-venv.sh"

FIX=false
VERBOSE=false

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --fix)
            FIX=true
            shift
            ;;
        --check)
            # Checking is the default; the flag is accepted for older callers.
            shift
            ;;
        --verbose)
            VERBOSE=true
            shift
            ;;
        --help)
            cat << EOF
Usage: $(basename "$0") [OPTIONS]

Run linting checks on the project using Ruff.

OPTIONS:
    --fix       Auto-fix linting issues where possible
    --check     Check only, fail if issues found (default mode)
    --verbose   Show detailed output
    --help      Display this help message

EXIT CODES:
    0           All checks passed
    1           Linting issues found
    2           Error running checks

EXAMPLES:
    $(basename "$0")              # Run checks in check mode
    $(basename "$0") --fix         # Auto-fix issues
    $(basename "$0") --verbose     # Show detailed output
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

echo "=== Linting (Ruff) ==="

if $FIX; then
    if $VERBOSE; then
        echo "Fixing linting issues..."
    fi
    EXIT_CODE=0
    ruff check . --fix || EXIT_CODE=$?
else
    if $VERBOSE; then
        echo "Checking for linting issues..."
    fi
    EXIT_CODE=0
    ruff check . || EXIT_CODE=$?
fi

if [ $EXIT_CODE -ne 0 ]; then
    echo "✗ Linting checks failed" >&2
    exit 1
fi

echo "=== Docstring coverage (interrogate) ==="

# Thresholds and scope live in [tool.interrogate] in pyproject.toml, so this
# script, the pre-commit hook and CI all apply the same rule.
if ! interrogate -c pyproject.toml; then
    echo "✗ Docstring coverage below threshold" >&2
    exit 1
fi

echo "✓ Linting checks passed"
exit 0
