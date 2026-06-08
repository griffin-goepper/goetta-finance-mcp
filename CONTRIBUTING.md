# Contributing to goetta-finance

Thanks for your interest. This is a local-first personal-finance tool — small, opinionated, and meant to stay that way.

## Setup

```bash
git clone https://github.com/griffin-goepper/goetta-finance-mcp.git
cd goetta-finance-mcp
python -m venv .venv && . .venv/bin/activate    # Windows: .venv\Scripts\activate
pip install -e ".[dev]"
```

## The bar for a passing change

A change is ready when all four are green:

```bash
ruff check .
ruff format --check .
mypy --strict src/goetta_finance
pytest
```

New behavior needs tests. We pin **outcomes** (what the user/Claude observes), not mechanisms — so a refactor that preserves behavior shouldn't churn the test suite.

## Read these first

- **[`CLAUDE.md`](./CLAUDE.md)** — operating principles, project layout, and the templates for adding an MCP tool, a storage backend, a SimpleFIN field, a migration, or a boolean flag. The "Things to avoid" section is a list of mistakes already made and fixed; it's worth skimming before a non-trivial change.
- **[`PROJECT_PLAN.md`](./PROJECT_PLAN.md)** — the vision, the phases, and what's intentionally out of scope (no budgeting-app features, no multi-tenancy, no cloud sync).
- **[`CUSTOMIZATION.md`](./CUSTOMIZATION.md)** — the map of user-tunable surfaces, useful for understanding which state is the user's vs. the codebase's.

## Two principles that shape most decisions

1. **Local-first is non-negotiable.** No telemetry, no auto-update checks, no analytics, no network calls except to SimpleFIN. The dashboard's JS/CSS is bundled, not CDN-loaded. If a feature seems to need an outbound call, raise it in an issue first.
2. **Apply the stranger test.** Would someone installing this tomorrow with their own SimpleFIN credentials benefit equally, or is the change tuned to one person's bank/merchants? User-state (rules, categories, account flags) lives in the user's DB or local config; codebase state (default seeds, analysis patterns) stays minimal and broadly applicable, with extension paths documented.

## Security tooling

The repo ships `bandit` / `ruff S` / `pip-audit` / `gitleaks` wired through pre-commit and CI. See the README's [**Security tooling**](./README.md#security-tooling) section for one-time setup and the manual audit workflow. New security findings get documented in `docs/SECURITY_AUDIT_2026-05.md` at the class-of-issue level — not as raw scanner output.

## Opening a PR

Keep changes to one concern (operating principle #3 — don't bundle the collector refactor with the SQL-tool work). Describe what changed, anything you decided that wasn't obvious, and anything you stubbed or skipped. CI runs the four gates above plus the security scanners.
