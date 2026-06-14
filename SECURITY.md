# Security policy

`goetta-finance` is a local-first tool that holds a SimpleFIN access URL (read access to
your bank data) and runs Claude-reachable SQL against your own database. We take reports
about either surface seriously.

## Supported versions

| Version | Supported |
| ------- | --------- |
| 0.1.x   | ✅        |

This is pre-1.0 software; fixes land on the latest `0.1.x` release.

## Reporting a vulnerability

Please report privately — **do not** open a public issue for a security bug.

Use GitHub's private vulnerability reporting:
**Security ▸ Advisories ▸ Report a vulnerability** on this repository.

Include reproduction steps and the impact you observed. We aim to acknowledge within a few
days. Coordinated disclosure is appreciated; we'll credit you in the advisory unless you
prefer otherwise.

## Threat model (summary)

The primary threat is **prompt injection via free-form transaction text** — descriptions,
memos, and payee names come from sources third parties control (Venmo/Zelle memos, ACH
strings, card-processor text). Claude reads that text and could be tricked into forwarding
it to the `sql_query` MCP tool or into creating a `category_rules` pattern.

Defenses in place (see [`docs/SECURITY_AUDIT_2026-05.md`](docs/SECURITY_AUDIT_2026-05.md)
for the full write-up):

- **`sql_query` defense in depth:** a prefix whitelist (`SELECT/WITH/SHOW/DESCRIBE`, no
  `EXPLAIN`), a `BEGIN TRANSACTION READ ONLY` wrapper, and a DuckDB connection opened with
  `enable_external_access=false` (immutable at runtime) that blocks filesystem/network access
  from SQL (`read_csv`, `COPY ... TO`, `httpfs`).
- **Rule-pattern validation** shared by the CLI and the MCP tool, plus a SQL statement
  timeout (`GOETTA_FINANCE_SQL_TIMEOUT_SECONDS`, default 30s) bounding regex evaluation.
- **No telemetry / no auto-update / no third-party network calls** beyond SimpleFIN itself.
- The SimpleFIN access URL is stored `0600` on POSIX; access URLs and transaction
  descriptions are never logged at INFO or above.

## Known unbounded surface

In-database resource exhaustion via `sql_query` (e.g. huge `generate_series`, recursive
CTEs) is not yet bounded — a local-only denial-of-service. Out of scope for the current
single-user, local-only threat model; documented here for transparency.
