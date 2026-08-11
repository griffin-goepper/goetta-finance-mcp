"""Transfer-link roll-forward and suggestion math (migration 0012).

ALL of it lives here — one home, like goals.py: the CLI (``account
link``/``links``, post-``sync`` reporting), the daemon's scheduled sync,
and the MCP tools call these functions and never re-derive the math.

The design is write-time roll-forward, not read-time effective balance:
``apply_transfer_links`` pushes matched transfers through the exact
write path ``account set-balance`` uses (``upsert_accounts`` + a
``balance_snapshots`` row), so every consumer of a balance — net worth,
the over-time series, goal progress, ``monthly_delta`` — agrees without
knowing links exist. The applications ledger plus each link's ``anchor``
make application idempotent: a transaction credits a manual account at
most once ever, and everything posted at or before the anchor is
trusted to already be inside the balance.

Suggestions are deliberately conservative and never auto-applied: a
candidate is a synced account's settled transactions whose payee
exactly equals a linkless manual account's name (case-insensitive),
seen at least twice. Surfaces show the candidate with a copy-pasteable
command; the user confirms.
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

from goetta_finance.models import (
    BalanceSnapshot,
    Transaction,
    TransferLink,
    TransferLinkSuggestion,
)
from goetta_finance.store import FinanceStore

# A payee seen once could be a coincidence; twice is a pattern. Keeps
# one-off refunds and name collisions out of the suggestion list.
SUGGESTION_MIN_TRANSACTIONS = 2

_SUGGESTION_SQL = """
SELECT
    d.id            AS account_id,
    d.name          AS account_name,
    t.account_id    AS source_account_id,
    s.name          AS source_account_name,
    t.payee         AS payee,
    COUNT(*)        AS transaction_count,
    SUM(-t.amount)  AS total,
    MIN(t.posted)   AS first_posted,
    MAX(t.posted)   AS last_posted,
    SUM(CASE WHEN t.posted > d.balance_date THEN 1 ELSE 0 END) AS pending_count,
    COALESCE(SUM(CASE WHEN t.posted > d.balance_date THEN -t.amount END), 0) AS pending_total
FROM accounts d
JOIN transactions t
  ON t.payee IS NOT NULL AND lower(t.payee) = lower(d.name)
JOIN accounts s ON s.id = t.account_id
WHERE d.is_manual = TRUE
  AND COALESCE(d.is_liability, FALSE) = FALSE
  AND COALESCE(d.is_hidden, FALSE) = FALSE
  AND s.is_manual = FALSE
  AND s.currency = d.currency
  AND t.pending = FALSE
  AND NOT EXISTS (SELECT 1 FROM transfer_links l WHERE l.account_id = d.id)
