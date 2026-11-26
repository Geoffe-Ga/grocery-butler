#!/usr/bin/env bash
# scripts/activate-venv.sh - Auto-detect and activate the shared virtualenv.
#
# Source this from other scripts AFTER setting PROJECT_ROOT:
#   source "$SCRIPT_DIR/activate-venv.sh"
#
# Search order:
#   1. $VIRTUAL_ENV already set (no-op)
#   2. $PROJECT_ROOT/.venv/
#   3. Shared venv at ../../grocery-butler/.venv/ (worktree layout)

if [ -z "${VIRTUAL_ENV:-}" ]; then
    _VENV_CANDIDATES=(
        "$PROJECT_ROOT/.venv"
        "$PROJECT_ROOT/../../grocery-butler/.venv"
    )
    for _venv in "${_VENV_CANDIDATES[@]}"; do
        if [ -f "$_venv/bin/activate" ]; then
            export VIRTUAL_ENV="$_venv"
            export PATH="$_venv/bin:$PATH"
            break
        fi
    done
    if [ -z "${VIRTUAL_ENV:-}" ]; then
        echo "Warning: No virtualenv found. Tools may not be available." >&2
    fi
fi
