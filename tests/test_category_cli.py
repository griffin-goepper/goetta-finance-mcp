from __future__ import annotations

from datetime import UTC, datetime
from decimal import Decimal
from pathlib import Path

import pytest
from typer.testing import CliRunner

from goetta_finance.cli import app
from goetta_finance.models import Account, AccountType, Transaction
from goetta_finance.store.duckdb_store import DuckDBStore

runner = CliRunner()


@pytest.fixture
def fresh_home(tmp_path: Path, monkeypatch: pytest.MonkeyPatch) -> Path:
    """GOETTA_FINANCE_HOME with a migrated empty DuckDB + the legacy
    merchant rules (STARBUCKS / KROGER / SHELL / AMAZON.COM) that
    migration 0007 demoted. Tests in this file used those merchants as
    convenient examples of "rule resolves to category" — see
    conftest.seed_legacy_merchant_rules for the rationale."""
    from tests.conftest import seed_legacy_merchant_rules

    monkeypatch.setenv("GOETTA_FINANCE_HOME", str(tmp_path))
    store = DuckDBStore(tmp_path / "data.duckdb")
    store.init()
    seed_legacy_merchant_rules(store)
    store.close()
    return tmp_path


def _seed_acct_and_txn(
    home: Path, *, txn_id: str = "ACT-tx-1", description: str = "STARBUCKS STORE #1"
) -> None:
    """Add one account + one transaction so override / categorize commands
    have a target. Closes the store before returning so the CLI can reopen."""
    store = DuckDBStore(home / "data.duckdb")
    try:
        store.upsert_accounts(
            [
                Account(
                    id="ACT-test",
                    org_name="Test",
                    name="Checking",
                    balance=Decimal("100.00"),
                    balance_date=datetime(2026, 5, 1, tzinfo=UTC),
                    type=AccountType.CHECKING,
                )
            ]
        )
        store.upsert_transactions(
            [
                Transaction(
                    id=txn_id,
                    account_id="ACT-test",
                    posted=datetime(2026, 5, 10, tzinfo=UTC),
                    amount=Decimal("-10.00"),
                    description=description,
                )
            ]
        )
    finally:
        store.close()


def _category_count(home: Path, name: str) -> int:
    store = DuckDBStore(home / "data.duckdb")
    try:
        row = store.conn.execute(
            "SELECT COUNT(*) FROM categories WHERE name = ?", [name]
        ).fetchone()
        assert row is not None
        return int(row[0])
    finally:
        store.close()


def _rule_count(home: Path) -> int:
    store = DuckDBStore(home / "data.duckdb")
    try:
        row = store.conn.execute("SELECT COUNT(*) FROM category_rules").fetchone()
        assert row is not None
        return int(row[0])
    finally:
        store.close()


def _resolved_category(home: Path, txn_id: str) -> str:
    store = DuckDBStore(home / "data.duckdb")
    try:
        row = store.conn.execute(
            "SELECT category FROM transactions_with_category WHERE id = ?",
            [txn_id],
        ).fetchone()
        assert row is not None
        return str(row[0])
    finally:
        store.close()


# --- category list / add / default-rules -----------------------------------


def test_category_list_shows_defaults(fresh_home: Path) -> None:
    result = runner.invoke(app, ["category", "list"])
    assert result.exit_code == 0, result.output
    # All 14 default category names appear, with the default flag set.
    for name in [
        "Groceries",
        "Dining",
        "Transportation",
        "Gas",
        "Utilities",
        "Subscriptions",
        "Rent/Mortgage",
        "Healthcare",
        "Entertainment",
        "Shopping",
        "Travel",
        "Transfers",
        "Income",
        "Uncategorized",
    ]:
        assert name in result.output
    # At least one "yes" appears for the Default column.
    assert "yes" in result.output


