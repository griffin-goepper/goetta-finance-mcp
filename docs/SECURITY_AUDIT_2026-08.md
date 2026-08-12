# Security audit — 2026-08

Follow-up to [`SECURITY_AUDIT_2026-05.md`](./SECURITY_AUDIT_2026-05.md). Same policy: this is the
*narrative summary*; raw scanner output stays `.gitignore`'d.

## Scope

- **Scanners:** bandit 1.9.4 (`-c pyproject.toml`, src only), pip-audit, gitleaks v8.30.1 (full
  history), ruff `S` rules. Full test suite (723 passed, 3 skipped).
- **Manual review — prioritized on what landed since May:** backup/restore (`backup.py`, the
  restore/reconcile paths in `duckdb_store.py`), the `/api/v1` JSON surface (`web/api.py`), the
  `--dash-dir` SPA mount, `--host` exposure, migrations 0011–0016.
- **Re-verified from the May pass:** `sql_query`'s three layers, shared rule-pattern validation,
  access-URL handling, sensitive-value logging, subprocess use.

Two findings were confirmed with a working proof of concept rather than by reading. Both PoCs ran
against throwaway databases in a temp directory; the live database and the running daemon were
never touched.

## Severity is graded against the stated threat model

`SECURITY.md` declares this single-user and local-only, and the severities below are graded against
that — not against how impressive the exploit is. That distinction moved one finding after review:

**Finding 1 was initially graded HIGH and is now MEDIUM.** A working PoC drove the first grade. The
question that should have driven it: what does injection grant *beyond* what an attacker who can
already write the archive has? Restore trusts archive content by design — anyone who can tamper with
the file can simply write a well-formed archive containing whatever balances and transactions they
want. Injection adds arbitrary DDL against tables outside the restore set, but there is no escape
from the database (`enable_external_access=false` holds) and it is the user's own data either way.
Real, worth fixing, not urgent.

**Finding 2 is the one that "it's only local" does not soften — it arguably sharpens it.** DNS
rebinding exists specifically to reach services bound to loopback from the public web. "We only bind
127.0.0.1" is the assumption it defeats, not a mitigation. It needs no local foothold, no LAN access,
and no malware: a page the user visits is the entire attack.

Most of the rest genuinely are low-stakes for a single-user local install: they need an attacker who
already has local file access or code execution as the user, at which point the game is over anyway
(the backup archives alone are plaintext JSON Lines of the full transaction history).

**One exposure that outranks several findings below and is not a bug:** backup archives are
unencrypted financial history, written by design into a cloud-synced folder. `access_url` is
redacted, so no live credential leaves — but the transaction data rests entirely on the security of
the user's cloud account. A deliberate trade-off, recorded here because it dominates findings 5 and 6
in practice.

## Findings

### MEDIUM (initially graded HIGH — see the re-grade above)

