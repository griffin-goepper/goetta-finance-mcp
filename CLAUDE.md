# CLAUDE.md

This file guides Claude Code (and any other AI assistant) working on this repo. Read it at the start of every session along with [`PROJECT_PLAN.md`](./PROJECT_PLAN.md).

## What this project is

`goetta-finance` is a local-first MCP server that connects SimpleFIN to Claude. Read `PROJECT_PLAN.md` for the full vision, architecture, and phases. This file is about *how to work on the code*.

## Operating principles

1. **PROJECT_PLAN.md is the source of truth.** If the plan and the code disagree, fix the code or update the plan with the user's approval — don't silently diverge.
2. **Always be working on a specific phase.** Ask which phase if it's unclear. Don't start Phase 3 work while Phase 2 has open items.
3. **One concern per PR-equivalent change.** Don't bundle the collector refactor with the SQL-query tool work. Smaller commits are easier to review.
4. **Local-first is sacred.** No telemetry, no auto-update checks, no network calls that aren't for SimpleFIN or loading dashboard JS from a CDN (and even that's an open question — see PROJECT_PLAN.md §13). If you're tempted to add one, stop and ask.
5. **The user's data is sensitive.** Never log access URLs, full account numbers, or transaction descriptions at INFO or above. DEBUG is fine for development but should be off by default.
6. **This project is open-source — apply the stranger test at planning time.** Each slice should ask: would a stranger installing this tomorrow with their own SimpleFIN credentials benefit equally, or am I building for myself? **User-state** (rules, categories, account flags, transaction overrides) lives in the user's DB or user-local config (`prefixes.txt`, `config.json`). **Codebase state** (default seeds, regex patterns the analysis code applies, displayed copy) should be minimal and broadly applicable, with extension paths documented in CUSTOMIZATION.md. Three classes of issue to watch: (a) accidental personal-data leakage — grep at code-review time; (b) me-specific defaults that should be configurable — catch at planning; (c) US-only / bank-specific assumptions baked into schema or display — catch at planning. Surfacing this before merging is cheaper than retrofitting generality later (see migration 0007 for the retrofit cost).

## Code conventions

- **Python 3.11+**, no support for older versions.
- **Type hints everywhere.** `mypy --strict` should pass.
- **Pydantic v2** for any data crossing a boundary (SimpleFIN response, MCP tool args, config file).
- **`decimal.Decimal` for money.** Never `float`. Configure DuckDB to return Decimals.
- **`pathlib.Path` for filesystem.** No raw strings.
- **`zoneinfo` for timezones.** All timestamps stored as UTC, displayed in the user's local TZ.
- **Exceptions:** raise specific subclasses of `GoettaFinanceError`. Catch at the CLI boundary and produce friendly messages.
- **Logging:** `logging` stdlib, structured where useful. Configure a single root logger in `cli.py`.

## Project layout

```
goetta-finance/
├── pyproject.toml
├── README.md
├── PROJECT_PLAN.md
├── CLAUDE.md
├── src/
│   └── goetta_finance/
│       ├── __init__.py
│       ├── __main__.py          # `python -m goetta_finance`
│       ├── cli.py               # typer app, command entry points
│       ├── config.py            # config loading, XDG paths
│       ├── daemon.py            # one-process host: MCP HTTP + dashboard + scheduler
│       ├── errors.py            # GoettaFinanceError hierarchy
│       ├── goals.py             # ALL goal progress/pace math (one home)
│       ├── mcp_config.py        # Claude Desktop/Code registration helpers
│       ├── models.py            # pydantic models
│       ├── simplefin.py         # SimpleFinClient
│       ├── collector.py         # collect() function
│       ├── server.py            # MCP server, tool registration (phase 2)
│       ├── validators.py        # shared CLI/MCP write-surface validation
│       ├── tools/               # one file per MCP tool, for clarity (phase 2)
│       │   ├── __init__.py
│       │   ├── _serialize.py    # shared Decimal/date JSON conversion
│       │   ├── accounts.py
│       │   ├── transactions.py
│       │   ├── balance_history.py
│       │   ├── categorize.py
│       │   ├── goals.py
│       │   ├── spending_by_category.py
│       │   ├── sql_query.py
│       │   ├── sync_now.py
│       │   └── uncategorized.py
│       ├── store/
│       │   ├── __init__.py      # FinanceStore Protocol
│       │   ├── duckdb_store.py  # default backend
│       │   ├── migrations/
│       │   │   └── 0001_init.sql
│       │   ├── sqlite_store.py  # phase 5
│       │   └── jsonl_store.py   # phase 5
│       └── web/                 # phase 3: local dashboard
│           ├── __init__.py
│           ├── app.py           # FastAPI app
│           ├── views.py         # HTMX page handlers
│           ├── charts.py        # Plotly figure builders (dashboard-only)
│           ├── templates/
│           └── static/
└── tests/
    ├── conftest.py
    ├── test_simplefin.py
    ├── test_collector.py
    ├── test_duckdb_store.py
    ├── test_tools.py            # phase 2
    └── fixtures/
        └── simplefin_demo_response.json
```