def test_category_list_shows_txn_and_rule_counts(fresh_home: Path) -> None:
    _seed_acct_and_txn(fresh_home, txn_id="ACT-sbux", description="STARBUCKS STORE #1")
    result = runner.invoke(app, ["category", "list"])
    assert result.exit_code == 0, result.output
    # The Dining row should show at least 1 txn (the Starbucks transaction).
    dining_line = next(ln for ln in result.output.splitlines() if ln.startswith("Dining"))
    parts = dining_line.split()
    # Columns: Category Default Txns Rules
    assert parts[2] == "1"
    # And at least one rule (Dining has multiple defaults).
    assert int(parts[3]) >= 1


def test_category_add_writes_non_default_row(fresh_home: Path) -> None:
    result = runner.invoke(app, ["category", "add", "--name", "MyCustomCat", "--color", "#abcdef"])
    assert result.exit_code == 0, result.output
    assert "MyCustomCat" in result.output
    store = DuckDBStore(fresh_home / "data.duckdb")
    try:
        row = store.conn.execute(
            "SELECT display_color, is_default FROM categories WHERE name = ?",
            ["MyCustomCat"],
        ).fetchone()
    finally:
        store.close()
    assert row is not None
    assert row[0] == "#abcdef"
    assert bool(row[1]) is False


def test_category_add_no_spending_flag(fresh_home: Path) -> None:
    """`category add --no-spending` writes a non-spending row."""
    result = runner.invoke(app, ["category", "add", "--name", "MyNonSpend", "--no-spending"])
    assert result.exit_code == 0, result.output
    assert "[non-spending]" in result.output
    store = DuckDBStore(fresh_home / "data.duckdb")
    try:
        cat = next(c for c in store.get_categories() if c.name == "MyNonSpend")
        assert cat.is_spending is False
    finally:
        store.close()


def test_category_set_spending_toggle(fresh_home: Path) -> None:
    """set-spending flips the flag; list shows [non-spending] tag."""
    result = runner.invoke(app, ["category", "set-spending", "Dining", "false"])
    assert result.exit_code == 0, result.output
    listed = runner.invoke(app, ["category", "list"])
    assert listed.exit_code == 0
    # Find the Dining row in the output
    dining_line = next(ln for ln in listed.output.splitlines() if ln.startswith("Dining"))
    assert "[non-spending]" in dining_line
    # Flip back.
    runner.invoke(app, ["category", "set-spending", "Dining", "true"])
    listed = runner.invoke(app, ["category", "list"])
    dining_line = next(ln for ln in listed.output.splitlines() if ln.startswith("Dining"))
    assert "[non-spending]" not in dining_line


def test_category_set_spending_case_insensitive(fresh_home: Path) -> None:
    result = runner.invoke(app, ["category", "set-spending", "dining", "false"])
    assert result.exit_code == 0, result.output
    store = DuckDBStore(fresh_home / "data.duckdb")
    try:
        cats = {c.name: c for c in store.get_categories()}
        assert cats["Dining"].is_spending is False
    finally:
        store.close()


def test_category_set_spending_unknown_category_suggests(fresh_home: Path) -> None:
    result = runner.invoke(app, ["category", "set-spending", "Dinning", "false"])
    assert result.exit_code != 0
    assert 'Did you mean "Dining"' in result.output


def test_category_list_shows_non_spending_tag_for_defaults(fresh_home: Path) -> None:
    """Transfers and Income are seeded as non-spending; the [non-spending]
    tag must show in `category list`."""
    result = runner.invoke(app, ["category", "list"])
    assert result.exit_code == 0, result.output
    transfers_line = next(ln for ln in result.output.splitlines() if ln.startswith("Transfers"))
    income_line = next(ln for ln in result.output.splitlines() if ln.startswith("Income"))
    assert "[non-spending]" in transfers_line
    assert "[non-spending]" in income_line
    # Dining is spending — no tag.
    dining_line = next(ln for ln in result.output.splitlines() if ln.startswith("Dining"))
    assert "[non-spending]" not in dining_line


def test_category_add_rejects_bad_color(fresh_home: Path) -> None:
    result = runner.invoke(app, ["category", "add", "--name", "BadColor", "--color", "not-a-color"])
    assert result.exit_code != 0
    assert _category_count(fresh_home, "BadColor") == 0