1. **A tampered backup archive achieves arbitrary SQL execution during restore.** *(open)*
   `DuckDBStore.restore_table` (`duckdb_store.py:691,696`) and `reconcile_table`
   (`duckdb_store.py:621,629,637,645`) build SQL by interpolating `snapshot.columns`. Those column
   names come from the archive's own JSONL header (`backup.py:_jsonl_to_snapshot`, reading
   `header["columns"]`), and `f'"{c}"'` quotes them without escaping an embedded `"`. The table
   *name* is safe — restore only reads `data/<table>.jsonl` for names in `RESTORE_ORDER` and passes
   the loop's own literal — but the columns are attacker-controlled file content.

   **Confirmed by PoC, deliberately not reproduced here.** A crafted column name in one
   `data/*.jsonl` header (with the manifest's sha256 recomputed to match, which the archive format
   makes trivial) closes the quoted identifier early and appends statements of its own. DuckDB's
   `execute()` runs multiple statements, which is what turns the quoting flaw into execution. It
   reproduces on both the `restore_table` and `reconcile_table` paths.

   Per the audit-artifact policy (`docs/SECURITY_AUDIT_2026-05.md` §Scope), an **unfixed** finding
   in self-hosted software is documented at class-of-issue level — enough for a maintainer to write
   the fix and the regression test, not a working payload for users who cannot patch on a fast
   cycle. The exact reproduction is recorded outside the repo and goes in here once the fix lands,
   the way 2026-05's remediated findings are written up in full.

   **Reachability.** `goetta-finance backup restore <archive>` is the obvious path, but note that
   `verify_backup` calls `restore_backup` into a temp database — so `backup verify`, and the
   automatic post-write verification inside `create_backup`, execute archive content too.

   **What bounds the blast radius:** the restore connection still carries
   `enable_external_access=false`, so the injected SQL cannot read files, `COPY ... TO` anything, or
   reach the network. The impact is arbitrary manipulation of the database being restored — silently
   rewritten balances or transactions, planted category rules, dropped tables — not RCE or
   exfiltration.

   **Why it matters despite needing a tampered file:** the documented workflow is "point the
   destination at a folder your cloud client already syncs." Archives are designed to live in
   OneDrive/Dropbox/Drive, which is a realistic tamper surface (shared folder, compromised cloud
   account, a stale archive restored from someone else's copy), and an archive is also the natural
   thing to hand someone for support.

   **Suggested fix:** validate `snapshot.columns` against the target table's real columns
   (`DESCRIBE`, which both paths already have access to) and reject unknown names before building
   any statement. Identifiers should never come from file content unvalidated. This also turns a
   confusing parser error into a clear "archive does not match this schema" message.

### MEDIUM

2. **`/api/v1` and the HTML dashboard did not validate the `Host` header — DNS rebinding read
   everything.** ✅ **FIXED in this pass.** `build_app` installs no middleware. **Confirmed by probe:** every endpoint tested
   returned a full 200 with `Host: evil.attacker.example:8765` —
   `/api/v1/summary`, `/api/v1/accounts`, `/api/v1/categories`, and `/`.

   The asymmetry is the tell. The same probe against `/api/mcp` returned **421 Invalid Host header**
   and **403 Invalid Origin header** — FastMCP's `transport_security` doing exactly what the May
   audit's checklist credited it with. So the tool-call surface is protected by a library default
   while the hand-rolled JSON API returning the same financial data has nothing in front of it.

   CORS is not the control here: there is no `Access-Control-Allow-Origin` header, so a plain
   cross-origin `fetch` cannot read the response. DNS rebinding is the bypass — after the attacker's
   domain re-resolves to `127.0.0.1`, the browser treats the daemon as same-origin and no CORS check
   applies. The default port (8765) is fixed and known. `/api/v1` makes this materially worse than
   it was in May: it is clean, stable JSON that a drive-by page can parse and exfiltrate directly,
   where before an attacker had to scrape HTML.

   **Remediation.** `HostAllowlistMiddleware` in `web/app.py`, added as the outermost middleware so
   a forged `Host` never reaches routing, a mounted sub-app, or `StaticFiles`. The allowlist comes
   from `trusted_hosts_for(bind_host)`: loopback names always, plus the `--host` literal when the
   user opts into a non-loopback bind, so a deliberate Tailscale/LAN bind keeps working. Rejections
   answer **421**, matching what `/api/mcp` already returns. `run_daemon` derives it from its bind
   address; the `web` command does the same.

   *Not Starlette's `TrustedHostMiddleware`*, for a concrete reason: it computes the hostname as
   `host.split(":")[0]`, which yields `"["` for an IPv6 literal like `[::1]:8765` — a `--host ::1`
   bind could never be allowlisted and would 400 on every request. The local `_hostname` parses the
   bracketed form. (Starlette's `TestClient` has the identical bug in its transport, which is why
   the IPv6 test sets the header explicitly instead of using `base_url`.)

   **Wildcard binds are the honest gap.** `--host 0.0.0.0` / `::` cannot enumerate the names that
   legitimately reach it, so `trusted_hosts_for` returns `("*",)` and the check switches off. The CLI
   now prints a second warning saying so, and recommends binding a concrete address. Anyone doing
   this has already accepted "anyone on this network can read your finances."

   Six regression tests in `tests/test_web_api.py`, including one that pins the pre-fix bug directly
   (forged `Host` against `/api/v1/*`, the HTML pages, `/static`, and `/dash`). The nine existing
   `TestClient` sites now drive a loopback `base_url` so the suite exercises the real default posture
   rather than an app with the check disabled.

   **Follow-up 2026-08-12 — the fix broke a legitimate deployment shape.** A reverse proxy in front
   of a loopback bind is invisible to `trusted_hosts_for`: `tailscale serve` forwards the request to
   `127.0.0.1:8765` but preserves the tailnet hostname in `Host`, so every phone request to
   `https://<host>.<tailnet>.ts.net/dash/` started answering 421 the morning after this landed. The
   deployment was not misconfigured and no bind-derived allowlist can cover it — the name lives at
   the proxy. Remediation: `trusted_hosts_for(bind_host, extra_hosts)` plus a repeatable
   `--allow-host` on `daemon` and `web` (port tolerated, hostname compared, loopback always kept).
   Opt-in by name rather than a heuristic: trusting `X-Forwarded-Host`, or trusting any request whose
   peer is loopback, would hand the bypass straight back — a rebound request also arrives on loopback,
   which is the entire premise of finding 2. `--allow-host '*'` is accepted and warns.

3. **36 known vulnerabilities across 9 packages in the local venv, several runtime-reachable.**
   ⚠️ **PARTIALLY FIXED** — the reachable chain is closed; the unreachable remainder is left, see
   "Remediation" at the end of this finding.

   pip-audit is clean in CI and dirty locally, which is the whole finding — see "Why CI did not
   catch this" below. Reachability, honestly graded:

   | Package | Version | Reachable? | Notes |
   | --- | --- | --- | --- |
   | `mcp` | 1.27.1 | **Yes** | PYSEC-2026-3482: SSE/Streamable-HTTP transports routed requests to a session by session id without verifying the same principal created it. This is the transport the daemon serves at `/api/mcp`. Fix: 1.27.2. |
   | `starlette` | 1.0.0 | **Yes** | PYSEC-2026-2281: `StaticFiles` on Windows is SSRF-able via a UNC path (`\\attacker.com\share`) — `os.path.realpath` opens an outbound SMB connection before rejecting the path, exposing NTLMv2 credentials for offline cracking. This deployment is Windows and mounts `StaticFiles` twice (`/static`, and `/dash` with `--dash-dir`). Fix: 1.1.0. Also PYSEC-2026-161/248 (`Host`/path confusion in `request.url`) and PYSEC-2026-249. I did **not** attempt to trigger the SMB callback — reporting from the advisory plus the confirmed mount points. |
   | `python-multipart` | 0.0.28 | Unlikely | Form-body DoS. No route accepts form data; FastAPI pulls it in regardless. |
   | `pyjwt`, `cryptography`, `pydantic-settings` | — | Unlikely | Transitive via `mcp`'s auth features, which are not enabled (`auth=None`). `cryptography` also ships a statically linked OpenSSL with its own advisory. |
   | `setuptools` | 65.5.0 | Build-time only | **Still unremediated from the May audit.** `pyproject.toml` carries the `>=78.1.1` build-system pin, but the local venv was never upgraded — that was left as "the user's slice" in May and has not happened. |

   **Why CI did not catch this.** `.github/workflows/security.yml` runs `pip install -e ".[dev]"` on
   `ubuntu-latest`, i.e. it audits a *freshly resolved* dependency set on Linux. It never sees the
   long-lived local venv the daemon actually runs from, and it could not surface a Windows-only
   `StaticFiles` issue in any case. CI green means the repo is fine; it says nothing about the
   running deployment.

   **Second-order gap:** `pyproject.toml` declares no security floors —
   `mcp>=1.2`, `fastapi>=0.115`, `uvicorn>=0.30` all permit the vulnerable versions. The
   `setuptools>=78.1.1` build-system pin is the existing precedent for doing this deliberately.

   **Remediation.** Upgraded the venv to `starlette` 1.6.0, `mcp` 1.29.0, `setuptools` 84.0.0 — all
   three now clear in `pip-audit`. Added floors to `pyproject.toml` so the fix survives a fresh
   install: `mcp>=1.28.1,<2` and a direct `starlette>=1.1.0` (the same
   floor-a-transitive-dep pattern the `setuptools` build-system pin already established).

   **The upgrade surfaced a live breakage worth recording.** `mcp` resolves to **2.0.0**, which
   removed `mcp.server.fastmcp` — the module both `server.py` and `web/app.py` import. With the old
   `mcp>=1.2` constraint, `pip install -e .` today produces an install that fails at import, and CI
   would have failed on the next dependency refresh for reasons unrelated to any code change. Hence
   the `<2` ceiling: lifting it is a port to the 2.x API, not a version bump. This was found only
   because the upgrade was actually run and the suite re-run against it.

   **Deliberately left:** `cryptography`, `pyjwt`, `pydantic-settings` (transitive via `mcp`'s auth
   features, which are not enabled), `msgpack`, `python-multipart` (no route accepts form data), and
   `pip` itself. All graded unreachable above. They are noise in `pip-audit` output, which is its own
   small cost — a future pass may want an `ignore-vuln` list with per-entry rationale so a genuinely
   new finding is not lost among them.

### LOW

4. **`json.dumps` piped through `| safe` into an inline `<script>` block.** `views.py:131,146,170`
   serializes Plotly figures and three templates interpolate them inside `<script>` tags
   (`net_worth.html:16-17`, `spending.html:16-17`, `spending_by_category.html:13-14`). `json.dumps`
   does not escape `<`, `>`, or `/`, so a string containing `</script>` terminates the script
   element and everything after it parses as HTML.

   **Not exploitable today.** The only user-controlled string reaching a figure is the category name
   on the pie chart (`charts.py:64`), and category creation is CLI-only — there is deliberately no
   `add_category` MCP tool, and `categorize_transaction` / `add_category_rule` both require an
   existing category. So the prompt-injection chain does not close: an injected memo cannot get
   Claude to mint a category named `</script><img src=x onerror=...>`.

   It is still unsafe-by-construction, and it is the same shape as the May audit's finding #2
   (internally-generated values in f-string SQL — "not exploitable today, but breaks the moment
   someone refactors the call site"). If an `add_category` MCP tool is ever added, this becomes
   stored XSS same-origin with `/api/v1` and `/api/mcp`. Cheap fix: escape `<`, `>`, `&` to their
   `\uXXXX` forms at serialization time.

5. **`save_config` writes the access URL before tightening permissions.** `config.py:196-209` opens
   and writes `config.json`, then `os.chmod`s to `0600`. On POSIX the file exists at the umask
   default (typically `0644`) with the credential already in it for the duration of the write.
   Narrow, and requires a local attacker. Fix: `os.open` with mode `0o600` and `O_CREAT|O_WRONLY|O_TRUNC`.

6. **`http://` is accepted for both the claim URL and the access URL.** `simplefin.py:158,169` allows
   an `http://` claim target, and `_split_access_url` (`simplefin.py:49-63`) accepts any scheme — so
   an `http://` access URL sends basic-auth bank-feed credentials in cleartext on every sync. The
   SimpleFIN spec uses https. Fix: require https, or warn loudly on http.
   (Verified fine on the same paths: `httpx` defaults to `follow_redirects=False`, so there is no
   redirect-based credential leak, and TLS verification is on by default.)

### INFORMATIONAL

7. **`query_sql`'s docstring contradicted its own whitelist about `EXPLAIN`.** ✅ **FIXED in this
   pass** — the docstring now names `_READ_ONLY_PREFIXES` as the source of truth and carries a
   "do not add `explain`" paragraph with the `EXPLAIN ANALYZE COPY ... TO` payload spelled out and
   the pinning test named. The constant was correct throughout; only the prose was wrong.

   Original finding, for the record: the constant is
   correct — `_READ_ONLY_PREFIXES = ("select", "with", "show", "describe", "desc")`,
   `duckdb_store.py:42` — but the docstring says the whitelist accepts
   "SELECT/WITH/**EXPLAIN**/SHOW/DESCRIBE" and later calls it "permissive for `WITH` and `EXPLAIN`"
   (`duckdb_store.py:~2195,~2209`). CLAUDE.md explicitly forbids re-adding `explain` (the
   `EXPLAIN ANALYZE COPY (SELECT 1) TO '/tmp/leak.csv'` case, which the read-only transaction does
   **not** block because `COPY ... TO` writes the filesystem rather than the database). A
   contributor reading only the docstring could "fix" the constant to match it and silently remove a
   control. Documentation defect on a load-bearing security decision — worth correcting.

8. **A non-loopback bind breaks MCP.** `build_server` calls `FastMCP(name)` with no `host`, so
   FastMCP's own `host` setting stays `127.0.0.1` and its auto-enabled allowlist is loopback-only —
   regardless of what uvicorn actually binds. Probe: `Host: 100.85.1.2:8765` → **421**. This fails
   safe (an exposed daemon does not get an unprotected tool surface), but it means `--host` on a
   Tailscale/LAN address serves the dashboard and `/api/v1` while MCP rejects every request. The May
   audit listed this as a future prompt; `--host` plus `--dash-dir` have made it live. Decide it
   deliberately: either document MCP as loopback-only, or pass an explicit
   `transport_security` when the user opts into a non-loopback bind.

9. **Archive decompression is unbounded.** `restore_backup` / `_verify_checksums` call `zf.read()`
   on members without a size cap, so a zip bomb is a memory DoS. User-initiated on a file they
   chose, so low — but it shares a trust boundary with finding 1 and would be fixed by the same
   "treat archives as untrusted input" pass.

### CodeQL — dismissed alerts, with rationale

GitHub Advanced Security raised two `py/redos` alerts ("inefficient regular expression") during this
work, both on the pattern `(a+)+$`:

- `tests/test_validators.py:358` — `test_goal_match_pattern_goes_through_rule_pattern_validator`
- `tests/test_tools.py:283` — the `set_goal` MCP-surface equivalent

Both are **adversarial input asserting rejection**, not patterns the code runs. Each passes the
canonical catastrophic-backtracking shape to `validators.validate_rule_pattern` and asserts it
raises `RulePatternError("nested quantifier")`. The string never reaches a regex engine against
untrusted text — the whole point of the test is that it does not. This is the same category as the
`tests/** = ["S101", "S105", "S608"]` per-file-ignores in `pyproject.toml`: the test tree exercises
the security boundary deliberately.

Dismissed as **"used in tests"** with that reasoning attached to each alert. **If this pattern ever
appears outside a test asserting its rejection, that is a real finding** — the dismissal is scoped to
these two call sites, and a new occurrence will alert again.

Worth noting the alerts were surfaced as inline PR review comments by `github-advanced-security[bot]`
rather than in the `code-scanning/alerts` list for the default branch, which is why a
`state=open` query against the repo returns nothing; they are scoped to `refs/pull/N/head`.

### Clean / re-confirmed

- **gitleaks:** full history (43 commits), no leaks. No personal data found in `src/`.
- **Sensitive logging:** no `logger.*` call in `src/` carries a description, memo, payee, access URL,
  or token. Config docstrings still mark `access_url` sensitive.
- **`sql_query`'s three layers:** intact. Prefix whitelist correctly excludes `explain`; the
  `BEGIN TRANSACTION READ ONLY` wrapper and `enable_external_access=false` are both in place, as are
  the `memory_limit=512MB` / `threads=2` / statement-timeout-watchdog additions from May. Comment
  stripping and statement splitting were reviewed for bypasses — a semicolon or `--` inside a string
  literal causes a false *rejection*, never a false acceptance.
- **Rule-pattern validation:** CLI and MCP still route through the identical
  `validators.validate_rule_pattern`. No fast-path was added.
- **`subprocess` use** (`mcp_config.py:181,208`): list args, no `shell=True`, values are
  typer-validated or constants. May's banked rationale holds.
- **ruff:** `All checks passed`. **bandit:** 8 B608, all reviewed — see below.
- **Tests:** 723 passed, 3 skipped (platform-gated). Every pinned defense still holds; the findings
  above are gaps in coverage, not regressions.

**On the 8 bandit B608 hits.** All are in the backup/restore code added since May
(`duckdb_store.py:537,596,629,637,645,696,715,745`) and all carry `# noqa: S608` with a rationale.
Six of the eight are correctly reasoned — the interpolated value is a table name from
`RESTORE_ORDER`, a sequence name just matched against `duckdb_sequences()`, or an int. But the
suppression comments at `629/637/645/696` justify only the *table name* and are silent on the
*column names* interpolated in the same statement, which is exactly where finding 1 lives. The
suppression was not wrong about what it addressed; it was incomplete about what the line does.
Worth noting as a review-process lesson: a `noqa` rationale should account for every interpolated
value on the line, not the one that prompted it.

## What was fixed in this pass

Findings 2, 3 (the reachable chain), and 7. Gates after the changes: **729 passed, 3 skipped**
(6 new tests), `ruff check` clean, `ruff format` clean, `mypy --strict` clean.

**A CI gate that is about to start failing, and is not caused by this pass.**
`bandit -r src/ -c pyproject.toml` exits **1** anywhere the backup/restore code is present. Measured
per-ref in a clean worktree:

```
origin/main                    -> bandit exit 0
transaction-search-pushdown    -> bandit exit 0   (PR #16, top of the clean run)
backup-restore                 -> bandit exit 1   (PR #17, where it starts)
```

So the bandit step of `.github/workflows/security.yml` is green on `main` today and goes red the
moment PR #17 lands — and stays red through #18, #19 and #20. It has not been failing historically;
an earlier draft of this document claimed it had, from a measurement taken against the stack tip
rather than `main`.

The cause is that the eight B608 sites carry only ruff's `# noqa: S608` and not bandit's
`# nosec B608` — the two tools do not share a suppression mechanism, and elsewhere in the same file
(`duckdb_store.py:1088` and others) both are used together.

**Do not fix this by adding `# nosec` to all eight**: four of them (`629/637/645/696`) are precisely
where finding 1 lives, and suppressing them would bank a rationale this audit just showed to be
incomplete. The honest sequence is to fix finding 1 first — then all eight suppressions are true and
the gate goes green on its own merits. A security gate that is always red teaches everyone to ignore
it, so this wants doing before or alongside the #17 merge, not after.

## Outstanding items

In rough priority order:

1. Validate archive column names against the real schema (finding 1) — still open.
2. Un-break the bandit CI gate, after finding 1 (see above).
3. Decide the non-loopback MCP posture deliberately (finding 8).
4. Findings 4, 5, 6, 9 — latent or negligible for a single-user local install; fix opportunistically.

Future-audit prompts:

- **CI audits a fresh Linux resolve, not the deployment.** Consider a `goetta-finance doctor`-style
  local `pip-audit`, or a periodic reminder — otherwise the venv the daemon runs from drifts
  silently between audits, which is exactly what happened between May and August.
- **The `/dash` SPA is out of scope here.** `goetta-dash` consumes `/api/v1` and renders
  MCP-writable strings (goal names via `set_goal`, transaction descriptions from third parties). If
  it renders any of those with `dangerouslySetInnerHTML`, that is XSS same-origin with the daemon.
  Audit it in its own repo.
- **Restore is the trust boundary nobody had designated.** Findings 1 and 9 both come from archives
  being treated as trusted because goetta-finance wrote them. The next feature that reads an
  external file should start from the opposite assumption.
