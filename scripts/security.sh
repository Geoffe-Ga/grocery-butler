#!/usr/bin/env bash
# scripts/security.sh - Run security checks with Bandit and pip-audit
# Usage: ./scripts/security.sh [--full] [--verbose] [--help]

set -euo pipefail

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
PROJECT_ROOT="$(dirname "$SCRIPT_DIR")"
source "$SCRIPT_DIR/activate-venv.sh"

FULL=false
VERBOSE=false

# Parse command line arguments
while [[ $# -gt 0 ]]; do
    case $1 in
        --full)
            FULL=true
            shift
            ;;
        --verbose)
            VERBOSE=true
            shift
            ;;
        --help)
            cat << EOF
Usage: $(basename "$0") [OPTIONS]

Run security checks using Bandit and pip-audit.

OPTIONS:
    --full      Run comprehensive security scan
    --verbose   Show detailed output
    --help      Display this help message

EXIT CODES:
    0           No security issues found
    1           Security issues found
    2           Error running checks

EXAMPLES:
    $(basename "$0")             # Run basic security checks
    $(basename "$0") --full      # Run comprehensive scan
    $(basename "$0") --verbose   # Show detailed output
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

echo "=== Security Checks (Bandit) ==="

# Run Bandit
if $VERBOSE; then
    echo "Running Bandit security scanner..."
fi
bandit -r grocery_butler/ || { echo "✗ Bandit found issues" >&2; exit 1; }

echo "=== Dependency Audit (pip-audit) ==="

# Run pip-audit for dependency vulnerability checking
# Point pip-audit at the venv Python so it audits the right environment
if $VERBOSE; then
    echo "Running pip-audit dependency checker..."
fi

# Known vulnerability ignores (deps with no fix available):
#   PYSEC-2022-42969: py 1.11.0 - deprecated package, transitive dep from pytest tooling
#   Issue #3: Remove once pytest ecosystem drops the py transitive dependency
PIP_AUDIT_ARGS=("--ignore-vuln" "PYSEC-2022-42969")

VENV_PYTHON="${VIRTUAL_ENV:-$PROJECT_ROOT/.venv}/bin/python"
if [ -x "$VENV_PYTHON" ]; then
    PIPAPI_PYTHON_LOCATION="$VENV_PYTHON" pip-audit "${PIP_AUDIT_ARGS[@]}" || { echo "✗ pip-audit found issues" >&2; exit 1; }
else
    pip-audit "${PIP_AUDIT_ARGS[@]}" || { echo "✗ pip-audit found issues" >&2; exit 1; }
fi

if $FULL; then
    echo "=== Comprehensive Security Scan ==="

    # Check for hardcoded secrets
    if command -v detect-secrets &> /dev/null; then
        if $VERBOSE; then
            echo "Running detect-secrets scan..."
        fi
        detect-secrets scan . || true
    fi
fi

echo "✓ Security checks passed"
exit 0
