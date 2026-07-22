"""Roll-forward and suggestion math (transfers.py, migration 0012).

The scenario throughout mirrors the motivating case: a manual savings
account whose balance was set once, funded by recurring transfers out of
a synced checking account (payee = the account's name).
"""

from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal

import pytest

from goetta_finance.errors import BalanceTrueUpError
from goetta_finance.models import Account, AccountType, BalanceSnapshot, Transaction
from goetta_finance.store.duckdb_store import DuckDBStore
from goetta_finance.transfers import (
    apply_transfer_links,
    describe_link,
    describe_suggestion,
    pending_transfer_delta,
    transfer_link_suggestions,
    true_up_manual_balance,
)

ANCHOR = datetime(2026, 5, 21, 13, 42, tzinfo=UTC)


def _checking(id: str = "ACT-chk", name: str = "Checking 1234") -> Account:
    return Account(
        id=id,
        org_name="Test Bank",
        name=name,
        currency="USD",
        balance=Decimal("6000.00"),
        balance_date=datetime(2026, 7, 1, tzinfo=UTC),
        type=AccountType.CHECKING,
    )


def _savings(
    id: str = "MANUAL-sav",
    name: str = "Apple Savings",
    *,
    liability: bool = False,
    hidden: bool = False,
) -> Account:
    return Account(
        id=id,
        name=name,
        currency="USD",
        balance=Decimal("10000.00"),
        balance_date=ANCHOR,
        type=AccountType.SAVINGS,
        is_manual=True,
        is_liability=liability,
        is_hidden=hidden,
    )


def _seed(store: DuckDBStore) -> None:
    store.upsert_accounts([_checking(), _savings()])
    store.record_balance_snapshot(
        BalanceSnapshot(account_id="MANUAL-sav", balance=Decimal("10000.00"), timestamp=ANCHOR)
    )


def _txn(
    id: str,
    *,
    month: int = 6,
    day: int,
    amount: str,
    payee: str | None = "Apple Savings",
    pending: bool = False,
) -> Transaction:
    return Transaction(
        id=id,
        account_id="ACT-chk",
        posted=datetime(2026, month, day, 12, tzinfo=UTC),
        amount=Decimal(amount),
        description="Web Authorized Pmt Apple Gs Savings",
        payee=payee,
        pending=pending,
    )


def _balance(store: DuckDBStore, account_id: str = "MANUAL-sav") -> Decimal:
    account = next(a for a in store.get_accounts(include_hidden=True) if a.id == account_id)
    return account.balance


def test_apply_rolls_forward_and_is_idempotent(store: DuckDBStore) -> None:
    _seed(store)
    store.upsert_transactions(
        [
            # Posted before the anchor: assumed inside the $10k already.
            _txn("t-old", month=5, day=15, amount="-500.00"),
            _txn("t-new", month=6, day=12, amount="-500.00"),
        ]
    )
    store.add_transfer_link("MANUAL-sav", "ACT-chk", match_type="contains", pattern="Apple Savings")

    lines = apply_transfer_links(store)
    assert len(lines) == 1
    assert "Apple Savings" in lines[0]
    assert "+500.00" in lines[0]
    assert "10500.00" in lines[0]
    account = next(a for a in store.get_accounts() if a.id == "MANUAL-sav")
    assert account.balance == Decimal("10500.00")
    # balance_date advanced to the applied transaction's posted time and
    # a genuine snapshot exists there — monthly_delta/net-worth-over-time
    # see the movement with no special-casing.
    assert account.balance_date == datetime(2026, 6, 12, 12, tzinfo=UTC)
    history = store.get_balance_history("MANUAL-sav", since=datetime(2026, 1, 1, tzinfo=UTC))
    assert [(s.timestamp, s.balance) for s in history] == [
        (ANCHOR, Decimal("10000.00")),
        (datetime(2026, 6, 12, 12, tzinfo=UTC), Decimal("10500.00")),
    ]

    # Re-running (every sync does) applies nothing new.
    assert apply_transfer_links(store) == []
    assert _balance(store) == Decimal("10500.00")


