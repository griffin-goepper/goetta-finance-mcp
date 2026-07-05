# goetta-finance — Project Plan

> Working name. Rename freely. Suggestions: `pocketbook`, `ledger-chat`, `coin`, `tellr`.

## 1. Vision

An open-source, local-first MCP server that lets people chat with their finances through Claude (or any MCP-capable client) without their data ever leaving their machine.

**The promise to users:**

- Quick setup that gets them connected to SimpleFIN and an MCP client in under 5 minutes.
- Their data lives only on their device, in a format they own.
- Natural-language interaction with their finances via Claude.
- Ad-hoc charts generated inline in chat — Claude builds them as artifacts from the data the server exposes.
- A local web dashboard for persistent, interactive views.

## 2. Non-goals

- Bank integrations beyond SimpleFIN. Use what works.
- Budgeting features that compete with Actual Budget or Firefly III. **Carve-out:** lightweight *goals* — per-category spending caps (month/year) and per-account balance targets (`at_least`/`at_most`, optional target date) — are in scope because they are read-time-computed thresholds over data we already store, with no envelopes, allocations, rollover, or stored budget state.
- Multi-user, multi-tenancy, or cloud hosting. This is local-first by design.
- Mobile apps.

## 3. Architecture

```
┌─────────────────────────────────────────────────────────┐
│                     User's machine                       │
│                                                          │
│   ┌──────────────┐         ┌─────────────────────────┐   │
│   │ Claude       │ ◄─MCP─► │ goetta-finance server      │   │
│   │ Desktop /    │ (stdio) │                         │   │
│   │ Code /       │         │  ┌──────┐  ┌─────────┐  │   │
│   │ Web          │         │  │tools │  │collector│  │   │
│   └──────────────┘         │  └──┬───┘  └────┬────┘  │   │
│                            │     │           │       │   │
│                            │  ┌──▼───────────▼────┐  │   │
│                            │  │  FinanceStore     │  │   │
│                            │  │  (DuckDB default) │  │   │
│                            │  └────┬──────────────┘  │   │
│                            └───────│─────────────────┘   │
│   ┌──────────────┐                 │                     │
│   │ Web dashboard│ ◄────HTTP───────┘                     │
│   └──────────────┘                                       │
└──────────────────┬───────────────────────────────────────┘
                   │
                   ▼ HTTPS (SimpleFIN access URL)
            ┌─────────────┐
            │ SimpleFIN   │
            │ Bridge      │
            └─────────────┘
```

Three independent processes, three clean boundaries:

1. **MCP server (`goetta-finance serve`)** — long-lived, stdio transport, exposes tools to Claude.
2. **Collector (`goetta-finance sync`)** — short-lived, pulls from SimpleFIN and writes to the store. Triggered by cron, by `goetta-finance daemon`, or lazily by the MCP server on stale-data detection.
3. **Web dashboard (`goetta-finance web`)** — long-lived, serves `localhost:8765`. Reads the store, never writes. Home for persistent interactive views (Plotly charts, saved queries).

### Where charts actually render

There are two distinct rendering surfaces, and they do different jobs:

- **Ad-hoc charts in chat** — produced by Claude itself as artifacts (HTML/React/SVG). The MCP server returns *data*; Claude renders. The server has no chart tool. This is disposable, single-question visualization ("chart my dining spend month-over-month").
- **The local web dashboard** — Plotly running in a real browser. Persistent, interactive, bookmarkable. The thing you actually open in the morning. This is where rich visualization lives because Claude clients today can't render arbitrary HTML returned from MCP tool calls reliably (see [anthropic-sdk-python #1329](https://github.com/anthropics/anthropic-sdk-python/issues/1329) for the open issue on inline image rendering from MCP).

This split is load-bearing — it's why the dashboard isn't optional and why we don't ship a `chart` MCP tool.

## 4. Tech stack