GROUP BY d.id, d.name, t.account_id, s.name, t.payee
HAVING COUNT(*) >= ?
ORDER BY d.name, s.name, t.payee
"""


def apply_transfer_links(store: FinanceStore) -> list[str]:
    """Roll linked manual balances forward from newly matched transfers.

    Returns one ASCII summary line per account whose balance moved
    (empty when nothing was eligible) — callers print/log them the way
    they do ``goal_breach_warnings`` lines. Amounts and account names
    only, never transaction text (the logging rule in CLAUDE.md).

    Per account: collect eligible transactions across its links (a
    transaction matched by two links counts once), apply the signed sum
    (outflow from the source credits the manual account; an inflow —
    money moving back — debits it), and advance the balance through the
    ``set-balance`` write path. The ledger rows are written first: if
    the process dies between the two writes, the failure mode is a
    transfer that never applies (visible, fixed by the next
    ``set-balance`` true-up) rather than one that silently applies
    twice.
    """
    links = store.list_transfer_links()
    if not links:
        return []
    accounts = {a.id: a for a in store.get_accounts(include_hidden=True)}
    by_account: dict[str, list[TransferLink]] = {}
    for link in links:
        by_account.setdefault(link.account_id, []).append(link)

    lines: list[str] = []
    for account_id, account_links in by_account.items():
        account = accounts.get(account_id)
        if account is None:  # pragma: no cover - transfer_links FKs accounts
            continue
        seen: set[str] = set()
        batches: list[tuple[TransferLink, list[Transaction]]] = []
        for link in account_links:
            txns = [t for t in store.eligible_transfer_transactions(link) if t.id not in seen]
            seen.update(t.id for t in txns)
            if txns:
                batches.append((link, txns))
        applied = [t for _, txns in batches for t in txns]
        if not applied:
            continue

        delta = sum((-t.amount for t in applied), Decimal("0"))
        for link, txns in batches:
            store.record_transfer_applications(account_id, link.id, txns)
        new_balance = account.balance + delta
        # Never move balance_date backwards: the balance already spoke
        # for at least its current date.
        new_date = max(max(t.posted for t in applied), account.balance_date)
        store.upsert_accounts(
            [account.model_copy(update={"balance": new_balance, "balance_date": new_date})]
        )
        store.record_balance_snapshot(
            BalanceSnapshot(account_id=account_id, balance=new_balance, timestamp=new_date)
        )
        sign = "+" if delta >= 0 else "-"
        noun = "transfer" if len(applied) == 1 else "transfers"
        lines.append(
            f"{account.name}: {sign}{abs(delta):.2f} {account.currency} from "
            f"{len(applied)} linked {noun}; balance now {new_balance:.2f}"
        )
    return lines


def pending_transfer_delta(store: FinanceStore, account_id: str) -> Decimal | None:
    """Signed sum the account's links WOULD apply from still-pending
    source transactions — a read-time preview of the next roll-forward.

    Same signs as ``apply_transfer_links`` (an outflow from the source
    credits the manual account, an inflow debits it) and the same
    cross-link dedup (a transaction matched by two links counts once).
    Nothing is ledgered or applied here; the money moves when the row
    settles and ``apply_transfer_links`` picks it up. Returns ``None``
    when the account has no links (the concept doesn't apply) and
    ``Decimal("0")`` when links exist but nothing matching is pending.

    A ``set-balance`` true-up re-anchors the links, so pending rows
    posted at or before the new anchor silently leave the preview —
    correct, the declared balance already speaks for them.
    """
    links = store.list_transfer_links(account_id=account_id)
    if not links:
        return None
    seen: set[str] = set()
    total = Decimal("0")
    for link in links:
        for txn in store.pending_transfer_transactions(link):
            if txn.id not in seen:
                seen.add(txn.id)
                total += -txn.amount
    return total


def transfer_link_suggestions(store: FinanceStore) -> list[TransferLinkSuggestion]:
    """Detect likely transfer links for manual accounts that have none.

    Exact case-insensitive payee == account-name equality, settled
    transactions only, same currency, at least
    ``SUGGESTION_MIN_TRANSACTIONS`` sightings. Hidden and liability
    manual accounts are skipped (the former are out of the user's view,
    the latter can't be linked yet). Each suggestion carries a
    copy-pasteable ``suggested_command``.
    """
    rows = store.query_sql(_SUGGESTION_SQL, [SUGGESTION_MIN_TRANSACTIONS])
    suggestions: list[TransferLinkSuggestion] = []
    for row in rows:
        payee = str(row["payee"])
        command = (
            f"goetta-finance account link {row['account_id']} "
            f'--from {row["source_account_id"]} --pattern "{payee}"'
        )
        suggestions.append(
            TransferLinkSuggestion(
                account_id=str(row["account_id"]),
                account_name=str(row["account_name"]),
                source_account_id=str(row["source_account_id"]),
                source_account_name=row["source_account_name"],
                payee=payee,
                transaction_count=int(row["transaction_count"]),
                total=Decimal(row["total"]),
                first_posted=_aware(row["first_posted"]),
                last_posted=_aware(row["last_posted"]),
                pending_count=int(row["pending_count"]),
                pending_total=Decimal(row["pending_total"]),
                suggested_command=command,
            )
        )
    return suggestions


def describe_link(link: TransferLink) -> str:
    """One ASCII line for CLI output; mirrors describe_goal's role."""
    dest = link.account_name or link.account_id
    source = link.source_account_name or link.source_account_id
    return (
        f"[{link.id}] {dest} <- {source}: {link.match_type} {link.pattern!r} "
        f"(rolls forward transfers posted after {link.anchor.date().isoformat()})"
    )


def describe_suggestion(suggestion: TransferLinkSuggestion) -> str:
    """One ASCII line for CLI output; command echoed separately."""
    source = suggestion.source_account_name or suggestion.source_account_id
    rollable = (
        f"; {suggestion.pending_count} newer than its balance "
        f"({suggestion.pending_total:.2f}) would roll forward on linking"
        if suggestion.pending_count
        else ""
    )
    return (
        f"{suggestion.account_name}: {suggestion.transaction_count} transfers "
        f"({suggestion.total:.2f}) from {source} match payee "
        f"{suggestion.payee!r}{rollable}"
    )


def _aware(value: datetime) -> datetime:
    """query_sql returns naive-UTC timestamps; models carry tz-aware UTC."""
    if value.tzinfo is None:
        return value.replace(tzinfo=UTC)
    return value.astimezone(UTC)


__all__ = [
    "SUGGESTION_MIN_TRANSACTIONS",
    "apply_transfer_links",
    "describe_link",
    "describe_suggestion",
    "pending_transfer_delta",
    "transfer_link_suggestions",
]
