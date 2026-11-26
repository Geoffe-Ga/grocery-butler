"""Fail when branch coverage is below the floor.

``--cov-fail-under`` (pyproject.toml) checks line and branch coverage combined,
so ./scripts/coverage.sh runs this afterwards on pytest-cov's JSON report to
hold branch coverage on its own to CLAUDE.md's standard.

Usage: python3 scripts/check_branch_coverage.py [coverage.json]
"""

from __future__ import annotations

import json
import sys
from pathlib import Path

MIN_BRANCH_COVERAGE = 90.0


def branch_coverage(report: Path) -> float:
    """Return the report's branch coverage as a percentage (100 with no branches)."""
    totals = json.loads(report.read_text(encoding="utf-8"))["totals"]
    branches = int(totals["num_branches"])
    if branches == 0:
        return 100.0
    return int(totals["covered_branches"]) * 100 / branches


def main(argv: list[str]) -> int:
    """Print the branch coverage and return 1 when it's below the floor."""
    report = Path(argv[1] if len(argv) > 1 else "coverage.json")
    rate = branch_coverage(report)
    print(f"Branch coverage: {rate:.2f}% (minimum {MIN_BRANCH_COVERAGE:.0f}%)")
    if rate < MIN_BRANCH_COVERAGE:
        print(f"✗ Branch coverage below {MIN_BRANCH_COVERAGE:.0f}%", file=sys.stderr)
        return 1
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv))