## Build / test / lint

```bash
# Install dev deps
pip install -e ".[dev]"

# Run tests
pytest                                # all tests
pytest tests/test_collector.py -v     # one file
pytest -k "overlap" -v                # by name

# Lint and type-check
ruff check .
ruff format .
mypy src/goetta_finance

# Run the CLI from source
python -m goetta_finance init
python -m goetta_finance sync
python -m goetta_finance serve      # phase 2
```

A passing PR-equivalent change has: `ruff check` clean, `mypy --strict` clean, all tests green.

## Testing approach

- **Unit tests for parsers and dedup logic.** Use the canned SimpleFIN demo response in `tests/fixtures/`. Don't hit the live Bridge from tests.
- **Integration test for `collect()`.** Run twice, assert idempotency: row counts unchanged on second call.
- **In-memory DuckDB for store tests.** `duckdb.connect(":memory:")`.
- **No mock heroics.** If a test needs five layers of mocks, the design is wrong — surface it.
- **One end-to-end test that boots the MCP server with the official SDK test client.** Slow but catches integration regressions.

## Common patterns to follow

### Adding a new MCP tool

1. New file in `src/goetta_finance/tools/`.
2. Register in `server.py`'s tool list.
3. Tool description should include enough context for Claude to use it well (parameter examples, return shape, when to prefer it over other tools).
4. Test that calls the tool through the MCP SDK test client.

### Adding a new storage backend

1. New file in `src/goetta_finance/store/`.
2. Implement the `FinanceStore` protocol.
3. Add a copy of the existing store test file, parametrized for the new backend.
4. Add a `--backend` option case in `cli.py init`.

### Adding a new SimpleFIN field

1. Update the pydantic `Account` or `Transaction` model.
2. Update the parser in `simplefin.py` to extract it.
3. Update the DuckDB schema with a new migration file `000N_add_<field>.sql`.
4. Update `upsert_*` in `duckdb_store.py`.
5. Update tests.

### Adding a default categorization rule

**Never edit `0004_categorization.sql` after ship.** Migrations run exactly once per DB (gated by `schema_migrations`), so editing 0004 has no effect on existing installations and would silently re-seed deletions on fresh ones. Instead:

1. Create a new migration file `0005_default_rules_expansion.sql` (or higher).
2. Insert into `category_rules` with `is_default = TRUE`, matching the shape of the seed in 0004.
3. The `category default-rules` CLI lists *all* `is_default=TRUE` rows across all migrations, so users see the cumulative seed.

Users who deleted or edited a default rule must not see it silently re-seeded; the migration-stamp convention is the safeguard.

### Migrations that remove or change seeded data — the 0007 lesson

If a migration deletes or modifies rows that were seeded by an earlier migration AND those rows might have been doing real work for existing users, the migration must include a **pre-migration mitigation step** that copies user-affecting seeded rows into user-owned space before the destructive change runs. Otherwise existing users silently regress between the moment the migration lands and the moment they manually re-curate.

Concrete example: migration 0007 deletes ~32 merchant-specific default rules from 0004's seed (`KROGER`, `STARBUCKS`, `SHELL`, etc.). For existing users, those defaults were almost certainly catching real transactions — `Groceries: 28 txns/yr` is mostly KROGER. After 0007 runs, every txn that only matched via a deleted default reverts to `Uncategorized`. The user has to manually re-add the rules they were depending on.

The 0007 rollout required a manual mitigation dance (query the live DB → identify defaults doing real work with no user-added duplicate → emit re-add commands → user runs them BEFORE restarting the daemon). That was acceptable as a one-off because the user accepted the trade-off in plan review. **Don't ship that shape again.** When the next migration changes seeded data:

1. Query at-migration time: identify rows that are currently catching real work AND don't have a user-owned equivalent.
2. Copy those rows to user-owned space (e.g. flip `is_default` from TRUE to FALSE, or insert a parallel user-tagged row) WITHIN the migration so users transition transparently.
3. THEN perform the destructive change.

This matches the [[feedback_pre_fix_audit_for_bug_pinning_tests]] discipline applied to data migrations: surface and handle the user-visible-effect before the schema change, not after.