def test_apply_debits_money_moving_back(store: DuckDBStore) -> None:
    _seed(store)
    store.upsert_transactions([_txn("t-back", day=15, amount="200.00")])
    store.add_transfer_link("MANUAL-sav", "ACT-chk", match_type="contains", pattern="Apple Savings")
    [line] = apply_transfer_links(store)
    assert "-200.00" in line
    assert _balance(store) == Decimal("9800.00")


def test_pending_transactions_wait_until_settled(store: DuckDBStore) -> None:
    _seed(store)
    pending = _txn("t-pend", day=20, amount="-300.00", pending=True)
    store.upsert_transactions([pending])
    store.add_transfer_link("MANUAL-sav", "ACT-chk", match_type="contains", pattern="Apple Savings")
    assert apply_transfer_links(store) == []
    assert _balance(store) == Decimal("10000.00")

    # The sync overlap window later re-upserts it settled.
    store.upsert_transactions([pending.model_copy(update={"pending": False})])
    [line] = apply_transfer_links(store)
    assert "+300.00" in line
    assert _balance(store) == Decimal("10300.00")


def test_overlapping_links_apply_a_transaction_once(store: DuckDBStore) -> None:
    _seed(store)
    store.upsert_transactions([_txn("t-once", day=12, amount="-500.00")])
    store.add_transfer_link("MANUAL-sav", "ACT-chk", match_type="contains", pattern="Apple")
    store.add_transfer_link("MANUAL-sav", "ACT-chk", match_type="contains", pattern="Savings")
    [line] = apply_transfer_links(store)
    assert "+500.00" in line
    assert _balance(store) == Decimal("10500.00")
    ledger = store.conn.execute(
        "SELECT COUNT(*) FROM transfer_link_applications WHERE transaction_id = 't-once'"
    ).fetchone()
    assert ledger is not None and ledger[0] == 1


def test_regex_link_matches_description_when_payee_missing(store: DuckDBStore) -> None:
    _seed(store)
    store.upsert_transactions([_txn("t-desc", day=12, amount="-500.00", payee=None)])
    store.add_transfer_link(
        "MANUAL-sav", "ACT-chk", match_type="regex", pattern="(?i)apple gs savings"
    )
    [line] = apply_transfer_links(store)
    assert "+500.00" in line


def test_true_up_absorbs_past_and_releases_future(store: DuckDBStore) -> None:
    """set-balance semantics end-to-end at the store level: re-anchoring
    at the true-up's as-of absorbs everything posted at or before it and
    re-applies everything after it against the new base — including a
    BACKDATED true-up, where already-applied transactions must re-apply."""
    _seed(store)
    store.upsert_transactions([_txn("t-june", day=12, amount="-500.00")])
    store.add_transfer_link("MANUAL-sav", "ACT-chk", match_type="contains", pattern="Apple Savings")
    apply_transfer_links(store)
    assert _balance(store) == Decimal("10500.00")

    # Forward true-up (June 20): the user's number wins; June 12 is
    # absorbed by it and must NOT re-apply.
    june20 = datetime(2026, 6, 20, tzinfo=UTC)
    account = next(a for a in store.get_accounts() if a.id == "MANUAL-sav")
    store.upsert_accounts(
        [account.model_copy(update={"balance": Decimal("11000.00"), "balance_date": june20})]
    )
    store.record_balance_snapshot(
        BalanceSnapshot(account_id="MANUAL-sav", balance=Decimal("11000.00"), timestamp=june20)
    )
    assert store.reset_transfer_link_anchors("MANUAL-sav", june20) == 1
    assert apply_transfer_links(store) == []
    assert _balance(store) == Decimal("11000.00")

    # New transfer after the true-up applies against the new base.
    store.upsert_transactions([_txn("t-late", day=25, amount="-100.00")])
    apply_transfer_links(store)
    assert _balance(store) == Decimal("11100.00")

    # Backdated true-up (as-of June 1): both June transactions post-date
    # it, so their ledger rows are released and they re-apply on top.
    june1 = datetime(2026, 6, 1, tzinfo=UTC)
    account = next(a for a in store.get_accounts() if a.id == "MANUAL-sav")
    store.upsert_accounts(
        [account.model_copy(update={"balance": Decimal("10000.00"), "balance_date": june1})]
    )
    store.record_balance_snapshot(
        BalanceSnapshot(account_id="MANUAL-sav", balance=Decimal("10000.00"), timestamp=june1)
    )
    store.reset_transfer_link_anchors("MANUAL-sav", june1)
    [line] = apply_transfer_links(store)
    assert "+600.00" in line
    assert _balance(store) == Decimal("10600.00")


