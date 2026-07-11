from __future__ import annotations

from collections.abc import Sequence
from datetime import date, datetime
from decimal import Decimal
from typing import Any, Protocol

from goetta_finance.models import (
    Account,
    BalanceSnapshot,
    Category,
    Goal,
    SyncResult,
    SyncRun,
    Transaction,
)


class FinanceStore(Protocol):
    def init(self) -> None: ...

    def upsert_accounts(self, accounts: list[Account]) -> None: ...

    def upsert_transactions(self, txns: list[Transaction]) -> SyncResult: ...

    def record_balance_snapshot(self, snap: BalanceSnapshot) -> None: ...

    def record_sync_run(self, run: SyncRun) -> int: ...

    def last_sync_time(self) -> datetime | None: ...

    def get_accounts(self, *, include_hidden: bool = False) -> list[Account]: ...

    def get_transactions(
        self,
        *,
        account_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        category: str | None = None,
        include_hidden: bool = False,
        limit: int | None = None,
    ) -> list[Transaction]: ...

    def get_transactions_with_category(
        self,
        *,
        account_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        category: str | None = None,
        include_hidden: bool = False,
        limit: int | None = None,
    ) -> list[dict[str, Any]]: ...

    def get_balance_history(self, account_id: str, since: datetime) -> list[BalanceSnapshot]: ...

    def query_sql(self, sql: str, params: Sequence[Any] | None = None) -> list[dict[str, Any]]: ...

    def delete_account(self, account_id: str, *, cascade_snapshots: bool = False) -> int: ...

    def set_account_liability(self, account_id: str, is_liability: bool) -> None: ...

    def set_account_hidden(self, account_id: str, is_hidden: bool) -> None: ...

    # Categorization (migration 0004).
    def get_categories(self) -> list[Category]: ...

    def category_counts(self) -> list[dict[str, Any]]: ...

    def add_category(
        self,
        name: str,
        display_color: str | None = None,
        *,
        is_spending: bool = True,
    ) -> Category: ...

    def set_category_spending(self, name: str, is_spending: bool) -> None: ...

    def add_rule(
        self,
        category_name: str,
        *,
        match_type: str,
        pattern: str,
        priority: int = 100,
    ) -> int: ...

    def remove_rule(self, rule_id: int, *, force: bool = False) -> None: ...

    def set_transaction_override(self, transaction_id: str, category_name: str) -> None: ...

    def clear_transaction_override(self, transaction_id: str) -> None: ...

    # Goals (migration 0008). Progress/status is NOT a store concern —
    # it's computed read-time by goals.py from these definitions plus
    # the existing read methods.
    def add_goal(
        self,
        name: str,
        *,
        kind: str,
        amount: Decimal,
        category_name: str | None = None,
        period: str | None = None,
        account_id: str | None = None,
        direction: str | None = None,
        target_date: date | None = None,
    ) -> Goal: ...

    def list_goals(self) -> list[Goal]: ...

    def remove_goal(self, goal_id: int) -> None: ...