def test_category_default_rules_lists_seeds(fresh_home: Path) -> None:
    """After migration 0007 the default rules are intentionally minimal
    (Spotify/Netflix/Hulu/Disney Plus/Amazon Prime → Subscriptions plus
    the (?i)transfer regex → Transfers). The CLI's default-rules
    command should surface that universal set."""
    result = runner.invoke(app, ["category", "default-rules"])
    assert result.exit_code == 0, result.output
    assert "Subscriptions" in result.output
    assert "Transfers" in result.output
    assert "SPOTIFY" in result.output
    assert "(?i)transfer" in result.output
    assert "[rule " in result.output  # rule ids are printed


# --- category set-rule (validator + happy path) -----------------------------


def test_category_set_rule_writes_and_prints_id(fresh_home: Path) -> None:
    before = _rule_count(fresh_home)
    result = runner.invoke(
        app,
        ["category", "set-rule", "Dining", "--match", "contains", "--pattern", "ZZZ-UBER-EATS-X"],
    )
    assert result.exit_code == 0, result.output
    assert "Added rule" in result.output
    assert _rule_count(fresh_home) == before + 1


def test_category_set_rule_refuses_uncompilable_regex(fresh_home: Path) -> None:
    before = _rule_count(fresh_home)
    result = runner.invoke(
        app, ["category", "set-rule", "Dining", "--match", "regex", "--pattern", "["]
    )
    assert result.exit_code != 0
    assert _rule_count(fresh_home) == before


def test_category_set_rule_refuses_nested_quantifier_pattern(fresh_home: Path) -> None:
    """Heuristic refuses the canonical (X+)+ shape — the classic ReDoS
    construct — at write time. Pin the outcome (no row + non-zero exit),
    not the exact wording.

    Design note: an earlier draft attempted a runtime ``re.search`` in a
    daemon thread with a 1s ``Event.wait`` timeout. Empirically that
    doesn't work on CPython: the ``re`` engine does NOT release the GIL
    during matching, so the wait can never preempt a long-running
    pattern (measured: ``(a+)+$`` against a 30-char sentinel held the
    GIL for 49 seconds while wait(1.0) was blocked the whole time).
    The heuristic shape check is the write-time best-effort; the
    runtime backstop is the query_sql statement-timeout watchdog."""
    before = _rule_count(fresh_home)
    result = runner.invoke(
        app,
        ["category", "set-rule", "Dining", "--match", "regex", "--pattern", "(a+)+"],
    )
    assert result.exit_code != 0
    assert _rule_count(fresh_home) == before


def test_category_set_rule_refuses_anchored_nested_quantifier(fresh_home: Path) -> None:
    """The anchored ``(a+)+$`` shape is also refused. This is the variant
    that genuinely backtracks on modern CPython (CPython 3.11 short-circuits
    the unanchored form for many inputs but cannot avoid the anchor case)."""
    before = _rule_count(fresh_home)
    result = runner.invoke(
        app,
        ["category", "set-rule", "Dining", "--match", "regex", "--pattern", "(a+)+$"],
    )
    assert result.exit_code != 0
    assert _rule_count(fresh_home) == before


def test_category_set_rule_refuses_large_counted_repetition(fresh_home: Path) -> None:
    """Large ``{N,}`` repetitions are refused (N > 10). Catches the
    ``(.*a){25}`` family that the nested-quantifier heuristic misses."""
    before = _rule_count(fresh_home)
    result = runner.invoke(
        app,
        ["category", "set-rule", "Dining", "--match", "regex", "--pattern", "(.*a){25}"],
    )
    assert result.exit_code != 0
    assert _rule_count(fresh_home) == before


def test_category_set_rule_accepts_legitimate_regex(fresh_home: Path) -> None:
    """Sanity: the validator doesn't false-positive on a simple regex."""
    before = _rule_count(fresh_home)
    result = runner.invoke(
        app,
        ["category", "set-rule", "Dining", "--match", "regex", "--pattern", "(?i)venmo"],
    )
    assert result.exit_code == 0, result.output
    assert _rule_count(fresh_home) == before + 1


