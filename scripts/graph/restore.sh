#!/usr/bin/env bash
# scripts/graph/restore.sh: fetch the published code graph instead of building it.
#
# Downloads graph.json and graph-meta.json from the rolling `knowledge-graph`
# release into graphify-out/, using the GitHub CLI (`gh`). Fail-soft: when gh is
# missing or the graph isn't published yet, it says so and exits 0, and you can
# run ./scripts/graph/build.sh instead.
#
# Usage: ./scripts/graph/restore.sh

set -euo pipefail

if [[ "${1:-}" == "--help" || "${1:-}" == "-h" ]]; then
  sed -n '2,9p' "$0" | sed 's/^# \{0,1\}//'
  exit 0
fi

SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
REPO_ROOT="$(git -C "$SCRIPT_DIR" rev-parse --show-toplevel)"
OUT_DIR="$REPO_ROOT/graphify-out"
GRAPH_TAG="knowledge-graph"

if ! command -v gh >/dev/null 2>&1; then
  echo "gh not found; build locally with ./scripts/graph/build.sh"
  exit 0
fi

mkdir -p "$OUT_DIR"
if (cd "$REPO_ROOT" && gh release download "$GRAPH_TAG" \
  --pattern graph.json --pattern graph-meta.json --dir "$OUT_DIR" --clobber); then
  echo "restored the published graph into $OUT_DIR"
  if [[ -f "$OUT_DIR/graph-meta.json" ]]; then
    cat "$OUT_DIR/graph-meta.json"
  fi
else
  echo "no published graph yet; build locally with ./scripts/graph/build.sh"
fi
