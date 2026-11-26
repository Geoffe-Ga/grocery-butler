#!/usr/bin/env bash
# scripts/complexity.sh - Code complexity analysis
# Usage: ./scripts/complexity.sh [--verbose] [--help]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
# shellcheck source=scripts/activate-venv.sh
source "$SCRIPT_DIR/activate-venv.sh"

VERBOSE=false

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --verbose)
            VERBOSE=true
            shift
            ;;
        --help)
            cat << EOF
Usage: $(basename "$0") [OPTIONS]

Analyze code complexity using Radon and Xenon.

Metrics:
  - Cyclomatic complexity (should be <= 10)
  - Maintainability index (should be >= 20)
  - Cognitive complexity

OPTIONS:
    --verbose   Show detailed output
    --help      Display this help message

EXIT CODES:
    0           Complexity acceptable
    1           Complexity exceeds thresholds
    2           Error during analysis

EXAMPLES:
    $(basename "$0")          # Analyze complexity
    $(basename "$0") --verbose # Show detailed output
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

echo "=== Code Complexity Analysis ==="

for tool in radon xenon; do
    command -v "$tool" >/dev/null || { echo "✗ $tool not installed: pip install -r requirements-dev.txt" >&2; exit 1; }
done

# One scope and one threshold set, shared with CI (which runs this script).
# CLAUDE.md: complexity <= 10 per function (xenon rank B), modules and the
# average stay at rank A.
echo ""
echo "Cyclomatic complexity (report):"
radon cc -a -nb grocery_butler/

echo ""
echo "Maintainability index (report):"
radon mi -nb grocery_butler/

xenon --max-absolute B --max-modules A --max-average A grocery_butler/ \
    || { echo "✗ Complexity exceeds thresholds" >&2; exit 1; }

echo "✓ Complexity analysis completed"
exit 0
