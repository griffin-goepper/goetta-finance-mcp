# Security audit — 2026-05

## Scope

- **Scanners:** bandit (1.9.4), pip-audit, gitleaks v8.30.1 (full git history), ruff S rules.
- **Manual review:**
  - SimpleFIN access URL handling — every code path that reads, stores, or could log the credential.
  - HTTP exposure — bind defaults, CORS posture, CSRF, Origin / DNS-rebinding on `/api/mcp`.
  - DuckDB resource-limit posture on the agent-callable read-only `sql_query` path.

This document is the *narrative summary* of the audit. Raw scanner output (`bandit-output.json`, `pip-audit-output.json`, `gitleaks-report.json`) is `.gitignore`'d on purpose — see the project memory file `reference_audit_artifact_policy.md` for the rationale.

## Findings

### CRITICAL / HIGH

None.

### MEDIUM — remediated in this audit pass

1. **setuptools 65.5.0 in the venv had 3 CVEs.** `PYSEC-2022-43012`, `PYSEC-2025-49`, `CVE-2024-6345` — all in setuptools' package-index / URL-fetching code. setuptools is not a runtime dep; it's only invoked at install time. Not reachable from the daemon's runtime surfaces, but cheap to fix.
   **Remediation:** added `setuptools>=78.1.1` to `[build-system].requires` in `pyproject.toml`. Local venv upgrade is the user's slice (`pip install --upgrade "setuptools>=78.1.1"`).

2. **Three SQL strings in `src/goetta_finance/web/aggregations.py` interpolated datetime/int values via f-string instead of parameter binding.** Internally-generated values, not user input — not exploitable today. But this is the kind of defense-in-depth gap that breaks the moment someone refactors the call site to plumb user input through. The whole point of `query_sql`'s 3-layer hardening is that the SQL path is the prompt-injection target; the dashboard's internal SQL paths should match that posture.
   **Remediation:** extended `DuckDBStore.query_sql` to accept optional positional params; converted all three call sites in `aggregations.py` to bind via `?` placeholders. New regression test `test_query_sql_params_binding_for_internal_callers`.

### MEDIUM — remediated proactively (Phase 2 manual hardening)

3. **No SQL resource limits on the DuckDB connection.** `CLAUDE.md` flagged this explicitly as the remaining gap in `sql_query`'s defense in depth: "in-database resource exhaustion (`SELECT * FROM generate_series(...)`)... not yet bounded." Reachable from any agent that calls the `sql_query` MCP tool — i.e., the prompt-injection attack surface.
   **Remediation:**
   - Added `memory_limit=512MB` and `threads=2` to the connect-time `config` in `DuckDBStore.conn`.
   - Added a `threading.Timer` watchdog around `query_sql` that calls `conn.interrupt()` after `GOETTA_FINANCE_SQL_TIMEOUT_SECONDS` (default 30s). DuckDB has no built-in `statement_timeout`; the watchdog is the safe inline approach (cancels in `finally`, no leaked timers).
   - Three new regression tests: `test_query_sql_memory_limit_bounds_huge_intermediate`, `test_query_sql_timeout_interrupts_long_running`, `test_query_sql_normal_query_unaffected_by_resource_limits`.

### LOW

None.

### INFORMATIONAL — true positives, banked rationale

These were flagged, audited, and confirmed not to be real risks in this codebase. Suppressions are inline (`# noqa: S...` with rationale) or in `pyproject.toml`'s `per-file-ignores` for test code that exercises adversarial patterns by design. A *new* occurrence of these patterns will still get flagged.

- **`mcp_config.py:178, 202` — `subprocess.run` (bandit B603, ruff S603).** We shell out to `claude mcp add` / `claude mcp remove`. Args are a list (no `shell=True`), values are typer-validated CLI flags or constants (`SERVER_KEY`, scope, transport). Correct pattern; bandit/ruff flag any subprocess for human review.
- **`duckdb_store.py:216, 341` — f-string SQL (bandit B608, ruff S608).** `IN ({placeholders})` interpolates only `?` markers, with actual ids bound via params. `get_transactions` interpolates a fixed allow-list of column predicates and `LIMIT {int(...)}` — both pre-validated, both with values bound separately.
- **`tests/test_collector.py:27`, `tests/test_duckdb_store.py:268, 272` — S608/S105 in tests.** Test helpers and regression tests construct adversarial SQL / use sentinel "secret"-named strings on purpose. **Note for future audits:** if `tests/test_duckdb_store.py:268`'s `secret = ...` re-flags despite the per-file-ignore, rename to `sentinel_value` rather than re-suppressing. Cheap re-triage prevention.

### Operational hygiene

- **Orphan `~oetta_finance-0.1.0.dist-info/` in `.venv/Lib/site-packages/`** — leftover from a failed editable-package reinstall (this Claude Code session held the lock on `goetta-finance.exe`). Cleaned during this audit; a clean `pip install -e ".[dev]"` will fully reseat the package once the user can release the .exe lock (i.e., next time no MCP-stdio Claude session is running against the file).
- **HTTP exposure manual checklist:**
  ```
  [✓] cli.py:daemon()/web()  default --host = 127.0.0.1; warning fires for non-loopback
  [✓] web/app.py             no CORS middleware (documented as intentional, audit comment added)
  [✓] dashboard routes       all GETs; HTMX uses querystrings; no CSRF surface
  [✓] /api/mcp               FastMCP auto-enables TransportSecurity DNS-rebinding
                             protection for localhost binds (allowed_hosts +
                             allowed_origins restricted to 127.0.0.1/localhost/::1)
                             — see mcp.server.fastmcp.server:178-183
  ```
- **SimpleFIN access URL handling checklist:**
  ```
  [✓] config.py:20-26       access_url field documented as sensitive
  [✓] config.py:79-81       mode 0600 on POSIX; OSError-suppressed on Windows
  [✓] simplefin.py:49-63    _split_access_url strips userinfo before storing as
                            self._base (netloc = host on line 61, base = urlunsplit
                            on line 62, return at 63); credentials live in the
                            self._auth tuple — unpacked at simplefin.py:118 and
                            stored separately at simplefin.py:120
  [✓] simplefin.py:203-207  the only logger.info call logs date windows, not URL
  [✓] web templates         no `access_url` reference in src/goetta_finance/web/
  [✓] .gitignore:31, 29-30  config.json + *.duckdb ignored
  [✓] gitleaks              full-history scan: "no leaks found"
  ```

## Outstanding items

None at class-of-issue level. Future-audit prompts:

- **In-database resource exhaustion is now bounded** (memory_limit, threads, statement-timeout watchdog), but DuckDB ships with new builtin functions every release — any future function that allocates unbounded memory or runs unbounded CPU outside the memory_limit (e.g., custom UDFs, ML extensions) should re-trigger this section.
- **DNS-rebinding protection is library-default** in FastMCP. If we ever pin to an older FastMCP or bind to a non-loopback host (e.g., for a future "expose the daemon to my home LAN" feature), `transport_security` needs to be set explicitly.
- **setuptools constraint is build-system-only.** If we add a runtime dep that pins setuptools indirectly, the constraint will move with it; re-audit at that time.

## Process

The repeatable audit workflow is documented in `README.md` §"Security tooling". Pre-commit hooks (`.pre-commit-config.yaml`) catch the easy stuff on every commit. The CI workflow at `.github/workflows/security.yml` runs the full scanner set on every PR.
