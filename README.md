# grocery-butler

Grocery Butler - A quality-controlled Python project.

## Description

This project follows maximum quality standards from day one, including:

- Comprehensive testing infrastructure (pytest with 90%+ coverage requirement)
- Code quality tools (ruff, mypy)
- Security scanning (bandit, pip-audit)
- Complexity analysis (radon, xenon)
- Pre-commit hooks
- CI/CD pipeline (GitHub Actions)
- AI-assisted development (Claude Code skills and subagents)

## Installation

```bash
# Clone the repository
git clone <repository-url>
cd grocery-butler

# Install dependencies
pip install -r requirements-dev.txt

# Install pre-commit hooks
pre-commit install
```

## Usage

Run the Hello World application:

```bash
python -m grocery_butler.main
```

Expected output:
```
Hello from grocery-butler!
```

## Development

### Running Quality Checks

```bash
# Run all quality checks (recommended before commit)
pre-commit run --all-files

# Or run individual checks:
./scripts/test.sh          # Run tests with coverage
./scripts/lint.sh          # Run linting
./scripts/format.sh --fix  # Auto-format code
./scripts/typecheck.sh     # Run type checking
./scripts/check-all.sh     # Run all checks
```

### Quality Tools

This project includes:

- **pytest**: Testing framework with 90%+ coverage requirement
- **ruff**: Fast Python linter and formatter (replaces flake8, black, isort)
- **mypy**: Static type checker
- **bandit**: Security linter
- **pip-audit**: Dependency vulnerability scanner
- **radon/xenon**: Code complexity analysis (≤10 cyclomatic complexity)
- **pre-commit**: Git hooks framework

### Project Structure

```
grocery-butler/
├── grocery_butler/     # Main package
│   ├── __init__.py
│   └── main.py
├── tests/                # Test suite
│   ├── __init__.py
│   └── test_main.py
├── scripts/              # Quality control scripts
│   ├── check-all.sh
│   ├── test.sh
│   ├── lint.sh
│   ├── format.sh
│   ├── typecheck.sh
│   ├── coverage.sh
│   ├── security.sh
│   ├── complexity.sh
│   └── mutation.sh
├── .github/workflows/    # CI/CD pipelines
├── .claude/              # AI subagents and skills
├── requirements.txt      # Runtime dependencies
├── requirements-dev.txt  # Development dependencies
├── pyproject.toml        # Tool configurations
└── .pre-commit-config.yaml  # Pre-commit hooks
```

### Testing

```bash
# Run tests
./scripts/test.sh

# Run tests with coverage report
./scripts/coverage.sh

# Run tests with HTML coverage report
./scripts/coverage.sh --html
# View htmlcov/index.html in browser
```

### Code Quality

This project maintains MAXIMUM QUALITY standards:

- **Test Coverage**: ≥90% required
- **Cyclomatic Complexity**: ≤10 per function
- **All Linters**: Must pass with zero violations
- **Type Coverage**: 100% type hints

## Deploying to Railway

The project includes a `Procfile` for [Railway](https://railway.app/) deployment with two processes:

| Process | Command | Description |
|---------|---------|-------------|
| `web` | `gunicorn grocery_butler.app:create_app()` | Flask web app |
| `worker` | `python -m grocery_butler.bot` | Discord bot |

### Required Environment Variables

Set these in your Railway project settings:

| Variable | Required | Description |
|----------|----------|-------------|
| `DATABASE_URL` | Yes | PostgreSQL connection URL (auto-injected by Railway Postgres plugin) |
| `ANTHROPIC_API_KEY` | Yes | Anthropic API key for Claude-powered features |
| `DISCORD_BOT_TOKEN` | Yes (worker) | Discord bot token for the worker process |
| `FLASK_SECRET_KEY` | Yes (web) | Stable secret key for Flask sessions (generate once, reuse) |
| `SAFEWAY_USERNAME` | Yes | Safeway account username |
| `SAFEWAY_PASSWORD` | Yes | Safeway account password |
| `SAFEWAY_STORE_ID` | Yes | Safeway store ID for product searches |
| `PORT` | Auto | Injected by Railway for the web process |

### Quick Start

1. Connect your Railway project to this GitHub repo
2. Add a PostgreSQL plugin (provides `DATABASE_URL` automatically)
3. Set the required environment variables above
4. Railway auto-deploys on push to `main`

## License

MIT License
