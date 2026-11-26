#!/usr/bin/env bash
# scripts/graph/build.sh: full, code-only knowledge-graph extraction.
#
# Installs the pinned graphify toolchain into the active environment (only when
# the CLI is absent), runs a deterministic tree-sitter AST extract over the
# repo, and prints node and edge counts. Local only: no LLM calls, no API keys.
# .graphifyignore keeps the venv, build output and env files out.
#
# Usage: ./scripts/graph/build.sh [extra graphify args...]
# Env:   GRAPHIFY_FORCE=1  bypass graphify's shrink guard after intentional
#                          file deletions (see scripts/graph/README.md).

set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  sed -n '2,11p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
OUT_DIR="$REPO_ROOT/graphify-out"

ensure_graphify() {
  if command -v graphify >/dev/null 2>&1; then
    return
  fi
  echo "graphify CLI not found; installing the pinned toolchain into the active environment..."
  python3 -m pip install -r "$SCRIPT_DIR/requirements.txt"
}

print_counts() {
  local graph_json="$OUT_DIR/graph.json"
  if [[ ! -f "$graph_json" ]]; then
    echo "warning: $graph_json not found; cannot report counts" >&2
    return
  fi
  python3 - "$graph_json" <<'PY'
import json
import sys

with open(sys.argv[1], encoding="utf-8") as handle:
    graph = json.load(handle)
node_count = len(graph.get("nodes", []))
# graphify emits node-link JSON: edges live under "links".
edge_count = len(graph.get("links") or graph.get("edges") or [])
print(f"{node_count:,} nodes / {edge_count:,} edges")
PY
}

ensure_graphify
echo "Building code graph for $REPO_ROOT ..."
graphify extract "$REPO_ROOT" --code-only --no-cluster --out "$REPO_ROOT" "$@"

# Reporting the counts is cosmetic; never let it override the extract exit code.
print_counts || true