### Goal math has one home

All goal progress/status/pace is computed in `src/goetta_finance/goals.py` (`evaluate_goals`); CLI `goal list`, MCP `list_goals`, the dashboard `/goals` page, and the post-sync breach warnings all call it — never re-derive spending or pace math per surface. Display wording likewise: `describe_goal` / `describe_progress` feed the CLI and dashboard (MCP returns raw fields). Spending-cap totals reuse `query_spending_by_category` (the pie's helper) so caps, pie, and monthly bars agree to the cent; pending transactions count, hidden accounts are excluded, periods are UTC calendar buckets. Balance goals on `is_liability` accounts evaluate `abs(balance)` (amount owed). Progress is never stored — no status columns, no events table; read-time evaluation is the feature (same retroactivity contract as the categorization view). Goal writes are gated by the shared validators in `validators.py` on both the CLI and MCP surfaces. Schema changes to `goals` must update `SQL_SCHEMA_HINT` and its marker tests, and `delete_account` refuses accounts that goals reference — extend that guard for any new FK.

### Adding a boolean flag (the `is_X` pattern)

There are now five `is_X BOOLEAN DEFAULT` columns across two tables, following one consistent shape: `is_manual` (0002), `is_liability` (0003), `is_hidden` (0005) on `accounts`; `is_default` (0004), `is_spending` (0006) on `categories`. When you need another, follow the template:

1. New migration file `000N_<short_name>.sql`. `ALTER TABLE <table> ADD COLUMN is_X BOOLEAN DEFAULT <FALSE|TRUE>;` plus an `UPDATE ... SET is_X = ... WHERE ...` if specific existing rows should flip (e.g., 0006 flips `Transfers` and `Income` to FALSE). DuckDB ALTER TABLE doesn't support inline NOT NULL, so always use DEFAULT; the Python layer reads NULL as False.
2. Add the field to the pydantic model (`Account` or `Category`) with the same default as the SQL DEFAULT.
3. Extend the corresponding `_row_to_<model>` mapper in `duckdb_store.py` to read the new column.
4. If the flag is **account-user-owned** (like `is_liability` / `is_hidden`), add the column name to `_USER_OWNED_COLUMNS` so the upsert preserves it across syncs. Forgetting this is the silent-clobber bug from the hide-accounts slice — pinned by `test_user_owned_flags_survive_sync`.
5. Add a setter method (`set_<table>_<flag>`) mirroring `set_account_liability` / `set_category_spending`. Use the case-insensitive lookup pattern for category names (`WHERE lower(name) = lower(?)`).
6. Update `FinanceStore` Protocol in `store/__init__.py`.
7. Add a CLI subcommand to flip the flag — mirror the `set-liability` / `set-spending` shape (typed `_parse_bool`, friendly errors via `_suggest_category` if relevant).
8. Update `SQL_SCHEMA_HINT` with a paragraph naming the flag, what it means, what filters on it by default, and what the opt-in is. Extend `test_schema_hint_mentions_categorization_tables` and `test_schema_hint_communicates_categorization_semantics` markers as needed.
9. Tests: migration-applied marker, default round-trip, set-flag round-trip, set-flag-raises-on-unknown, default read paths filter it (or include it) as designed.

## Things to avoid

- **Don't write your own JSON parser for SimpleFIN responses.** Use pydantic.
- **Don't pass raw dicts between modules.** Use pydantic models. Dicts hide schema rot.
- **Don't add a new dependency for something the stdlib does well.** `pathlib`, `datetime`, `decimal`, `logging`, `sqlite3` are all fine.
- **Don't add an MCP tool that wraps a single SQL query.** Use `sql_query` instead.
- **Don't add an MCP tool that returns a chart, HTML, or any rendered visualization.** This was tried and cut on purpose — MCP clients today don't render arbitrary HTML/widgets from tool results reliably (image content goes into a collapsed accordion on claude.ai; Claude Desktop image rendering is unreliable). The pattern: MCP tools return *data*, Claude renders inline charts as artifacts from that data, and persistent interactive charts live in the web dashboard. See PROJECT_PLAN.md §7 "Why there's no `chart` tool" before reopening this.
- **Don't catch broad `Exception`.** Be specific. Let unknown failures crash loudly in dev.
- **Don't add print statements.** Use the logger.
- **Don't fetch SimpleFIN data inside an MCP tool synchronously.** That blocks Claude's response. Use the lazy-sync pattern: check `last_sync_time()`, return stale data with a note, and trigger a background sync.
- **`goetta-finance serve` and `goetta-finance web` cannot run simultaneously on Windows.** DuckDB takes an exclusive OS file lock on the DB even when one of the handles is opened `read_only=True`. The web CLI detects this at startup and prints a friendly hint. On macOS/Linux DuckDB uses advisory POSIX locks so concurrent read-only + read-write may work; not relied upon. **Use `goetta-finance daemon` to host both surfaces (dashboard + MCP HTTP) from one process** — it's the supported way to run them concurrently. The separate `serve` and `web` commands remain for users who only need one at a time.
- **Never hard-kill the daemon (or any process holding the DuckDB write handle) — use the stop file.** Create `daemon.stop` next to `data.duckdb`; the daemon watches for it and shuts down gracefully within seconds (clean store close → checkpoint). A force-kill freezes whatever is in the WAL, and DuckDB's WAL replay fails on `CREATE OR REPLACE VIEW` entries (`GetDefaultDatabase with no default database set` — internal assertion): a kill right after a migration leaves the database unopenable until the WAL is manually moved aside. This happened live on 2026-07-05 with migration 0009. Two safeguards now exist — `init()` checkpoints after applying migrations, and the stop-file watch — but the rule stands: prefer the stop file; if you must kill, run a clean CLI open/close first to force a checkpoint. The stop file is deliberately a file and not an HTTP endpoint: `POST /shutdown` on an unauthenticated localhost port would be reachable by any local process and any web page via no-cors `fetch` — a drive-by denial of service.
- **Don't "fix" `claude_desktop_config_path()` for the Microsoft Store build of Claude Desktop without testing against a live Store install.** The MSIX-packaged Claude Desktop (installed via the Windows Store, executable lives under `C:\Program Files\WindowsApps\Claude_*\`) reads config from a sandboxed `%LOCALAPPDATA%\Packages\Claude_<id>\` path — not the `%APPDATA%\Claude\` that the wizard currently writes. The exact sandboxed location is undocumented by Anthropic and the package family name (`pzs8sxrjxfjjc` observed) may not be stable across versions. `init`'s step [4/4] now prefers Claude Code (`claude mcp add`) which has no path-discovery problem and works for everyone. The README documents this gap in the "Claude clients" table. If you want to support MSIX directly, verify the path against a real Store install first and gate the detection on `sys.platform == "win32"` + the actual existence of the WindowsApps Claude executable — don't infer it from `%LOCALAPPDATA%\Packages\` layout alone.
- **Don't add a user-controllable boolean flag on accounts without adding it to `_USER_OWNED_COLUMNS` in `duckdb_store.py` and verifying it's excluded from the `ON CONFLICT SET` clause in `upsert_accounts`.** The clobber bug fixed in migration 0005 (hide-accounts slice) was silent — `is_liability` was being silently reset to `False` on every sync because the SimpleFIN parser produces the natural default and the SET clause enumerated the column. The fix is structural: `_SIMPLEFIN_SOURCED_COLUMNS` drives the SET clause programmatically; `_USER_OWNED_COLUMNS` is the explicit other category. A new flag either belongs to SimpleFIN's response or it doesn't — make the choice deliberately. The regression test `test_user_owned_flags_survive_sync` pins the class-of-bug, not just current instances. Don't add a new user-controlled flag without extending that test, and don't bypass the structural pattern by inlining a column name in the SET clause.
- **Don't add a `category_id` column to `transactions`.** Resolution is read-time via the `transactions_with_category` view on purpose — adding or editing rules / overrides applies retroactively without any backfill. A write-time column would silently break that contract, force a backfill on every rule change, and defeat the "rules-and-overrides" design of the categorization slice. The pydantic `Transaction` model does NOT carry a `category` field for the same reason; only `serialize_transaction` (the dict shape sent over MCP) attaches a resolved category.
- **`category_rules.pattern` is an MCP-reachable write surface.** The same prompt-injection-via-memo threat model from `sql_query` extends here: a third-party-controlled transaction memo could trick Claude into calling the `add_category_rule` MCP tool (or running `goetta-finance category set-rule <category> --pattern <evil-regex>`). The pattern then runs against every future view read (`transactions_with_category`), including from the dashboard and `spending_by_category`. **The load-bearing runtime defense is the existing `query_sql` statement-timeout watchdog** (`GOETTA_FINANCE_SQL_TIMEOUT_SECONDS`, default 30s) — DuckDB's `regexp_matches` evaluation is bounded by that timeout when the view is queried through `sql_query`. Write-time, both surfaces run the IDENTICAL best-effort filter — `validators.validate_rule_pattern` (extracted from cli.py so the CLI and the MCP tool can't drift): `re.compile()` for syntax errors plus a heuristic shape check for nested quantifiers like `(X+)+` and large counted repetitions like `(.*a){25}`. We do NOT use a runtime regex timeout (e.g. a daemon thread with `Event.wait(1.0)`) — CPython's `re` engine does NOT release the GIL during matching, so the wait can never preempt a long-running pattern (measured: `(a+)+$` against a 30-char sentinel pinned the GIL for 49s while the wait was blocked). Don't add a "fast-path" rule-creation API that skips the shared validator, and don't claim the validator is more than best-effort — the runtime watchdog is what matters.
- **Don't simplify `sql_query`'s defense in depth.** It has *three* layers and all of them are intentional. Threat model is prompt injection: transaction descriptions, memos, and payee names come from free-form text that third parties control (Venmo/Zelle memos, ACH descriptions, card-processor strings); Claude reads that text and can be tricked into forwarding it to `sql_query`.

  The three layers, outermost first:

  1. **Pre-flight prefix whitelist** (`SELECT/WITH/SHOW/DESCRIBE`) — fast-fail with a friendly error before SQL hits DuckDB. `explain` is deliberately excluded, see below.
  2. **`BEGIN TRANSACTION READ ONLY`** wrapping execution — DuckDB's storage layer refuses in-database mutations smuggled through whitelisted prefixes (e.g., a `WITH` CTE that wraps a DELETE).
  3. **`duckdb.connect(path, config={'enable_external_access': 'false'})`** — set at connect time and **immutable** (DuckDB rejects `SET enable_external_access = true` at runtime with "Cannot enable external access while database is running"). Blocks every code path that touches the OS filesystem or network: `read_csv`/`read_parquet`/`read_blob` (information disclosure), `COPY ... TO 'file'` (exfiltration), `httpfs` URLs (beacon-out). Errors surface as DuckDB `PermissionException: file system operations are disabled by configuration`.

  Concrete payloads each layer is load-bearing for:

  - **`WITH cte AS (SELECT 1) DELETE FROM accounts`** — `WITH` is whitelisted (legitimate analytical prefix), so this reaches DuckDB. The read-only transaction refuses it. Pinned by `tests/test_duckdb_store.py::test_query_sql_rejects_with_cte_delete`.
  - **`EXPLAIN ANALYZE INSERT INTO accounts ...`** — `EXPLAIN ANALYZE` *executes* the inner statement, and the read-only transaction would refuse the INSERT, but we don't even let it get there: `explain` is excluded from the whitelist.
  - **`EXPLAIN ANALYZE COPY (SELECT 1) TO '/tmp/leak.csv'`** — this is the reason `explain` is excluded. `COPY ... TO` writes the *filesystem*, not the database, so the read-only transaction does NOT block it. Now caught at **two layers**: prefix whitelist (no `explain`) and `enable_external_access=false`. Pinned by `::test_query_sql_rejects_explain_analyze_copy_to_file`.
  - **`SELECT * FROM read_csv('~/.ssh/id_rsa')`** — passes the prefix whitelist (`SELECT` is legitimate) and the read-only transaction (no DB mutation), but `enable_external_access=false` refuses the file open. Pinned by `::test_query_sql_blocks_external_file_read` and `::test_query_sql_blocks_http_url_read` (the network-exfiltration variant).

  Do not re-add `explain` to the whitelist without first re-reading the `EXPLAIN ANALYZE COPY` case. Do not relax `enable_external_access`: it's immutable for a reason, and the regression tests pin both filesystem-read and HTTP cases. A separate `duckdb.connect(path, read_only=True)` handle would be a stronger form of layer 2 but DuckDB refuses two handles to the same file from one process when configs differ — that's why we use the transaction wrapper. If you find yourself "cleaning up" the apparent redundancy, stop and re-read this paragraph.

  **What's still unbounded:** in-database resource exhaustion (`SELECT * FROM generate_series(...)` with huge ranges, recursive CTEs, etc.) — denial-of-service via memory/CPU. Not yet bounded; mitigation would be DuckDB's `memory_limit` / `threads` configs and a query timeout. Out of scope for the current threat model (single-user local-only tool).

## Working with the user

The repo owner is technical and prefers terse, accurate responses. When you finish a phase or a tricky piece:

- Summarize what changed in 3–5 bullets.
- Surface any decisions you made that weren't in the plan, especially data-model or interface choices.
- Note anything you skipped or stubbed (and why).
- Ask for direction on the next slice rather than steamrolling into the following phase.

## When in doubt

1. Re-read PROJECT_PLAN.md.
2. Pick the simpler option.
3. Ask the user before adding complexity, dependencies, or new files outside the layout above.
