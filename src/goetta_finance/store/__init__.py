from __future__ import annotations

from collections.abc import Sequence
from datetime import datetime
from typing import Any, Protocol

from goetta_finance.models import (
    Account,
    BalanceSnapshot,
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

    def get_accounts(self) -> list[Account]: ...

    def get_transactions(
        self,
        *,
        account_id: str | None = None,
        start: datetime | None = None,
        end: datetime | None = None,
        limit: int | None = None,
    ) -> list[Transaction]: ...

    def get_balance_history(self, account_id: str, since: datetime) -> list[BalanceSnapshot]: ...

    def query_sql(
        self, sql: str, params: Sequence[Any] | None = None
    ) -> list[dict[str, Any]]: ...
