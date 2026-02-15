# Claude Code Project Context: grocery-butler

**Table of Contents**
- [1. Critical Principles](#1-critical-principles)
- [2. Project Overview](#2-project-overview)
- [3. The Maximum Quality Engineering Mindset](#3-the-maximum-quality-engineering-mindset)
- [4. Stay Green Workflow](#4-stay-green-workflow)
- [5. Architecture](#5-architecture)
- [6. Quality Standards](#6-quality-standards)
- [7. Development Workflow](#7-development-workflow)
- [8. Testing Strategy](#8-testing-strategy)
- [9. Tool Usage & Code Standards](#9-tool-usage--code-standards)
- [10. Common Pitfalls & Troubleshooting](#10-common-pitfalls--troubleshooting)
- [Appendix A: AI Subagent Guidelines](#appendix-a-ai-subagent-guidelines)
- [Appendix B: Key Files](#appendix-b-key-files)
- [Appendix C: External References](#appendix-c-external-references)

---

## 1. Critical Principles

These principles are **non-negotiable** and must be followed without exception:

### 1.1 Use Project Scripts, Not Direct Tools

Always invoke tools through `./scripts/*` instead of directly.

**Why**: Scripts ensure consistent configuration across local development and CI.

| Task | NEVER | ALWAYS |
|------|-------|--------|
| Format code | `ruff format .` | `./scripts/format.sh` |
| Run tests | `pytest` | `./scripts/test.sh` |
| Type check | `mypy .` | `./scripts/typecheck.sh` |
| Lint code | `ruff check .` | `./scripts/lint.sh` |
| Security scan | `bandit -r src/` | `./scripts/security.sh` |
| All checks | *(run each tool)* | `./scripts/check-all.sh` |

See [9.1 Tool Invocation Patterns](#91-tool-invocation-patterns) for complete list.

---

### 1.2 DRY Principle - Single Source of Truth

Never duplicate content. Always reference the canonical source.

**Examples**:
- Workflow documentation -> single source
- Other files -> Link to canonical docs
- Never copy workflow steps into multiple files

**Why**: Duplicated docs get out of sync, causing confusion and errors.

---

### 1.3 No Shortcuts - Fix Root Causes

Never bypass quality checks or suppress errors without justification.

**Forbidden Shortcuts**:
- Commenting out failing tests
- Adding `# noqa` without issue reference
- Lowering quality thresholds to pass builds
- Using `git commit --no-verify` to skip pre-commit
- Deleting code to reduce complexity metrics

**Required Approach**:
- Fix the failing test or mark with `@pytest.mark.skip(reason="Issue #N")`
- Refactor code to pass linting (or justify with issue: `# noqa  # Issue #N: reason`)
- Write tests to reach 90% coverage
- Always run pre-commit checks
- Refactor complex functions into smaller ones

See [10.1 No Shortcuts Policy](#101-no-shortcuts-policy) for detailed examples.

---

### 1.4 Stay Green - Never Request Review with Failing Checks

Follow the 3-gate workflow rigorously.

**The Rule**:
- NEVER create PR while CI is red
- NEVER request review with failing checks
- NEVER merge without LGTM

**The Process**:
1. Gate 1: Local checks pass (`./scripts/check-all.sh` -> exit 0)
2. Gate 2: CI pipeline green (all jobs pass)
3. Gate 3: Code review LGTM

See [4. Stay Green Workflow](#4-stay-green-workflow) for complete documentation.

---

### 1.5 Quality First - Meet MAXIMUM QUALITY Standards

Quality thresholds are immutable. Meet them, don't lower them.

**Standards**:
- Test Coverage: >=90%
- Docstring Coverage: >=95%
- Cyclomatic Complexity: <=10 per function

**When code doesn't meet standards**:
- Never change `fail_under = 70` in pyproject.toml
- Write more tests, refactor code, improve quality

See [6. Quality Standards](#6-quality-standards) for enforcement mechanisms.

---

### 1.6 Operate from Project Root

Use relative paths from project root. Never `cd` into subdirectories.

**Why**: Ensures commands work in any environment (local, CI, scripts).

**Examples**:
- `./scripts/test.sh tests/test_main.py`
- Never `cd tests && pytest test_main.py`

**CI Note**: CI always runs from project root. Commands that use `cd` will break in CI.

---

### 1.7 Verify Before Commit

Run `./scripts/check-all.sh` before every commit. Only commit if exit code is 0.

**Pre-Commit Checklist**:
- [ ] `./scripts/check-all.sh` passes (exit 0)
- [ ] All new functions have tests
- [ ] Coverage >=90% maintained
- [ ] No failing tests
- [ ] Conventional commit message ready

See [10. Common Pitfalls & Troubleshooting](#10-common-pitfalls--troubleshooting) for complete list.

---

**These principles are the foundation of MAXIMUM QUALITY ENGINEERING. Follow them without exception.**

---

## 2. Project Overview

**grocery-butler** is a Python application designed with maximum quality engineering standards.

**Purpose**: Grocery Butler - a quality-controlled Python project for grocery management workflows.

**Key Features**:
- TBD - project is in early development

---

## 3. The Maximum Quality Engineering Mindset

**Core Philosophy**: It is not merely a goal but a source of profound satisfaction and professional pride to ship software that is GREEN on all checks with ZERO outstanding issues. This is not optional -- it is the foundation of our development culture.

### 3.1 The Green Check Philosophy

When all CI checks pass with zero warnings, zero errors, and maximum quality metrics:
- Tests: 100% passing
- Coverage: >=90%
- Linting: 0 errors, 0 warnings
- Type checking: 0 errors
- Security: 0 vulnerabilities
- Docstring coverage: >=95%

This represents **MAXIMUM QUALITY ENGINEERING** -- the standard to which all code must aspire.

### 3.2 Why Maximum Quality Matters

1. **Pride in Craftsmanship**: Every green check represents excellence in execution
2. **Zero Compromise**: Quality is not negotiable -- it's the baseline
3. **Compound Excellence**: Small quality wins accumulate into robust systems
4. **Trust and Reliability**: Green checks mean the code does what it claims
5. **Developer Joy**: There is genuine satisfaction in seeing all checks pass

### 3.3 The Role of Quality in Development

Quality engineering is not a checkbox -- it's a continuous commitment:

- **Before Commit**: Run `./scripts/check-all.sh` and fix every issue
- **During Review**: Address every comment, resolve every suggestion
- **After Merge**: Monitor CI, ensure all checks remain green
- **Always**: Treat linting errors as bugs, not suggestions

### 3.4 The "No Red Checks" Rule

**NEVER** merge code with:
- Failing tests
- Linting errors (even "minor" ones)
- Type checking failures
- Coverage below threshold
- Security vulnerabilities
- Unaddressed review comments

If CI shows red, the work is not done. Period.

---

## 4. Stay Green Workflow

**Policy**: Never request review with failing checks. Never merge without LGTM.

The Stay Green workflow enforces iterative quality improvement through **3 sequential gates**. Each gate must pass before proceeding to the next.

### 4.1 The Three Gates

1. **Gate 1: Local Pre-Commit** (Iterate Until Green)
   - Run `./scripts/check-all.sh`
   - Fix all formatting, linting, types, complexity, security issues
   - Fix tests and coverage (90%+ required)
   - Only push when all local checks pass (exit code 0)

2. **Gate 2: CI Pipeline** (Iterate Until Green)
   - Push to branch: `git push origin feature-branch`
   - Monitor CI: `gh pr checks --watch`
   - If CI fails: fix locally, re-run Gate 1, push again
   - Only proceed when all CI jobs show green

3. **Gate 3: Code Review** (Iterate Until LGTM)
   - Wait for code review (AI or human)
   - If feedback provided: address ALL concerns
   - Re-run Gate 1, push, wait for CI
   - Only merge when review shows LGTM with no reservations

### 4.2 Quick Checklist

Before creating/updating a PR:

- [ ] Gate 1: `./scripts/check-all.sh` passes locally (exit 0)
- [ ] Push changes: `git push origin feature-branch`
- [ ] Gate 2: All CI jobs show green
- [ ] Gate 3: Code review shows LGTM
- [ ] Ready to merge!

### 4.3 Anti-Patterns (DO NOT DO)

- Don't request review with failing CI
- Don't skip local checks (`git commit --no-verify`)
- Don't lower quality thresholds to pass
- Don't ignore review feedback
- Don't merge without LGTM

---

## 5. Architecture

### 5.1 Core Philosophy

- **Maximum Quality**: No shortcuts, comprehensive tooling, strict enforcement
- **Composable**: Modular components with clear interfaces
- **Testable**: Every component designed for easy testing
- **Maintainable**: Clear structure, excellent documentation
- **Reproducible**: Consistent behavior across environments

### 5.2 Component Structure

```
grocery-butler/
├── .github/
│   ├── workflows/
│   │   ├── ci.yml                     # CI/CD pipeline
│   │   ├── claude.yml                 # Claude Code integration
│   │   ├── claude-code-review.yml     # Claude code review plugin
│   │   └── code-review.yml           # Manual code review template
├── .claude/
│   ├── agents/                        # AI subagent configurations
│   └── skills/                        # Shared skill files
├── scripts/
│   ├── check-all.sh                   # Run all quality checks
│   ├── test.sh                        # Test suite runner
│   ├── lint.sh                        # Linting
│   ├── format.sh                      # Code formatting (Ruff)
│   ├── security.sh                    # Security scanning
│   ├── typecheck.sh                   # Type checking (MyPy)
│   ├── complexity.sh                  # Complexity analysis (Radon)
│   └── coverage.sh                    # Coverage reporting
├── grocery_butler/
│   ├── __init__.py
│   └── main.py
├── tests/
│   ├── __init__.py
│   └── test_main.py
├── pyproject.toml                     # Project configuration
├── requirements.txt                   # Runtime dependencies
├── requirements-dev.txt               # Development dependencies
├── .pre-commit-config.yaml            # Git hooks
├── CLAUDE.md                          # This file
└── README.md                          # Project overview
```

### 5.3 Key Design Patterns

**Data Processing Pipeline**:
- Modular stages with clear inputs/outputs
- Type-safe transformations with mypy validation
- Comprehensive error handling at each stage
- Logging and observability built-in

**Validation Layer**:
- Schema-based validation using Pydantic
- Custom validators for domain-specific rules
- Clear error messages for validation failures

---

## 6. Quality Standards

### 6.1 Code Quality Requirements

All code must meet these standards before merging to main:

#### Test Coverage
- **Code Coverage**: 90% minimum (branch coverage)
- **Docstring Coverage**: 95% minimum (interrogate)
- **Test Types**: Unit, Integration, and Property-based coverage required

#### Type Checking
- **MyPy**: Strict mode, no `# type: ignore` without justification
- **Type Hints**: All function parameters and return types required
- **Generic Types**: Use for collections (list, dict, etc.)

#### Code Complexity
- **Cyclomatic Complexity**: Max 10 per function
- **Maintainability Index**: Minimum 20 (radon)
- **Max Arguments**: 5 per function
- **Max Branches**: 12 per function
- **Max Lines per Function**: 50 lines

#### Linting and Formatting
- **Ruff**: Linting and formatting (replaces Black + isort)
- **Bandit**: Security scanning with zero exceptions
- **pip-audit**: Dependency vulnerability checking

#### Documentation Standards
- **Google-style Docstrings**: All public APIs
- **Type Hints in Docstrings**: Args, Returns, Raises sections
- **Code Examples**: For complex functions
- **Architecture Decision Records**: For significant decisions
- **README Sections**: Updated when adding new components

### 6.2 Forbidden Patterns

The following patterns are NEVER allowed without explicit justification and issue reference:

1. **Type Ignore**
   ```python
   # FORBIDDEN
   value = some_function()  # type: ignore

   # ALLOWED (with issue reference)
   value = some_function()  # type: ignore  # Issue #42: Third-party lib returns Any
   ```

2. **Suppressed Linting**
   ```python
   # FORBIDDEN
   exec(user_input)  # noqa: S102

   # ALLOWED (with justification)
   exec(trusted_code)  # noqa: S102  # Issue #15: Plugin system requires dynamic execution
   ```

3. **Commented-Out Tests**
   ```python
   # FORBIDDEN
   # def test_something():
   #     assert True

   # ALLOWED
   @pytest.mark.skip(reason="Issue #23: Blocked by upstream bug")
   def test_something():
       assert True
   ```

---

## 7. Development Workflow

### 7.1 Feature Development

1. Create feature branch from main
2. Implement feature with TDD (Red-Green-Refactor)
3. Run `./scripts/check-all.sh` until green
4. Push and create PR
5. Wait for CI green + code review LGTM
6. Merge

### 7.2 Bug Fixes

1. Write failing test that reproduces the bug
2. Fix the bug (make test pass)
3. Run `./scripts/check-all.sh`
4. Push, PR, review, merge

---

## 8. Testing Strategy

### 8.1 Test Types

- **Unit Tests**: Fast, isolated, mock external dependencies
- **Integration Tests**: Test component interactions
- **Property-based Tests**: Hypothesis for edge case discovery

### 8.2 Test Conventions

- File naming: `test_<module>.py`
- Function naming: `test_<what>_<scenario>_<expected>`
- Use fixtures for shared setup
- AAA pattern: Arrange, Act, Assert

---

## 9. Tool Usage & Code Standards

### 9.1 Tool Invocation Patterns

| Task | Command |
|------|---------|
| Format code | `./scripts/format.sh` |
| Fix formatting | `./scripts/format.sh --fix` |
| Check formatting | `./scripts/format.sh --check` |
| Run tests | `./scripts/test.sh` |
| Run specific test | `./scripts/test.sh tests/test_main.py` |
| Type check | `./scripts/typecheck.sh` |
| Lint code | `./scripts/lint.sh` |
| Lint check only | `./scripts/lint.sh --check` |
| Security scan | `./scripts/security.sh` |
| Complexity check | `./scripts/complexity.sh` |
| Coverage report | `./scripts/coverage.sh` |
| All checks | `./scripts/check-all.sh` |

### 9.2 Code Style

- Line length: 88 characters (Ruff default)
- Import sorting: Ruff isort rules (I)
- Naming: PEP 8 conventions
- Type hints: Required on all function signatures

---

## 10. Common Pitfalls & Troubleshooting

### 10.1 No Shortcuts Policy

When a quality check fails, the ONLY acceptable response is to fix the underlying issue:

| Problem | Wrong Response | Right Response |
|---------|---------------|----------------|
| Test fails | Comment out test | Fix the code or test |
| Linting error | Add `# noqa` | Refactor the code |
| Low coverage | Lower threshold | Write more tests |
| Type error | Add `# type: ignore` | Fix the types |
| Complex function | Ignore warning | Split into smaller functions |

### 10.2 Common Issues

**CI passes locally but fails in CI**:
- Check Python version matrix (3.11, 3.12, 3.13)
- Verify PYTHONPATH is set correctly
- Check for OS-specific path issues

**Coverage below threshold**:
- Run `./scripts/coverage.sh --html` and review htmlcov/index.html
- Focus on branch coverage, not just line coverage
- Test error paths and edge cases

---

## Appendix A: AI Subagent Guidelines

### A.1 Subagent Architecture

The project uses a hierarchical agent architecture:

- **Level 0**: Chief Architect - strategic decisions, system-wide coordination
- **Level 2**: Orchestrators - coordinate reviews and cross-cutting concerns
- **Level 3**: Specialists - domain-specific expertise (testing, security, performance, etc.)

### A.2 Subagent Usage Rules

1. Subagents must respect scope boundaries
2. Escalate decisions outside scope to higher-level agents
3. All reviews must be posted to GitHub PRs, never local files
4. Follow the delegation pattern: plan -> delegate -> review

---

## Appendix B: Key Files

| File | Purpose |
|------|---------|
| `CLAUDE.md` | This file - project context for Claude Code |
| `pyproject.toml` | Project configuration and tool settings |
| `.pre-commit-config.yaml` | Git hook definitions |
| `scripts/check-all.sh` | Master quality check script |
| `.github/workflows/ci.yml` | CI/CD pipeline definition |

---

## Appendix C: External References

- [Ruff Documentation](https://docs.astral.sh/ruff/)
- [MyPy Documentation](https://mypy.readthedocs.io/)
- [Pytest Documentation](https://docs.pytest.org/)
- [Bandit Documentation](https://bandit.readthedocs.io/)