| Concern | Choice | Rationale |
|---|---|---|
| Language | Python 3.11+ | Best MCP SDK, DuckDB is first-class, fast iteration |
| MCP SDK | `mcp` (official Python SDK) | The only sensible choice |
| Storage (default) | DuckDB | Embedded, columnar, excellent for analytical queries Claude will run |
| Storage (alts) | SQLite (stdlib), JSONL files | Cover users with different priorities |
| Charts (dashboard only) | Plotly | Interactive HTML inside the local web dashboard. The MCP server itself returns data, never charts — Claude renders inline charts as artifacts from that data. |
| CLI | `typer` | Clean ergonomics, good help text |
| Models | `pydantic` v2 | Validation at the SimpleFIN boundary |
| HTTP client | `httpx` | Modern, sync + async |
| Web dashboard | FastAPI + HTMX | Lightweight, no SPA build step |
| Distribution | `pipx install goetta-finance`; also `uvx goetta-finance` | Standard Python CLI distribution |
| Config root | `$XDG_DATA_HOME/goetta-finance/` (default `~/.local/share/goetta-finance/`) | XDG-compliant, override via `GOETTA_FINANCE_HOME` |

## 5. Data model

Pydantic models on the wire, projected onto whatever storage backend is configured.

```python
class Account(BaseModel):
    id: str                    # SimpleFIN account ID, primary key
    org_id: str | None
    org_name: str | None       # e.g. "Chase", "Vanguard"
    name: str                  # e.g. "Checking 1234"
    currency: str = "USD"
    balance: Decimal
    available_balance: Decimal | None
    balance_date: datetime
    type: AccountType | None   # checking/savings/credit/investment/loan/other
    extra: dict[str, Any] = {} # raw passthrough, for future use

class Transaction(BaseModel):
    id: str                    # SimpleFIN transaction ID, primary key
    account_id: str
    posted: datetime
    transacted_at: datetime | None
    amount: Decimal            # signed; negative = money out
    description: str
    payee: str | None          # extracted/cleaned downstream
    memo: str | None
    pending: bool = False
    extra: dict[str, Any] = {}

class BalanceSnapshot(BaseModel):
    account_id: str
    balance: Decimal
    timestamp: datetime

class SyncRun(BaseModel):
    started_at: datetime
    finished_at: datetime
    accounts_touched: int
    transactions_new: int
    transactions_updated: int
    warnings: list[str]
    errors: list[str]
```

DuckDB schema (initial migration `0001_init.sql`):

```sql
CREATE TABLE accounts (
    id TEXT PRIMARY KEY,
    org_id TEXT,
    org_name TEXT,
    name TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    balance DECIMAL(18,2) NOT NULL,
    available_balance DECIMAL(18,2),
    balance_date TIMESTAMP NOT NULL,
    type TEXT,
    extra JSON,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE transactions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    posted TIMESTAMP NOT NULL,
    transacted_at TIMESTAMP,
    amount DECIMAL(18,2) NOT NULL,
    description TEXT NOT NULL,
    payee TEXT,
    memo TEXT,
    pending BOOLEAN NOT NULL DEFAULT FALSE,
    extra JSON,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_transactions_account_posted ON transactions(account_id, posted DESC);
CREATE INDEX idx_transactions_posted ON transactions(posted DESC);

CREATE TABLE balance_snapshots (
    account_id TEXT NOT NULL REFERENCES accounts(id),
    timestamp TIMESTAMP NOT NULL,
    balance DECIMAL(18,2) NOT NULL,
    PRIMARY KEY (account_id, timestamp)
);

CREATE TABLE sync_runs (
    id INTEGER PRIMARY KEY,
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    accounts_touched INTEGER NOT NULL DEFAULT 0,
    transactions_new INTEGER NOT NULL DEFAULT 0,
    transactions_updated INTEGER NOT NULL DEFAULT 0,
    warnings JSON,
    errors JSON
);
```

## 6. Storage backend interface

Pluggable so users can pick. SQLite and JSONL backends are stretch goals — DuckDB only for v1.

```python
class FinanceStore(Protocol):
    def init(self) -> None: ...
    def upsert_accounts(self, accounts: list[Account]) -> None: ...
    def upsert_transactions(self, txns: list[Transaction]) -> SyncResult: ...
    def record_balance_snapshot(self, snap: BalanceSnapshot) -> None: ...
    def record_sync_run(self, run: SyncRun) -> int: ...

    # Read side (used by MCP tools)
    def last_sync_time(self) -> datetime | None: ...
    def get_accounts(self) -> list[Account]: ...
    def get_transactions(self, *, account_id: str | None = None,
                         start: datetime | None = None,
                         end: datetime | None = None,
                         limit: int | None = None) -> list[Transaction]: ...
    def get_balance_history(self, account_id: str,
                            since: datetime) -> list[BalanceSnapshot]: ...
    def query_sql(self, sql: str) -> list[dict]: ...   # read-only, see §7
```