def test_category_set_rule_refuses_empty_pattern(fresh_home: Path) -> None:
    before = _rule_count(fresh_home)
    result = runner.invoke(
        app, ["category", "set-rule", "Dining", "--match", "contains", "--pattern", ""]
    )
    assert result.exit_code != 0
    assert _rule_count(fresh_home) == before


def test_category_set_rule_refuses_unknown_category(fresh_home: Path) -> None:
    before = _rule_count(fresh_home)
    result = runner.invoke(
        app,
        ["category", "set-rule", "NoSuchCat", "--match", "contains", "--pattern", "X"],
    )
    assert result.exit_code != 0
    assert "category not found" in result.output.lower()
    assert _rule_count(fresh_home) == before


def test_category_set_rule_unknown_category_suggests_close_match(fresh_home: Path) -> None:
    """Typo "Dinning" → "Dining" suggestion. Pin the load-bearing UX, not
    the exact phrasing of the surrounding error."""
    result = runner.invoke(
        app,
        ["category", "set-rule", "Dinning", "--match", "contains", "--pattern", "X"],
    )
    assert result.exit_code != 0
    assert 'Did you mean "Dining"' in result.output


def test_category_set_rule_unknown_category_fallback_when_no_close_match(
    fresh_home: Path,
) -> None:
    """A genuinely-different name falls back to the list-command hint."""
    result = runner.invoke(
        app,
        ["category", "set-rule", "Foodstuffs", "--match", "contains", "--pattern", "X"],
    )
    assert result.exit_code != 0
    assert "category list" in result.output
    assert "Did you mean" not in result.output


def test_category_set_rule_case_insensitive(fresh_home: Path) -> None:
    """Lower-case category name resolves to the canonical row."""
    before = _rule_count(fresh_home)
    result = runner.invoke(
        app,
        ["category", "set-rule", "dining", "--match", "contains", "--pattern", "ZZZ-CHILIS"],
    )
    assert result.exit_code == 0, result.output
    assert _rule_count(fresh_home) == before + 1


# --- category set-rule amount bounds (migration 0009) ------------------------


def _rule_bounds(home: Path, rule_id: int) -> tuple[Decimal | None, Decimal | None]:
    store = DuckDBStore(home / "data.duckdb")
    try:
        row = store.conn.execute(
            "SELECT min_amount, max_amount FROM category_rules WHERE id = ?", [rule_id]
        ).fetchone()
        assert row is not None
        return (
            row[0] if isinstance(row[0], Decimal) else None,
            row[1] if isinstance(row[1], Decimal) else None,
        )
    finally:
        store.close()


def test_category_set_rule_with_amount_bounds(fresh_home: Path) -> None:
    before = _rule_count(fresh_home)
    result = runner.invoke(
        app,
        [
            "category",
            "set-rule",
            "Dining",
            "--pattern",
            "ZZZ-SPEEDY",
            "--max-amount",
            "20",
        ],
    )
    assert result.exit_code == 0, result.output
    assert "Added rule" in result.output
    assert "|amount| < $20.00" in result.output
    assert _rule_count(fresh_home) == before + 1
    # The bound round-trips as an exact Decimal.
    rule_id = int(result.output.split("Added rule ")[1].split(":")[0])
    assert _rule_bounds(fresh_home, rule_id) == (None, Decimal("20.00"))


def test_category_set_rule_rejects_non_numeric_bound(fresh_home: Path) -> None:
    """Bad bound fails at _parse_decimal, before the store is opened."""
    before = _rule_count(fresh_home)
    result = runner.invoke(
        app,
        ["category", "set-rule", "Dining", "--pattern", "ZZZ-X", "--max-amount", "abc"],
    )
    assert result.exit_code != 0
    assert _rule_count(fresh_home) == before


