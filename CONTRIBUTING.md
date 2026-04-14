# Contributing to Backup Sentinel

Thanks for your interest in contributing! This project aims to be a practical, compliance-focused monitoring tool for Proxmox backup infrastructure. Contributions of all sizes are welcome — bug reports, documentation fixes, new features, and test coverage.

## Getting Started

### Prerequisites

- Python 3.12+
- Docker & Docker Compose
- A PostgreSQL instance for testing (docker-compose brings one)

### Local Development

```bash
# Clone and set up
git clone https://github.com/wvogel/backup-sentinel.git
cd backup-sentinel
cp .env.example .env
cp oauth2-proxy.env.example oauth2-proxy.env

# Generate a Fernet key
python -c "from cryptography.fernet import Fernet; print(Fernet.generate_key().decode())"
# Paste it into .env as BSENTINEL_SECRET_KEY

# Start the stack
docker compose up -d

# Or run the app locally (requires a running Postgres)
python -m venv .venv
source .venv/bin/activate
pip install -r requirements.txt
uvicorn app.main:app --reload --host 0.0.0.0 --port 8000
```

### Running Tests

```bash
pip install pytest
pytest tests/ -v
```

### Linting

We use [ruff](https://docs.astral.sh/ruff/) for linting and formatting.

```bash
pip install ruff==0.14.0
ruff check app tests scripts
ruff format app tests scripts
```

### Pre-commit Hooks

```bash
pip install pre-commit
pre-commit install
```

Hooks will run automatically on every `git commit`.

## Project Structure

```
backup-sentinel/
├── app/
│   ├── i18n/              # Translations (en.json, de.json)
│   ├── services/          # Business logic (sync, notifications, PDF, crypto)
│   ├── web/               # FastAPI route handlers
│   ├── db_*.py            # Database query modules
│   ├── config.py          # Environment-driven configuration
│   └── main.py            # App entry point + middleware
├── templates/             # Jinja2 HTML templates
├── static/                # CSS, JS, icons
├── scripts/               # Bootstrap shell scripts, CLI tools
├── tests/                 # pytest suite
└── docs/                  # User and admin documentation
```

## Commit Style

- Use a short, imperative summary line (`fix: ...`, `feat: ...`, `docs: ...`, `chore: ...`)
- Explain the *why* in the body if it isn't obvious
- One logical change per commit
- Sign your commits with `Co-Authored-By:` if pairing

## Pull Requests

1. Fork the repo and create a feature branch.
2. Make your changes. Include tests for new behavior.
3. Run `ruff check` and `ruff format --check` — both should be clean.
4. Run `pytest` — all tests should pass.
5. Update documentation in `docs/` if you change user-visible behavior.
6. Update `CHANGELOG.md` under `[Unreleased]`.
7. Open a PR with a clear description of what and why.

## Adding a Translation

1. Add keys to both `app/i18n/en.json` and `app/i18n/de.json` (sorted by section).
2. Use `{{ t("key.name") }}` in templates and `T("key.name")` in JS.
3. For variable substitution, use `{placeholder}` in the JSON and `t("key", placeholder=value)` in code.

## Reporting Security Issues

Please do **not** open a public issue for security vulnerabilities. Contact the maintainers privately first. See the repository's security policy for details.

## License

By contributing, you agree that your contributions will be licensed under the [MIT License](LICENSE).
