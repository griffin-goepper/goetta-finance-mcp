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
- *"How much did I spend on dining in February?"* → `sql_query` against the DuckDB store

The MCP server exposes five tools:

- **`list_accounts`** — all accounts with current balances
- **`get_transactions`** — filter by account, date range, text search; up to 1000 rows
- **`account_balance_history`** — per-account balance snapshots over time
- **`sql_query`** — read-only SQL against the local DuckDB store (see security notes below)
- **`sync_now`** — trigger a fresh pull from SimpleFIN

`sql_query` is the workhorse: most natural-language questions collapse to a SQL query plus a Claude-rendered artifact. The MCP server intentionally has no `chart` tool — Claude renders inline charts as artifacts from the data tools return.

## The web dashboard

`goetta-finance web` serves five views at `http://127.0.0.1:8765`:

- **Accounts** — current balances and as-of timestamps
- **Net worth** — Plotly line chart from balance snapshots
- **Spending** — monthly income (up) and spending (down) stacked bars
- **Transactions** — sortable, searchable table; filters update via HTMX without full page reloads
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
- **No transaction categorization.** Spending charts group by income vs. spending only; per-category breakdowns require manual SQL via `sql_query` (or wait for a later phase).

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

[`CLAUDE.md`](./CLAUDE.md) documents the operating principles, project layout, and patterns for adding new MCP tools, storage backends, or SimpleFIN fields. Read it before opening a PR.

## License

Not yet declared. The project isn't published; choose a license before publishing.
