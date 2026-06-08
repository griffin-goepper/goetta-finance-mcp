"""Discovery surface for the Uncategorized bucket.

``top_uncategorized_patterns`` is the curation loop's entry point: it
surfaces the largest spending patterns currently resolving to
'Uncategorized', normalized so that bank-template prefixes ("Web
Authorized Pmt ...") and payment-processor wrappers ("TST*", "AplPay")
don't fragment one merchant into many rows.

The prefix-strip list is user-tunable via $GOETTA_FINANCE_HOME/
prefixes.txt (see config.load_prefix_strip_patterns and
CUSTOMIZATION.md) because bank templates vary per institution — the
codebase ships only the universal processor-level prefixes.
"""

from __future__ import annotations

import re
from collections import defaultdict
from datetime import UTC, datetime, timedelta
from decimal import Decimal
from typing import Any

from goetta_finance.config import load_prefix_strip_patterns
from goetta_finance.store import FinanceStore

_TOKEN_RE = re.compile(r"[A-Z0-9][A-Z0-9\-\&\.\'\+]*")


def _normalize(description: str, prefixes: list[re.Pattern[str]]) -> str:
    """Strip known prefixes, uppercase, and key on the first two tokens."""
    d = description.upper()
    changed = True
    # Strip repeatedly: "Recurring Debit Purchase TST* MERCHANT" sheds
    # both the bank template and the processor wrapper.
    while changed:
        changed = False
        for pat in prefixes:
            new = pat.sub("", d, count=1) if pat.match(d) else d
            if new != d:
                d = new.lstrip()
                changed = True
    tokens = _TOKEN_RE.findall(d)
    if not tokens:
        return d.strip() or "?"
    return " ".join(tokens[:2])


def top_uncategorized_patterns(
    store: FinanceStore,
    *,
    days: int = 30,
    top: int = 10,
    now: datetime | None = None,
) -> list[dict[str, Any]]:
    """Largest Uncategorized spending patterns over the last ``days`` days.

    Groups spending-only (amount < 0), non-hidden-account transactions
    whose resolved category is 'Uncategorized' by normalized description
    prefix. Sorted by total descending; at most ``top`` rows. Each row
    carries a ``suggested_command`` the user can copy if they prefer the
    CLI over asking Claude to call ``add_category_rule``.
    """
    end = now or datetime.now(tz=UTC)
    start = end - timedelta(days=days)
    rows = store.query_sql(
        """
        SELECT description, amount
        FROM transactions_with_category
        WHERE posted >= ? AND posted <= ?
          AND amount < 0
          AND category = 'Uncategorized'
          AND COALESCE(account_is_hidden, FALSE) = FALSE
        """,
        [start, end],
    )
    prefixes = load_prefix_strip_patterns()
    groups: dict[str, dict[str, Any]] = defaultdict(
        lambda: {"total": Decimal("0"), "transaction_count": 0, "sample_description": ""}
    )
    for row in rows:
        key = _normalize(str(row["description"]), prefixes)
        g = groups[key]
        g["total"] += -row["amount"]
        g["transaction_count"] += 1
        if not g["sample_description"]:
            g["sample_description"] = str(row["description"])

    ranked = sorted(groups.items(), key=lambda kv: -kv[1]["total"])[:top]
    out: list[dict[str, Any]] = []
    for pattern, g in ranked:
        out.append(
            {
                "pattern": pattern,
                "total": str(g["total"]),
                "transaction_count": g["transaction_count"],
                "sample_description": g["sample_description"],
                "suggested_command": (
                    "goetta-finance category set-rule <category> "
                    f'--match contains --pattern "{pattern}"'
                ),
            }
        )
    return out
