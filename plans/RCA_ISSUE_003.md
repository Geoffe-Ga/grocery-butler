# RCA: PYSEC-2022-42969 — py 1.11.0 Vulnerability Suppression

## Problem Statement

`scripts/security.sh` contained an `--ignore-vuln PYSEC-2022-42969` flag that
suppressed pip-audit warnings for CVE-2022-42969 (ReDoS in the deprecated `py`
package). Locally, `py` is absent (pytest 9.x dropped it), but removing the
suppression caused CI to fail because `py 1.11.0` was still present in the
GitHub Actions runner system Python.

## Root Cause

Two interacting issues:

1. **Stale suppression in `scripts/security.sh`**: The `--ignore-vuln
   PYSEC-2022-42969` flag was added when `py` was a transitive dependency of
   pytest. pytest >=8.x dropped it, making the suppression unnecessary locally.

2. **CI installs into system Python**: The CI workflow (`ci.yml`) installed all
   dependencies directly into the GitHub Actions runner's system Python — no
   virtual environment. The runner images ship with `py 1.11.0` pre-installed,
   so `pip-audit` flagged it even though it is not one of our dependencies.
   Meanwhile, `scripts/security.sh` detected no virtualenv (`Warning: No
   virtualenv found`) and fell through to a bare `pip-audit` call that scanned
   the entire system environment.

## Impact

- **CI severity**: High — all 3 Python matrix jobs (3.11, 3.12, 3.13) failed
  the security check after the suppression was removed.
- **Local severity**: None — local venv does not contain `py`.
- **Risk**: The stale `--ignore-vuln` could silently mask a re-introduction of
  the vulnerable `py` package if a future dependency brings it back.

## Contributing Factors

- CI did not use a virtual environment, mixing project deps with runner system
  packages.
- The initial RCA (PR #38) only verified locally and missed the CI divergence.
- No automated check to verify that suppression targets still exist in the
  project's actual dependency tree.

## Fix Strategy

1. **Add venv to CI** (`ci.yml`): Create `.venv`, export `VIRTUAL_ENV` and
   prepend `.venv/bin` to `$GITHUB_PATH` so all subsequent steps (including
   `security.sh`) run inside the venv. This isolates pip-audit from runner
   system packages.
2. **Remove the suppression** (`scripts/security.sh`): Already done — the
   `--ignore-vuln PYSEC-2022-42969` argument and associated comments were
   removed.
3. **Close issue #3** after CI goes green.

## Prevention

- CI now uses a virtual environment, preventing false positives from runner
  system packages.
- Periodically review any future `--ignore-vuln` entries to check if the
  suppressed package is still present in the project's dependency tree.
