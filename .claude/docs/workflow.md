# Development Workflow

**Navigation**: [Back to CLAUDE.md](../../CLAUDE.md) | [Quality Standards](quality-standards.md) | [Testing](testing.md)

---

## 1. The Standard

Work is done when every gate in [`CLAUDE.md`](../../CLAUDE.md#critical-principles) passes: tests, lint, types, security, complexity, coverage, docstrings, the build, pre-commit, CI and the review verdict. If anything is red, the work is not done. Treat a lint error as a bug, not a suggestion.

## 2. Before You Start

1. Find the issue, ADR or backlog item the work serves. Read its acceptance criteria.
2. Check it against ADR 0001 and the backlog's "Safety and product principles". If the work needs a decision nobody has made, stop and raise it (the `decide` skill lays out the options).
3. Branch from an up-to-date `main`:

   ```bash
   git checkout main && git pull origin main
   git checkout -b feature/<issue>-<slug>    # or bugfix/<issue>-<slug>
   ```

## 3. Build with TDD

1. **Red:** write a test for the behavior and watch it fail.
2. **Green:** write the least code that makes it pass.
3. **Refactor:** clean up with the test still green.

Iterate on one test file directly for speed (see [Tools](tools.md#3-when-direct-invocation-is-fine)), then run the scripts. A bug fix starts with a test that reproduces the bug (`bug-squashing-methodology`).

## 4. Check Locally

```bash
./scripts/fix-all.sh            # Ruff fixes and formatting
./scripts/check-all.sh          # every check; must exit 0
./scripts/precommit-changed.sh  # hooks on the changed files, as CI runs them
```

Set `TEST_DATABASE_URL` to a throwaway Postgres database when the change touches `grocery_butler/db/`, so the Postgres-only tests run instead of skipping.

## 5. Commit

Conventional Commits, enforced by the `commit-msg` hook:

```bash
git commit -m "feat(api): add observation endpoint (#123)"
git commit -m "fix(consolidator): keep restock items when a staple is out (#130)"
git commit -m "docs(adr): record the list-export adapter decision"
```

Keep each commit one logical change. Never use `--no-verify`. The `no-commit-to-branch` hook blocks commits to `main`.

## 6. Open the PR and Pass CI

```bash
git push -u origin feature/<issue>-<slug>
gh pr create --fill
gh pr checks --watch
```

CI's required check is **CI gate**. It needs the Python gate (`./scripts/check-all.sh` on 3.11, 3.12 and 3.13, with a Postgres service and `TEST_DATABASE_URL` set) and pre-commit on the PR's changed files. When it fails, fix the cause locally, re-run section 4, and push once (`ci-debugging`).

`main` runs the full CI nightly and on manual dispatch, not on each merge. If a nightly run on `main` goes red, fixing it comes before new work.

## 7. Review

`code-review.yml` posts a verdict on the PR. What each verdict means is in [`CLAUDE.md`](../../CLAUDE.md#review-verdicts); the procedure is in `address-feedback`, and `await-claude-review` covers waiting for it. Self-review first with `review-diff`.

## 8. Before a Release

Run `./scripts/mutation.sh` (or dispatch CI by hand, which runs the mutation job) and aim for ≥80%. It is slow, and it is not a merge gate.

## 9. Branches

- `main`: always deployable. Railway deploys each push to it from the `Dockerfile`, and the entrypoint migrates the database before gunicorn starts.
- `feature/*`, `bugfix/*`: branched from `main`, merged by PR.

---

**Navigation**: [Back to CLAUDE.md](../../CLAUDE.md) | [Quality Standards](quality-standards.md) | [Testing](testing.md)
