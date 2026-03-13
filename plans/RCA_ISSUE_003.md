# RCA: PYSEC-2022-42969 — py 1.11.0 Vulnerability Suppression

## Problem Statement

`scripts/security.sh` contains an `--ignore-vuln PYSEC-2022-42969` flag that
suppresses pip-audit warnings for CVE-2022-42969 (ReDoS in the deprecated `py`
package). The `py` package is no longer installed — pytest 9.x dropped it as a
dependency — but the suppression remains.

## Root Cause

- **Location**: `scripts/security.sh:79-81`
- **What**: `PIP_AUDIT_ARGS=("--ignore-vuln" "PYSEC-2022-42969")` suppresses
  the vulnerability check for `py 1.11.0`.
- **Why it was added**: `py` was a transitive dependency of the pytest ecosystem
  and had no upstream fix at the time. Suppressing was the only option.
- **Why it's now wrong**: pytest >=8.x removed the `py` dependency entirely.
  Our environment runs pytest 9.0.2, which does not pull in `py`.

## Impact

- **Current severity**: Low — the vulnerable package is absent.
- **Risk**: The stale `--ignore-vuln` could silently mask a re-introduction of
  the vulnerable `py` package if a future dependency brings it back.
- **Scope**: Security scanning pipeline only.

## Contributing Factors

- No automated check to verify that suppression targets still exist in the
  dependency tree.
- Issue #3 was created to track removal but was never actioned.

## Fix Strategy

1. Remove the `--ignore-vuln PYSEC-2022-42969` argument and associated comments
   from `scripts/security.sh`.
2. Verify `./scripts/security.sh` still passes cleanly.
3. Close issue #3.

## Prevention

- Periodically review `--ignore-vuln` entries to check if the suppressed
  package is still present.
- Consider adding a CI step that fails if `--ignore-vuln` targets packages not
  in the current dependency tree.