`query_sql` is the read-only escape hatch for Claude to run arbitrary analytical queries against the store. Crucial for the natural-language UX — see §7.

## 7. MCP tool surface (v1)

Five tools is plenty. Resist adding more until users ask.

| Tool | Purpose |
|---|---|
| `list_accounts` | All accounts with current balance. No args. |
| `get_transactions` | Filter by account, date range, text search. Returns up to `limit` rows. |
| `account_balance_history` | Time series of balance snapshots for an account. |
| `sql_query` | Read-only SQL against the DuckDB store. Includes schema in the tool description so Claude knows what to query. |
| `sync_now` | Triggers a fresh pull from SimpleFIN. Returns the sync result. |

`sql_query` is the secret weapon. Most "natural language to finance insight" requests collapse to "Claude writes a SQL query against the store, gets results back, then either explains them or renders them as an inline artifact." Keep `get_transactions` for ergonomic common-case retrieval; lean on `sql_query` for everything else.

### Why there's no `chart` tool

Earlier drafts of this plan included one. It got cut because MCP clients today don't reliably render arbitrary HTML or interactive widgets returned from tool calls — image content blocks from MCP tools get buried in a collapsed "tool use" accordion on claude.ai, and Claude Desktop's image rendering from MCP has historically been unreliable.

The pattern that *does* work today: the MCP server returns data via `sql_query`, and Claude generates a chart **artifact** (HTML, React, or SVG) as part of its own response. That artifact renders inline in claude.ai web and Claude Desktop because it's a first-class artifact, not an MCP tool result. Tool descriptions should hint at this so Claude reaches for it: e.g., the `sql_query` description ends with *"Results are well-suited to be visualized as an inline chart artifact when the user asks for a visualization."*

Persistent, interactive Plotly visualization happens in the web dashboard, not in chat.

This decision can be revisited if MCP rendering gains first-class support for HTML/widget responses in Claude clients.

## 8. SimpleFIN client

Wraps the Bridge API. Key responsibilities:

- Claim setup token → access URL (one-time).
- Fetch `/accounts` with `start-date`, `end-date` params.
- Chunk requests into ≤90-day windows when the date range exceeds that.
- Surface Bridge warnings (rate limits, date caps) up the stack.
- Never log the access URL.

```python
class SimpleFinClient:
    def __init__(self, access_url: str): ...
    def claim(setup_token: str) -> str: ...  # @classmethod
    def fetch(self, start: datetime, end: datetime) -> SimpleFinResponse: ...
    def fetch_chunked(self, start: datetime, end: datetime,
                      chunk_days: int = 60) -> Iterator[SimpleFinResponse]: ...
```

Note: chunk at 60 days, not 90, to leave headroom for the 45-day "recommended" cap SimpleFIN is signaling they may enforce.

## 9. Collector

The function that pulls from SimpleFIN and writes to the store.

```python
def collect(store: FinanceStore, client: SimpleFinClient, *,
            overlap_days: int = 5) -> SyncRun:
    last = store.last_sync_time() or (now() - timedelta(days=90))
    start = last - timedelta(days=overlap_days)
    end = now()

    run = SyncRun(started_at=now(), ...)
    for chunk in client.fetch_chunked(start, end):
        accounts = parse_accounts(chunk)
        txns = parse_transactions(chunk)

        store.upsert_accounts(accounts)
        result = store.upsert_transactions(txns)
        for acct in accounts:
            store.record_balance_snapshot(
                BalanceSnapshot(account_id=acct.id,
                                balance=acct.balance,
                                timestamp=acct.balance_date))

        run.transactions_new += result.new
        run.transactions_updated += result.updated
        run.warnings.extend(chunk.warnings)

    run.finished_at = now()
    store.record_sync_run(run)
    return run
```

Non-obvious requirements:

1. **Overlap window** — always re-pull the last 5 days. Banks post transactions late.
2. **Balance snapshot every sync**, even when balance is unchanged.
3. **Idempotent** — dedup on SimpleFIN's IDs. Never generate your own.
4. **Pending transactions** — v1 ignores them (`pending=True` rows are dropped). v2 stores them separately.
5. **Quota awareness** — log a warning if Bridge returns a rate-limit warning. Don't auto-retry tight loops.

## 10. CLI

Single entry point: `goetta-finance <command>`.

