# Knowledge Graph (graphify)

A queryable code graph of this repo, built by [graphify](https://pypi.org/project/graphifyy/) from tree-sitter ASTs. No LLM calls, no API keys, no cost. Ported from the Reconcile repository, code graph only.

## Get the graph

| Command | What it does |
|---|---|
| `./scripts/graph/restore.sh` | Download the published graph of `main` (needs `gh`). Fastest. |
| `./scripts/graph/build.sh` | Build the graph from scratch (installs the pinned `graphifyy` if the CLI is missing). |
| `./scripts/graph/update.sh` | Re-extract only changed files after you edit code. |

Output lands in `graphify-out/` (gitignored). Set `GRAPHIFY_FORCE=1` on `build.sh` or `update.sh` after deleting files, so graphify's shrink guard accepts the smaller graph.

## Query it

```bash
graphify query "where does the shopping list get consolidated?"
graphify path "PantryManager" "create_app"
graphify explain "extract_json_text"
graphify affected "create_connection"
```

Quote each node's `source_location` when you cite the graph, and confirm the answer in that file.

## What's in it

Python and shell. Markdown isn't extracted, and neither are the SQL migrations (graphify needs an extra parser for them, which the pin leaves out). `.graphifyignore` keeps out `.venv`, `.claude`, env files, `mutants`, coverage and build output; graphify also honors `.gitignore`.

## How it's published

`.github/workflows/graph-build.yml` runs on code pushes to `main` (incremental), nightly at 04:40 UTC (full rebuild) and on manual dispatch. It uploads `graph.json` and `graph-meta.json` to a rolling `knowledge-graph` release with the built-in `GITHUB_TOKEN`. This repo is public, so the release is too; it holds nothing the code doesn't.

## Upgrading

Change the single pin in `scripts/graph/requirements.txt`; the workflow's cache key follows it. It's kept apart from `requirements-dev.txt` because the quality gates don't need it, and installing the tree-sitter toolchain in every CI job would slow them down.
