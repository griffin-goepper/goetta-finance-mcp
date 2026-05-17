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
│       ├── errors.py            # GoettaFinanceError hierarchy
│       ├── models.py            # pydantic models
│       ├── simplefin.py         # SimpleFinClient
│       ├── collector.py         # collect() function
│       ├── server.py            # MCP server, tool registration (phase 2)
│       ├── tools/               # one file per MCP tool, for clarity (phase 2)
│       │   ├── __init__.py
│       │   ├── accounts.py
│       │   ├── transactions.py
│       │   ├── balance_history.py
│       │   ├── sql_query.py
│       │   └── sync_now.py
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
- **Don't "fix" `claude_desktop_config_path()` for the Microsoft Store build of Claude Desktop without testing against a live Store install.** The MSIX-packaged Claude Desktop (installed via the Windows Store, executable lives under `C:\Program Files\WindowsApps\Claude_*\`) reads config from a sandboxed `%LOCALAPPDATA%\Packages\Claude_<id>\` path — not the `%APPDATA%\Claude\` that the wizard currently writes. The exact sandboxed location is undocumented by Anthropic and the package family name (`pzs8sxrjxfjjc` observed) may not be stable across versions. `init`'s step [4/4] now prefers Claude Code (`claude mcp add`) which has no path-discovery problem and works for everyone. The README documents this gap in the "Claude clients" table. If you want to support MSIX directly, verify the path against a real Store install first and gate the detection on `sys.platform == "win32"` + the actual existence of the WindowsApps Claude executable — don't infer it from `%LOCALAPPDATA%\Packages\` layout alone.
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