```
goetta-finance init           # interactive setup wizard
goetta-finance sync           # one-shot pull from SimpleFIN
goetta-finance serve          # start the MCP server (stdio)
goetta-finance daemon         # long-running process that syncs on a schedule
goetta-finance status         # show last sync, account count, errors
goetta-finance config         # view/edit config
goetta-finance web            # start the local dashboard at localhost:8765
```

### `goetta-finance init` flow

```
Welcome to goetta-finance! Let's get you set up. (~3 minutes)

[1/4] SimpleFIN account
  You'll need a SimpleFIN Bridge account ($1.50/mo) to connect your banks.
  Sign up at https://bridge.simplefin.org/ if you haven't.
  Press Enter to open the SimpleFIN token page in your browser...

  [opens https://bridge.simplefin.org/]

  Once you've created a setup token, paste it here:
  > [base64 token]

  ✓ Claimed access URL. Saved to ~/.local/share/goetta-finance/config.json
    (mode 600, owner-read-only)

[2/4] Storage backend
  Default: DuckDB (recommended)
  Other options: sqlite, jsonl
  Backend [duckdb]: <Enter>
  ✓ Initialized DuckDB at ~/.local/share/goetta-finance/data.duckdb

[3/4] Initial data pull
  Pulling available history from SimpleFIN (up to 90 days)...
  ✓ 3 accounts found: Chase Checking, Chase Savings, Vanguard Brokerage
  ✓ 247 transactions imported
  ⚠ 1 warning: "Bank XYZ only returned 30 days of history"

[4/4] MCP client integration
  Detected: Claude Desktop at ~/Library/Application Support/Claude/
  Add goetta-finance to your MCP servers? [Y/n] y
  ✓ Added to claude_desktop_config.json

  Restart Claude Desktop, then try asking:
    "What were my biggest expenses last month?"
    "Show me a chart of my net worth over the last 60 days."
    "How much did I spend on groceries in February?"

Setup complete. Run `goetta-finance status` any time to check sync health.
```

The wizard must be re-runnable. Each step is idempotent and skippable if already configured.

## 11. Implementation phases

### Phase 1 — MVP core (~1 week)

Goal: a working MCP server with SimpleFIN sync and DuckDB storage.

- [ ] Project scaffolding: `pyproject.toml`, `src/goetta_finance/`, tests
- [ ] Pydantic models for `Account`, `Transaction`, `BalanceSnapshot`, `SyncRun`
- [ ] `SimpleFinClient` with `claim()` and `fetch_chunked()`
- [ ] `DuckDBStore` implementing `FinanceStore`
- [ ] `collect()` function with overlap window and idempotent writes
- [ ] CLI: `init`, `sync`, `status` (skeleton)
- [ ] Config loader (`~/.local/share/goetta-finance/config.json`)
- [ ] Tests for parser, dedup, overlap logic (use the SimpleFIN demo token)

**Done when:** `goetta-finance init` walks through setup end-to-end, `goetta-finance sync` pulls real data, and `goetta-finance status` reports it correctly.

### Phase 2 — MCP server (~3 days)

- [ ] `goetta-finance serve` over stdio
- [ ] Tools: `list_accounts`, `get_transactions`, `account_balance_history`, `sync_now`
- [ ] `sql_query` tool with read-only enforcement
- [ ] Auto-write Claude Desktop config block in `init`
- [ ] End-to-end test using the MCP SDK's test harness

**Done when:** asking Claude "what's my checking balance" works through Claude Desktop, with a real (or demo) SimpleFIN connection.

### Phase 3 — Web dashboard (~1 week)

The dashboard is part of v1 because it's where rich, persistent visualization lives. Without it, the only place to *see* your data is whatever ad-hoc chart Claude draws in a single conversation.

- [ ] `goetta-finance web` command starts FastAPI on `localhost:8765` (configurable port)
- [ ] No auth — localhost-only bind by default. Document risk if user changes the bind address.
- [ ] HTMX-based pages, no SPA build pipeline
- [ ] Built-in views for v1:
  - [ ] Accounts overview — list with balances, last-updated timestamp per account
  - [ ] Net worth over time — Plotly line chart from `balance_snapshots`
  - [ ] Spending by month — Plotly bar, grouped or stacked
  - [ ] Transactions table — sortable, filterable by account/date/text
  - [ ] Sync health — last sync, warnings, error log from `sync_runs`
- [ ] Reads via the same `FinanceStore` interface as the MCP tools — never touches DuckDB directly
- [ ] Render test: open in Chrome/Safari/Firefox, confirm charts are interactive

