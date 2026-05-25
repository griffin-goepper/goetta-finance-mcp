# goetta-finance

A local-first tool that connects [SimpleFIN](https://bridge.simplefin.org/) to Claude. Your bank data lives only on your machine, in a DuckDB file you own. Claude reads it through an MCP server; you read it through a small web dashboard at `localhost:8765`.

See [`PROJECT_PLAN.md`](./PROJECT_PLAN.md) for the full vision and roadmap.

## Requirements

- Python **3.11+**
- A SimpleFIN Bridge account (about $1.50/month) — sign up at <https://bridge.simplefin.org/>

## Install

`goetta-finance` isn't on PyPI yet. Clone the repo and install in editable mode:

```bash
git clone <repo-url>
cd goetta-finance
python -m venv .venv
.venv/Scripts/activate         # Windows
# source .venv/bin/activate    # macOS/Linux
pip install -e ".[dev]"
```

This puts a `goetta-finance` executable on your `PATH` (inside the venv).

## Quick start

```bash
goetta-finance init       # interactive setup wizard
goetta-finance sync       # pull fresh data from SimpleFIN
goetta-finance web        # open the dashboard at http://127.0.0.1:8765
```

The `init` wizard walks four steps:

1. Claim a SimpleFIN setup token → access URL (stored at `~/.local/share/goetta-finance/config.json`, mode `0600` on POSIX).
2. Initialize the DuckDB store.
3. Pull initial history (up to 90 days).
4. Auto-write the Claude Desktop MCP config (merges into the existing file; preserves any other servers you've configured).

Re-running `init` is safe — each step detects existing state and offers to skip or replace.

## Commands

| Command | What it does |
|---|---|
| `goetta-finance init` | Interactive setup. Re-runnable. |
| `goetta-finance sync` | One-shot pull from SimpleFIN. Idempotent — safe to run repeatedly. |
| `goetta-finance status` | Show last sync, account list with balances, recent warnings/errors. |
| `goetta-finance serve` | Start the MCP server over stdio (used by Claude Desktop). |
| `goetta-finance web` | Start the local web dashboard. `--port 8765` and `--host 127.0.0.1` by default. |
| `goetta-finance daemon` | Long-lived process: dashboard + MCP HTTP endpoint + daily scheduled sync, from one process. See "Daemon mode" below. |
| `goetta-finance account add\|list\|set-balance\|set-liability\|remove` | Manage manual accounts and liability flags. See "Manual accounts and liabilities" below. |
| `goetta-finance category list\|add\|set-rule\|remove-rule\|default-rules` | Manage categories and the rules that map descriptions to them. See "Transaction categorization" below. |
| `goetta-finance transaction categorize\|uncategorize` | Manual per-transaction category overrides. See "Transaction categorization" below. |

## Manual accounts and liabilities

SimpleFIN can't reach every account — 401(k) providers, HSAs, brokerages outside its bank list, and student-loan servicers all sit outside. `goetta-finance account` lets you track those by hand so they show up in MCP queries and the dashboard alongside synced accounts.

### The four subcommands

```bash
# Create a manual account. Prompts interactively for any missing flags.
goetta-finance account add \
  --name "Apple Savings" \
  --org "Apple" \
  --type savings \
  --balance 30000 \
  [--as-of 2026-05-17]     # observation date, defaults to today (UTC)

# Mark an account as a liability (or clear the flag). Works on any account id.
goetta-finance account add ... --liability      # at creation time
goetta-finance account set-liability MANUAL-<uuid> true     # after the fact
goetta-finance account set-liability ACT-<simplefin-id> true   # SimpleFIN accounts too

# Update the balance on a manual account (also writes a balance_snapshot).
goetta-finance account set-balance MANUAL-<uuid> 32500 [--as-of 2026-05-17]

# List all accounts. Manual + liability rows are tagged in the output.
goetta-finance account list

# Remove a manual account. Two layers of safety: cascade-delete its snapshots
# (--force) AND type the account name to confirm (skip with --yes for scripts).
goetta-finance account remove MANUAL-<uuid> --force
```

### Sign convention for liabilities

A liability **always reduces net worth, regardless of how the source signs the balance.** The signed-balance formula is:

```sql
CASE WHEN is_liability THEN -ABS(balance) ELSE balance END
```

So you can enter a student loan either way and the math comes out right:

- `account add --type loan --balance 22500 --liability` (positive amount owed)
- A SimpleFIN credit card showing `balance = -500` and you've flipped it to `is_liability=true`

Both contribute `-500` and `-22500` respectively to net worth — collapsing the loan-servicer convention and the SimpleFIN convention to one answer. The dashboard's net-worth chart and the accounts page footer respect the formula. When writing `sql_query` SELECTs against the `accounts` table, reach for the same `CASE WHEN` expression to compute totals correctly.

`is_liability` is independent of `type` on purpose — `type` describes what kind of account it is (`loan`, `credit`, `investment`), while `is_liability` controls how net-worth math treats it. A margin account is `type=investment` but functionally a liability; you can flip the flag without changing the type.

### Heads-up

- **Retroactive flag.** Toggling `is_liability` re-treats all historical `balance_snapshots` for that account under the new value in net-worth-over-time charts. This is almost always what you want; if it isn't, flip the flag back.
- **CC-credit edge case.** A credit card with `is_liability=true` and a *positive* balance (you overpaid and now have a credit) computes as `-balance` instead of `+balance`. Rare; `set-liability false` while the credit exists, then re-enable, is the workaround.
- **Balance is authoritative.** Payments to a manual loan don't auto-decrement the balance — re-run `set-balance` from your servicer's monthly statement.

## Transaction categorization

Every transaction resolves to a category at *read time* through a SQL view (`transactions_with_category`). Three layers, outermost wins:

1. **Manual override** — a row in `transaction_overrides` for that transaction id.
2. **Rule match** — the lowest-priority rule in `category_rules` whose pattern matches the transaction's description.
3. **`Uncategorized`** — the fallback when nothing else matches.

Read-time resolution is the feature, not an optimization: adding or editing a rule applies retroactively to every existing transaction with zero data migration. A `category_id` column on `transactions` would silently break that.

Migration 0004 ships **14 default categories** (`Groceries`, `Dining`, `Transportation`, `Gas`, `Utilities`, `Subscriptions`, `Rent/Mortgage`, `Healthcare`, `Entertainment`, `Shopping`, `Travel`, `Transfers`, `Income`, `Uncategorized`) and **38 default rules** for common US merchants (Kroger/Trader Joe's → Groceries, Starbucks/DoorDash → Dining, Shell/Exxon → Gas, Spotify/Netflix → Subscriptions, etc.). The seeds aim for "good enough on day one" — your bank's exact descriptions probably differ, so expect to add a handful of rules.

### CLI

```bash
# Inspect what was seeded vs. what you've added.
goetta-finance category list                 # all categories with txn + rule counts
goetta-finance category default-rules        # the is_default=TRUE rule set

# Add a rule. Pattern matches case-insensitively against transaction description.
goetta-finance category set-rule Dining --match contains --pattern 'CHIPOTLE'
goetta-finance category set-rule Dining --match regex --pattern '(?i)venmo.*lunch'

# Remove a rule. Defaults require --force AND a typed-pattern confirmation;
# user-added rules just need the id.
goetta-finance category remove-rule 42
goetta-finance category remove-rule 7 --force        # default rule, prompts for the pattern

# Add a custom category.
goetta-finance category add --name "Gardening" --color "#4ade80"

# Recategorize a single transaction (manual override beats any rule).
goetta-finance transaction categorize <txn-id> Groceries
goetta-finance transaction uncategorize <txn-id>     # back to rule resolution

# Category names are case-insensitive ("dining" → "Dining") and typos get
# a "Did you mean?" suggestion via difflib.
```

### From Claude

The `spending_by_category(start, end)` MCP tool aggregates per-category totals over a date range. By default it returns spending only (amount < 0, Income excluded) as positive dollar magnitudes sorted descending. Pass `include_income=True` to add the Income category as a row whose `total` is *negative* — sign conveys direction (cash in).

`get_transactions(category="Dining", ...)` filters server-side through the view. Every transaction Claude sees carries a resolved `category` field — falling back to `"Uncategorized"`, never `None`.

For anything custom, query the view directly via `sql_query`:

```sql
SELECT category, COUNT(*), SUM(-amount) AS total
FROM transactions_with_category
WHERE posted >= '2026-01-01' AND amount < 0
GROUP BY category ORDER BY total DESC;
```

### Dashboard

- **By category** page: pie chart of the last 30 days' spending, Income excluded.
- **Transactions** page: per-row colored category badge + a category-filter dropdown that narrows via HTMX without a page reload. The badge tooltip pre-fills the CLI command to recategorize that specific transaction id — copy-paste-ready.

Inline categorize-from-dashboard (HTMX dropdown + write endpoint) is deliberately *not* in v1; use the CLI. The reason: the standalone `goetta-finance web` opens the DuckDB store read-only, so a write endpoint would only work in daemon mode and forking dashboard behavior on writability isn't worth it until dogfooding shows frequent re-categorization friction.

### Heads-up

- **Rule patterns are MCP-reachable.** A transaction memo can carry text that tricks Claude into running `category set-rule ... --pattern <evil-regex>`. The CLI validator does best-effort filtering (refuses uncompilable regexes, nested quantifiers like `(X+)+`, large counted repetitions like `(.*a){25}`) but CPython's `re` engine doesn't release the GIL so a runtime regex timeout isn't possible. The load-bearing runtime defense is the existing `query_sql` statement-timeout watchdog (`GOETTA_FINANCE_SQL_TIMEOUT_SECONDS`, default 30s). See [`CLAUDE.md`](./CLAUDE.md) for the threat model.
- **Default rules are USA-biased.** Rename a default rule by removing it (`--force`) and adding your own, or just add a higher-priority rule that wins (lower number = higher precedence).
- **One category per transaction.** Costco-style mixed purchases get one label. No splits in v1.
- **Default rules don't re-seed if you delete them.** Migrations run once per database; the slate stays where you leave it. To add new defaults later, add a new migration file (`0005_default_rules_expansion.sql`, etc.) — never edit `0004_categorization.sql`.

## Daemon mode

`goetta-finance daemon` runs one long-lived process that hosts:

- The dashboard at `http://127.0.0.1:8765/`
- The MCP endpoint at `http://127.0.0.1:8765/api/mcp` (streamable-HTTP transport — Claude Code and Claude Desktop both support this)
- An internal scheduler that runs `collect()` daily at `--sync-at` local time (default `06:00`)

One process means one DuckDB write handle, which is what sidesteps the Windows DuckDB-lock conflict between `serve` and `web`. If the laptop was closed past the daily tick, the scheduler detects "we slept through it" on wake and runs a catch-up sync immediately.

```bash
goetta-finance daemon                         # defaults: 127.0.0.1:8765, sync at 06:00 local
goetta-finance daemon --sync-at 03:30         # sync nightly at 3:30am
goetta-finance daemon --no-schedule           # MCP + dashboard only, no automatic sync
goetta-finance daemon --no-mcp                # dashboard + scheduler only (e.g. headless server)
```

To register the daemon's MCP endpoint with Claude Code:

```bash
claude mcp add goetta-finance --scope user --transport http http://127.0.0.1:8765/api/mcp
```

(Re-run `goetta-finance init` to pick the daemon path interactively — it will also clear any stale stdio registration first.)

**In v1 the daemon does not auto-start.** Keep it running in a separate terminal, or install one of the scheduling snippets below to start it at login.

## Scheduling

You can run `goetta-finance` two ways: with the daemon (continuous, lazy-sync triggered too), or with an OS scheduler running `goetta-finance sync` periodically. The daemon is the better default when you want the MCP endpoint always available. Use the OS scheduler when you just want fresh data and the dashboard on demand.

### Linux — systemd user units

```ini
# ~/.config/systemd/user/goetta-finance.service
[Unit]
Description=goetta-finance daily sync

[Service]
Type=oneshot
ExecStart=%h/.local/bin/goetta-finance sync
```

```ini
# ~/.config/systemd/user/goetta-finance.timer
[Unit]
Description=goetta-finance daily sync at 06:00

[Timer]
OnCalendar=*-*-* 06:00:00
Persistent=true

[Install]
WantedBy=timers.target
```

```bash
systemctl --user daemon-reload
systemctl --user enable --now goetta-finance.timer
```

Replace `goetta-finance sync` with `goetta-finance daemon` and drop the timer if you want the daemon at login instead.

### macOS — launchd

```xml
<!-- ~/Library/LaunchAgents/com.user.goetta-finance.plist -->
<?xml version="1.0" encoding="UTF-8"?>
<!DOCTYPE plist PUBLIC "-//Apple//DTD PLIST 1.0//EN"
  "http://www.apple.com/DTDs/PropertyList-1.0.dtd">
<plist version="1.0">
<dict>
  <key>Label</key><string>com.user.goetta-finance</string>
  <key>ProgramArguments</key>
  <array>
    <string>/usr/local/bin/goetta-finance</string>
    <string>sync</string>
  </array>
  <key>StartCalendarInterval</key>
  <dict><key>Hour</key><integer>6</integer><key>Minute</key><integer>0</integer></dict>
</dict>
</plist>
```

```bash
launchctl load ~/Library/LaunchAgents/com.user.goetta-finance.plist
```

For daemon-at-login, swap the `ProgramArguments` to `[..., "daemon"]` and replace `StartCalendarInterval` with `<key>RunAtLoad</key><true/><key>KeepAlive</key><true/>`.

### Windows — Task Scheduler

```powershell
# Daily sync at 06:00
schtasks /Create /TN "goetta-finance sync" `
  /TR '"C:\path\to\goetta-finance.exe" sync' `
  /SC DAILY /ST 06:00

# Or start the daemon at login (foreground in a window)
schtasks /Create /TN "goetta-finance daemon" `
  /TR '"C:\path\to\goetta-finance.exe" daemon' `
  /SC ONLOGON
```

## Claude clients

`goetta-finance serve` is a stdio MCP server — it talks to a Claude client over a local pipe. Which clients work:

| Client | Works? | How `init` registers it |
|---|---|---|
| **Claude Code** (`claude` CLI) | ✅ best path | `claude mcp add goetta-finance --scope user -- <full-path>\goetta-finance.exe serve`. `init` does this automatically if it finds `claude` on PATH. |
| **Claude Desktop** (direct download from claude.ai/download) | ✅ | `init` writes `%APPDATA%\Claude\claude_desktop_config.json` (or `~/Library/Application Support/Claude/` on macOS). Fully quit and reopen Claude Desktop after. |
| **Claude Desktop** (Microsoft Store install) | ⚠ known gap | MSIX sandboxing redirects config reads away from `%APPDATA%\Claude\`. The wizard writes the wrong path; the tools never appear. Workaround: use Claude Code, or uninstall the Store build and install Claude Desktop from claude.ai/download. |
| **claude.ai web** | ❌ | The web app can't spawn local processes. Only remote (HTTP/OAuth) MCP servers work there, and exposing your bank data over HTTP would defeat the local-first design. |

`init` runs through whichever clients it detects in step [4/4]. You can re-run `init` at any time to refresh registrations without going through the SimpleFIN steps again.

## Using it from Claude

Once registered, restart your Claude client and try things like:

- *"What's my checking balance?"* → `list_accounts`
- *"Show me everything I spent at Starbucks last month"* → `get_transactions(search="Starbucks", ...)`
- *"Chart my net worth over the last 90 days"* → `account_balance_history` + Claude renders an inline chart artifact
- *"How much did I spend on dining last month?"* → `spending_by_category` returns categorized totals (Income excluded by default)
- *"Is the data current?"* → `sync_status` reports last sync + freshness

The MCP server exposes seven tools:

- **`list_accounts`** — all accounts with current balances
- **`get_transactions`** — filter by account, date range, category, text search; up to 1000 rows. Every row carries a resolved `category` field.
- **`account_balance_history`** — per-account balance snapshots over time
- **`spending_by_category`** — categorized spending totals between two dates. Income excluded by default; opt in via `include_income=True`.
- **`sql_query`** — read-only SQL against the local DuckDB store (see security notes below). Prefer `transactions_with_category` over the bare `transactions` table when you want category info.
- **`sync_status`** — report when the SimpleFIN data was last synced and whether it's stale
- **`sync_now`** — trigger a fresh pull from SimpleFIN

`sql_query` is the workhorse for anything the other tools don't cover: most natural-language questions collapse to a SQL query plus a Claude-rendered artifact. The MCP server intentionally has no `chart` tool — Claude renders inline charts as artifacts from the data tools return.

## The web dashboard

`goetta-finance web` serves six views at `http://127.0.0.1:8765`:

- **Accounts** — current balances and as-of timestamps
- **Net worth** — Plotly line chart from balance snapshots
- **Spending** — monthly income (up) and spending (down) stacked bars
- **By category** — pie chart of the last 30 days' spending (Income excluded)
- **Transactions** — sortable, searchable table with a category filter and a colored category badge per row. Filters update via HTMX without full page reloads. The badge tooltip carries the pre-filled CLI command to recategorize that specific transaction.
- **Sync** — last sync time and recent warnings/errors

The dashboard binds to `127.0.0.1` only by default. If you pass a non-loopback `--host`, the CLI prints a warning — **there is no auth**. Don't expose this to a network you don't fully trust.

## Where your data lives

Default paths (XDG-compliant on Linux/macOS, follows the same layout on Windows):

```
~/.local/share/goetta-finance/
├── config.json          # mode 0600 on POSIX; contains your SimpleFIN access URL
└── data.duckdb          # the database
```

Override the location with `GOETTA_FINANCE_HOME=/some/other/dir` or `$XDG_DATA_HOME`.

The SimpleFIN access URL is sensitive — it grants read access to your bank data. The default `chmod 0600` keeps it owner-only on Linux/macOS; on Windows, file ACLs apply.

## Privacy and security

- **No telemetry, no auto-update checks, no analytics.** The only outbound network call is to SimpleFIN itself during `sync`. The dashboard's HTMX and Plotly assets are bundled locally — no CDN requests when you load it.
- **`sql_query` has three layers of defense in depth** to prevent prompt injection through transaction memo / payee fields:
  1. A pre-flight prefix whitelist (`SELECT/WITH/SHOW/DESCRIBE`). `EXPLAIN` is deliberately excluded.
  2. A `BEGIN TRANSACTION READ ONLY` wrapper — DuckDB's storage layer refuses in-database mutations that slip the whitelist (e.g., `WITH cte AS (...) DELETE FROM accounts`).
  3. The DuckDB connection is opened with `enable_external_access=false` (immutable at runtime), which blocks `read_csv`, `read_blob`, `COPY ... TO 'file'`, and `httpfs` URLs — closing the information-disclosure and filesystem-exfiltration vectors.

  See [`CLAUDE.md`](./CLAUDE.md) "Things to avoid" for the full threat model and regression tests.

## Known limitations

- **Microsoft Store install of Claude Desktop**: see the Claude clients table above. Use Claude Code or the direct-download Claude Desktop until `init` learns the MSIX-sandboxed config path.
- **On Windows, `serve` and `web` cannot run simultaneously as separate processes.** DuckDB takes an exclusive OS file lock on the database even for a read-only handle. Use `goetta-finance daemon` (one process, both surfaces) to avoid the conflict, or stop one before starting the other. macOS/Linux use advisory POSIX locks so concurrent read-only + read-write *may* work, but it isn't relied upon.
- **Pending transactions are dropped.** Only `posted` transactions are stored in v1. SimpleFIN's pending feed will be supported in a later phase.
- **Single currency assumption.** Money is stored as `DECIMAL(18,2)` and charts use `USD` labels. Multi-currency support isn't here yet.
- **Categorization is flat and rule-based.** No hierarchy, no transaction splits, no LLM auto-categorization, no transfer dedup (transfers between your own accounts show up in both balances). The default rules are USA-merchant biased; you'll add your own — see "Transaction categorization" above.
- **No inline category editing in the dashboard.** Recategorize via the `goetta-finance transaction categorize` CLI; the transactions page surfaces the exact command in each badge's tooltip.

## Development

```bash
pip install -e ".[dev]"

# All tests (POSIX-only file-permission test skips on Windows)
pytest

# Targeted runs
pytest tests/test_collector.py -v
pytest -k "query_sql" -v

# Lint and type-check
ruff check .
ruff format --check .
mypy --strict src/goetta_finance
```

A passing change has all four clean.

### Security tooling

One-time setup:

```bash
pipx install pre-commit
pre-commit install

# gitleaks is a Go binary, install via your OS package manager:
#   macOS:   brew install gitleaks
#   Windows: winget install gitleaks
#   Linux:   see https://github.com/gitleaks/gitleaks/releases or your distro's repo
```

`pre-commit install` wires bandit / ruff / gitleaks into your `git commit` flow automatically. Manual audit run (e.g. before tagging a release):

```bash
bandit -r src/ -c pyproject.toml
pip-audit
gitleaks detect --source . --redact
ruff check .                       # includes ruff's S (bandit-derived) rules
```

Raw scanner output is git-ignored on purpose — see [`docs/SECURITY_AUDIT_2026-05.md`](./docs/SECURITY_AUDIT_2026-05.md) for the narrative-summary policy. New findings should be reported there, not in raw JSON.

[`CLAUDE.md`](./CLAUDE.md) documents the operating principles, project layout, and patterns for adding new MCP tools, storage backends, or SimpleFIN fields. Read it before opening a PR.

## License

Not yet declared. The project isn't published; choose a license before publishing.