def test_shared_true_up_then_later_transfer_applies_exactly_once(store: DuckDBStore) -> None:
    """The shared write path (CLI set-balance AND MCP set_account_balance
    call it): after a true-up, a linked settled transfer posted AFTER the
    true-up's as_of applies once — and only once — on subsequent syncs."""
    _seed(store)
    store.upsert_transactions([_txn("t-early", day=12, amount="-500.00")])
    store.add_transfer_link("MANUAL-sav", "ACT-chk", match_type="contains", pattern="Apple Savings")
    apply_transfer_links(store)
    assert _balance(store) == Decimal("10500.00")

    # Forward true-up (June 20): absorbs t-early, declares 11000.
    june20 = datetime(2026, 6, 20, tzinfo=UTC)
    result = true_up_manual_balance(store, "MANUAL-sav", Decimal("11000.00"), as_of=june20)
    assert result.links_reanchored == 1
    assert result.applied == []  # nothing posted after June 20 yet
    assert result.account.balance == Decimal("11000.00")
    assert result.snapshot.balance == Decimal("11000.00")
    assert result.snapshot.timestamp == june20
    assert _balance(store) == Decimal("11000.00")

    # A transfer posted AFTER the true-up rolls forward on the next sync…
    store.upsert_transactions([_txn("t-late", day=25, amount="-100.00")])
    [line] = apply_transfer_links(store)
    assert "+100.00" in line
    assert _balance(store) == Decimal("11100.00")

    # …and never again: re-running (every sync does) applies nothing, and
    # the ledger holds exactly one row for it.
    assert apply_transfer_links(store) == []
    assert _balance(store) == Decimal("11100.00")
    ledger = store.conn.execute(
        "SELECT COUNT(*) FROM transfer_link_applications WHERE transaction_id = 't-late'"
    ).fetchone()
    assert ledger is not None and ledger[0] == 1


def test_shared_true_up_refusals(store: DuckDBStore) -> None:
    """Unknown, non-manual, non-finite, and future-dated true-ups are
    refused by the shared function — both surfaces inherit the gates."""
    _seed(store)
    now = datetime(2026, 6, 1, tzinfo=UTC)
    with pytest.raises(BalanceTrueUpError, match="account not found"):
        true_up_manual_balance(store, "MANUAL-nope", Decimal("1"), as_of=now)
    with pytest.raises(BalanceTrueUpError, match="non-manual"):
        true_up_manual_balance(store, "ACT-chk", Decimal("1"), as_of=now)
    with pytest.raises(BalanceTrueUpError, match="finite"):
        true_up_manual_balance(store, "MANUAL-sav", Decimal("NaN"), as_of=now)
    with pytest.raises(BalanceTrueUpError, match="future"):
        true_up_manual_balance(
            store, "MANUAL-sav", Decimal("1"), as_of=datetime(2999, 1, 1, tzinfo=UTC)
        )
    # No writes happened.
    assert _balance(store) == Decimal("10000.00")