def test_category_set_rule_rejects_inverted_bounds(fresh_home: Path) -> None:
    before = _rule_count(fresh_home)
    result = runner.invoke(
        app,
        [
            "category",
            "set-rule",
            "Dining",
            "--pattern",
            "ZZZ-X",
            "--min-amount",
            "30",
            "--max-amount",
            "20",
        ],
    )
    assert result.exit_code != 0
    assert _rule_count(fresh_home) == before


def test_category_set_rule_rejects_subcent_bound(fresh_home: Path) -> None:
    before = _rule_count(fresh_home)
    result = runner.invoke(
        app,
        [
            "category",
            "set-rule",
            "Dining",
            "--pattern",
            "ZZZ-X",
            "--max-amount",
            "19.999",
        ],
    )
    assert result.exit_code != 0
    assert _rule_count(fresh_home) == before


def test_category_remove_rule_echo_includes_bounds(fresh_home: Path) -> None:
    """The remove confirmation names the bounds so the user can tell
    complementary same-pattern rules apart."""
    store = DuckDBStore(fresh_home / "data.duckdb")
    try:
        rule_id = store.add_rule(
            "Dining",
            match_type="contains",
            pattern="ZZZ-BOUNDED",
            priority=10,
            max_amount=Decimal("20.00"),
        )
    finally:
        store.close()
    result = runner.invoke(app, ["category", "remove-rule", str(rule_id)])
    assert result.exit_code == 0, result.output
    assert "|amount| < $20.00" in result.output


# --- category remove-rule ---------------------------------------------------


def _add_user_rule(home: Path, pattern: str = "ZZZ-USERX") -> int:
    """Add a non-default rule and return its id."""
    store = DuckDBStore(home / "data.duckdb")
    try:
        return store.add_rule("Dining", match_type="contains", pattern=pattern, priority=10)
    finally:
        store.close()


def _first_default_rule_id_and_pattern(home: Path) -> tuple[int, str]:
    store = DuckDBStore(home / "data.duckdb")
    try:
        row = store.conn.execute(
            "SELECT id, pattern FROM category_rules WHERE is_default = TRUE ORDER BY id LIMIT 1"
        ).fetchone()
        assert row is not None
        return int(row[0]), str(row[1])
    finally:
        store.close()


def test_category_remove_rule_user_rule(fresh_home: Path) -> None:
    rule_id = _add_user_rule(fresh_home)
    result = runner.invoke(app, ["category", "remove-rule", str(rule_id)])
    assert result.exit_code == 0, result.output
    store = DuckDBStore(fresh_home / "data.duckdb")
    try:
        row = store.conn.execute(
            "SELECT COUNT(*) FROM category_rules WHERE id = ?", [rule_id]
        ).fetchone()
    finally:
        store.close()
    assert row is not None and row[0] == 0


def test_category_remove_default_rule_refused_without_force(fresh_home: Path) -> None:
    rule_id, _ = _first_default_rule_id_and_pattern(fresh_home)
    before = _rule_count(fresh_home)
    result = runner.invoke(app, ["category", "remove-rule", str(rule_id)])
    assert result.exit_code != 0
    assert "default" in result.output.lower()
    assert _rule_count(fresh_home) == before


def test_category_remove_default_rule_with_force_and_yes(fresh_home: Path) -> None:
    rule_id, _ = _first_default_rule_id_and_pattern(fresh_home)
    before = _rule_count(fresh_home)
    result = runner.invoke(app, ["category", "remove-rule", str(rule_id), "--force", "--yes"])
    assert result.exit_code == 0, result.output
    assert _rule_count(fresh_home) == before - 1


def test_category_remove_default_rule_with_force_correct_typed_pattern(
    fresh_home: Path,
) -> None:
    rule_id, pattern = _first_default_rule_id_and_pattern(fresh_home)
    before = _rule_count(fresh_home)
    result = runner.invoke(
        app, ["category", "remove-rule", str(rule_id), "--force"], input=f"{pattern}\n"
    )
    assert result.exit_code == 0, result.output
    assert _rule_count(fresh_home) == before - 1