**Done when:** `goetta-finance web` starts cleanly, all five views render, and the user can identify last month's biggest expense without ever opening Claude.

### Phase 4 — Scheduling & polish (~2 days)

- [x] `goetta-finance daemon` with internal scheduler (default: daily at 6am local). Hosts dashboard + streamable-HTTP MCP at `/api/mcp` + scheduler in one process — also resolves the Windows DuckDB-lock conflict.
- [x] Lazy sync: MCP server triggers `collect()` if `last_sync_time` is older than `GOETTA_FINANCE_LAZY_SYNC_HOURS` (default 6h). Uses a daemon thread (not `asyncio.create_task`) so the sync outlives the request. New `sync_status` MCP tool exposes freshness for Claude.
- [x] Documented systemd / launchd / Windows Task Scheduler snippets in README.
- [x] Better error messages on the Bridge's various failure modes (`BridgeAuthError`, `BridgeRateLimitError`, `BridgeUnavailableError`).

### Phase 5 — v2 features (later)

- [ ] SQLite and JSONL storage backends
- [ ] Pending transaction handling
- [ ] SQLCipher encryption-at-rest option
- [ ] Manual account support (for assets SimpleFIN can't reach: 401k providers, home equity, etc.)
- [ ] Transaction categorization with feedback loop
- [ ] Saved-query system in the dashboard (named SQL queries that render as cards)
- [ ] Revisit MCP-side chart rendering if/when client support lands

### Phase 6 — Goals

User-defined thresholds, evaluated at READ TIME (no stored status, no events table — recategorizing a transaction retroactively changes goal progress, same design as the `transactions_with_category` view). Two kinds in one `goals` table (migration 0008): `spending_cap` (net spending in a category stays under $N per calendar month/year) and `balance` (account balance `at_least`/`at_most` $N, optional `target_date` for pace math).

Design invariants:

- All goal math lives in `src/goetta_finance/goals.py` (`evaluate_goals`); every surface calls it — no per-surface re-derivation.
- Spending-cap totals reuse `query_spending_by_category` (the pie's helper): caps, pie, and monthly bars agree to the cent. Pending transactions count (the pie includes them; a cap is an early-warning device).
- Periods are UTC calendar buckets, matching `date_trunc('month', posted)` in the dashboard.
- Liability accounts evaluate `abs(balance)` — "at_most 2000" on a credit card means "owe under 2000" regardless of sign convention.
- Post-sync breach summary (CLI `sync` + daemon scheduler log) fires only on status `over` — never `at_risk` (pace noise) or unmet `at_least` (normal saving state).

Slices:

- [x] `tools/_serialize.py` extraction (the deferred rule-of-three cleanup)
- [ ] Migration 0008 + `Goal`/`GoalProgress` models + validators + store methods + `goals.py` domain math
- [ ] CLI `goal` group (add-spending / add-balance / list / remove) + post-sync breach lines
- [ ] MCP tools: `list_goals` / `set_goal` / `remove_goal` + `SQL_SCHEMA_HINT` paragraph
- [ ] Dashboard `/goals` page (GET-only; progress bars, status badges)
- [ ] Docs: CLAUDE.md pattern entry, README

## 12. Out-of-scope reminders

If a contributor PR adds any of the following without discussion, push back:

- A web service hosted by us
- A SaaS auth layer
- Mobile companion apps
- Replacement-grade budgeting features (envelope budgeting, budget allocation/rollover, transaction splitting). The goals feature (Phase 6) is the deliberate ceiling: user-defined thresholds evaluated at read time. Anything that needs persisted budget state or period-close semantics belongs in a dedicated budgeting app.
- Cloud sync between devices (let users self-host syncthing if they want it)

## 13. Open questions for first session

Things to decide once during Phase 1 and never revisit:

1. License — MIT by default unless we have a reason otherwise.
2. Package name on PyPI — check availability.
3. Versioning — semver, `0.x` until the data model is stable.
4. Default sync cadence in `daemon` mode — 6am local? Every 6 hours?
5. ~~Where to source Plotly JS for the dashboard — CDN (smaller package) or bundled (offline-friendly, no third-party request from a finance app)?~~ **Resolved (Phase 3):** bundle `plotly-basic.min.js` locally under `src/goetta_finance/web/static/`. The `basic` variant covers the dashboard's chart types (scatter, bar) at ~1MB vs ~3.5MB for full plotly.js. Local-first principle wins over wheel size.