def test_apply_without_links_is_a_noop(store: DuckDBStore) -> None:
    _seed(store)
    assert apply_transfer_links(store) == []


def test_pending_delta_previews_unsettled_transfers(store: DuckDBStore) -> None:
    _seed(store)
    store.upsert_transactions([_txn("t-pend", month=7, day=10, amount="-800.00", pending=True)])
    store.add_transfer_link("MANUAL-sav", "ACT-chk", match_type="contains", pattern="Apple Savings")
    assert pending_transfer_delta(store, "MANUAL-sav") == Decimal("800.00")
    # Preview only: nothing applies, nothing moves.
    assert apply_transfer_links(store) == []
    assert _balance(store) == Decimal("10000.00")


def test_pending_delta_none_without_links(store: DuckDBStore) -> None:
    _seed(store)
    assert pending_transfer_delta(store, "MANUAL-sav") is None


def test_pending_delta_zero_with_links_and_no_pending(store: DuckDBStore) -> None:
    _seed(store)
    store.upsert_transactions([_txn("t-settled", day=12, amount="-500.00")])
    store.add_transfer_link("MANUAL-sav", "ACT-chk", match_type="contains", pattern="Apple Savings")
    assert pending_transfer_delta(store, "MANUAL-sav") == Decimal("0")


def test_pending_delta_excludes_pre_anchor_pending(store: DuckDBStore) -> None:
    _seed(store)
    store.upsert_transactions([_txn("t-old-pend", month=5, day=15, amount="-500.00", pending=True)])
    store.add_transfer_link("MANUAL-sav", "ACT-chk", match_type="contains", pattern="Apple Savings")
    assert pending_transfer_delta(store, "MANUAL-sav") == Decimal("0")


def test_pending_delta_counts_overlapping_links_once(store: DuckDBStore) -> None:
    _seed(store)
    store.upsert_transactions([_txn("t-pend", day=20, amount="-800.00", pending=True)])
    store.add_transfer_link("MANUAL-sav", "ACT-chk", match_type="contains", pattern="Apple")
    store.add_transfer_link("MANUAL-sav", "ACT-chk", match_type="contains", pattern="Savings")
    assert pending_transfer_delta(store, "MANUAL-sav") == Decimal("800.00")


def test_pending_delta_ignores_already_applied_ids(store: DuckDBStore) -> None:
    """The sync-overlap race: a row settles, applies, then a later chunk
    re-upserts it flagged pending. The ledger row must keep it out of the
    preview or the same money counts twice."""
    _seed(store)
    txn = _txn("t-race", day=12, amount="-500.00")
    store.upsert_transactions([txn])
    store.add_transfer_link("MANUAL-sav", "ACT-chk", match_type="contains", pattern="Apple Savings")
    apply_transfer_links(store)
    assert _balance(store) == Decimal("10500.00")

    store.upsert_transactions([txn.model_copy(update={"pending": True})])
    assert pending_transfer_delta(store, "MANUAL-sav") == Decimal("0")


def test_pending_delta_debits_pending_money_moving_back(store: DuckDBStore) -> None:
    _seed(store)
    store.upsert_transactions([_txn("t-back", day=15, amount="200.00", pending=True)])
    store.add_transfer_link("MANUAL-sav", "ACT-chk", match_type="contains", pattern="Apple Savings")
    assert pending_transfer_delta(store, "MANUAL-sav") == Decimal("-200.00")


def test_pending_delta_hands_off_to_roll_forward_on_settle(store: DuckDBStore) -> None:
    """The full lifecycle: previewed while pending, applied once settled —
    never both at once."""
    _seed(store)
    pending = _txn("t-cycle", month=7, day=10, amount="-800.00", pending=True)
    store.upsert_transactions([pending])
    store.add_transfer_link("MANUAL-sav", "ACT-chk", match_type="contains", pattern="Apple Savings")
    assert pending_transfer_delta(store, "MANUAL-sav") == Decimal("800.00")
    assert _balance(store) == Decimal("10000.00")

    store.upsert_transactions([pending.model_copy(update={"pending": False})])
    [line] = apply_transfer_links(store)
    assert "+800.00" in line
    assert _balance(store) == Decimal("10800.00")
    assert pending_transfer_delta(store, "MANUAL-sav") == Decimal("0")