def test_category_remove_default_rule_with_force_wrong_typed_pattern(
    fresh_home: Path,
) -> None:
    rule_id, _ = _first_default_rule_id_and_pattern(fresh_home)
    before = _rule_count(fresh_home)
    result = runner.invoke(
        app,
        ["category", "remove-rule", str(rule_id), "--force"],
        input="DEFINITELY-NOT-THE-PATTERN\n",
    )
    assert result.exit_code != 0
    assert _rule_count(fresh_home) == before


def test_category_remove_rule_unknown_id(fresh_home: Path) -> None:
    result = runner.invoke(app, ["category", "remove-rule", "999999"])
    assert result.exit_code != 0
    assert "not found" in result.output.lower()


# --- transaction categorize / uncategorize ---------------------------------


def test_transaction_categorize_round_trip(fresh_home: Path) -> None:
    _seed_acct_and_txn(fresh_home, txn_id="ACT-tx-cat", description="ZZZ unmatched")
    assert _resolved_category(fresh_home, "ACT-tx-cat") == "Uncategorized"
    result = runner.invoke(app, ["transaction", "categorize", "ACT-tx-cat", "Dining"])
    assert result.exit_code == 0, result.output
    assert _resolved_category(fresh_home, "ACT-tx-cat") == "Dining"


def test_transaction_categorize_case_insensitive(fresh_home: Path) -> None:
    """`categorize <id> dining` resolves to the canonical Dining."""
    _seed_acct_and_txn(fresh_home, txn_id="ACT-tx-ci", description="ZZZ unmatched")
    result = runner.invoke(app, ["transaction", "categorize", "ACT-tx-ci", "dining"])
    assert result.exit_code == 0, result.output
    assert _resolved_category(fresh_home, "ACT-tx-ci") == "Dining"


def test_transaction_categorize_unknown_category(fresh_home: Path) -> None:
    _seed_acct_and_txn(fresh_home, txn_id="ACT-tx-uc")
    result = runner.invoke(app, ["transaction", "categorize", "ACT-tx-uc", "NoSuchCat"])
    assert result.exit_code != 0
    assert "category not found" in result.output.lower()


def test_transaction_categorize_unknown_category_suggests_close_match(
    fresh_home: Path,
) -> None:
    _seed_acct_and_txn(fresh_home, txn_id="ACT-tx-sm")
    result = runner.invoke(app, ["transaction", "categorize", "ACT-tx-sm", "Dinning"])
    assert result.exit_code != 0
    assert 'Did you mean "Dining"' in result.output


def test_transaction_categorize_unknown_transaction(fresh_home: Path) -> None:
    result = runner.invoke(app, ["transaction", "categorize", "NO-SUCH-TXN", "Dining"])
    assert result.exit_code != 0
    assert "transaction not found" in result.output.lower()


def test_transaction_uncategorize_round_trip(fresh_home: Path) -> None:
    _seed_acct_and_txn(fresh_home, txn_id="ACT-tx-un", description="STARBUCKS STORE #1")
    # Pre-override: Dining via the default rule.
    assert _resolved_category(fresh_home, "ACT-tx-un") == "Dining"
    runner.invoke(app, ["transaction", "categorize", "ACT-tx-un", "Shopping"])
    assert _resolved_category(fresh_home, "ACT-tx-un") == "Shopping"
    result = runner.invoke(app, ["transaction", "uncategorize", "ACT-tx-un"])
    assert result.exit_code == 0, result.output
    # Falls back to rule resolution (not 'Uncategorized').
    assert _resolved_category(fresh_home, "ACT-tx-un") == "Dining"


def test_transaction_uncategorize_noop_when_absent(fresh_home: Path) -> None:
    _seed_acct_and_txn(fresh_home, txn_id="ACT-tx-nop", description="ZZZ unmatched")
    result = runner.invoke(app, ["transaction", "uncategorize", "ACT-tx-nop"])
    assert result.exit_code == 0, result.output
    # No prior override, no error, category stays 'Uncategorized'.
    assert _resolved_category(fresh_home, "ACT-tx-nop") == "Uncategorized"