def test_suggestions_detect_exact_payee_name_match(store: DuckDBStore) -> None:
    _seed(store)
    store.upsert_transactions(
        [
            _txn("t-1", month=5, day=15, amount="-500.00"),
            _txn("t-2", month=6, day=12, amount="-500.00"),
            # Noise that must not suggest: different payee, pending, too rare.
            _txn("t-other", day=13, amount="-50.00", payee="Apple Credit Card"),
            _txn("t-pend", day=14, amount="-500.00", pending=True),
        ]
    )
    [s] = transfer_link_suggestions(store)
    assert s.account_id == "MANUAL-sav"
    assert s.source_account_id == "ACT-chk"
    assert s.payee == "Apple Savings"
    assert s.transaction_count == 2
    assert s.total == Decimal("1000.00")
    # Only the June transfer post-dates the anchor balance.
    assert s.pending_count == 1
    assert s.pending_total == Decimal("500.00")
    assert s.suggested_command == (
        'goetta-finance account link MANUAL-sav --from ACT-chk --pattern "Apple Savings"'
    )
    # Human line stays ASCII and mentions the essentials.
    line = describe_suggestion(s)
    assert "Apple Savings" in line and "2 transfers" in line


def test_suggestions_match_name_case_insensitively(store: DuckDBStore) -> None:
    _seed(store)
    store.upsert_transactions(
        [
            _txn("t-1", day=1, amount="-100.00", payee="APPLE SAVINGS"),
            _txn("t-2", day=2, amount="-100.00", payee="APPLE SAVINGS"),
        ]
    )
    [s] = transfer_link_suggestions(store)
    assert s.payee == "APPLE SAVINGS"


def test_suggestions_require_two_sightings(store: DuckDBStore) -> None:
    _seed(store)
    store.upsert_transactions([_txn("t-1", day=12, amount="-500.00")])
    assert transfer_link_suggestions(store) == []


def test_suggestions_skip_linked_hidden_and_liability_accounts(store: DuckDBStore) -> None:
    store.upsert_accounts(
        [
            _checking(),
            _savings(),
            _savings(id="MANUAL-hidden", name="Hidden Fund", hidden=True),
            _savings(id="MANUAL-loan", name="Loan Fund", liability=True),
        ]
    )
    store.upsert_transactions(
        [
            _txn("t-1", day=1, amount="-100.00"),
            _txn("t-2", day=2, amount="-100.00"),
            _txn("t-h1", day=3, amount="-100.00", payee="Hidden Fund"),
            _txn("t-h2", day=4, amount="-100.00", payee="Hidden Fund"),
            _txn("t-l1", day=5, amount="-100.00", payee="Loan Fund"),
            _txn("t-l2", day=6, amount="-100.00", payee="Loan Fund"),
        ]
    )
    [s] = transfer_link_suggestions(store)
    assert s.account_id == "MANUAL-sav"

    # Linking the remaining candidate silences it too.
    store.add_transfer_link("MANUAL-sav", "ACT-chk", match_type="contains", pattern="Apple Savings")
    assert transfer_link_suggestions(store) == []


def test_describe_link_is_ascii_and_names_both_sides(store: DuckDBStore) -> None:
    _seed(store)
    link = store.add_transfer_link(
        "MANUAL-sav", "ACT-chk", match_type="contains", pattern="Apple Savings"
    )
    line = describe_link(link)
    assert "Apple Savings <- Checking 1234" in line
    assert "2026-05-21" in line
    assert line.isascii()
