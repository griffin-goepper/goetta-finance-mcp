from __future__ import annotations

from datetime import datetime
from decimal import Decimal
from enum import StrEnum
from typing import Any

from pydantic import BaseModel, ConfigDict, Field


class AccountType(StrEnum):
    CHECKING = "checking"
    SAVINGS = "savings"
    CREDIT = "credit"
    INVESTMENT = "investment"
    LOAN = "loan"
    OTHER = "other"


class Account(BaseModel):
    model_config = ConfigDict(frozen=False, extra="forbid")

    id: str
    org_id: str | None = None
    org_name: str | None = None
    name: str
    currency: str = "USD"
    balance: Decimal
    available_balance: Decimal | None = None
    balance_date: datetime
    type: AccountType | None = None
    extra: dict[str, Any] = Field(default_factory=dict)
    is_manual: bool = False
    is_liability: bool = False


class Transaction(BaseModel):
    model_config = ConfigDict(frozen=False, extra="forbid")

    id: str
    account_id: str
    posted: datetime
    transacted_at: datetime | None = None
    amount: Decimal
    description: str
    payee: str | None = None
    memo: str | None = None
    pending: bool = False
    extra: dict[str, Any] = Field(default_factory=dict)


class BalanceSnapshot(BaseModel):
    model_config = ConfigDict(frozen=False, extra="forbid")

    account_id: str
    balance: Decimal
    timestamp: datetime


class SyncRun(BaseModel):
    model_config = ConfigDict(frozen=False, extra="forbid")

    started_at: datetime
    finished_at: datetime | None = None
    accounts_touched: int = 0
    transactions_new: int = 0
    transactions_updated: int = 0
    warnings: list[str] = Field(default_factory=list)
    errors: list[str] = Field(default_factory=list)


class SyncResult(BaseModel):
    """Returned by FinanceStore.upsert_transactions."""

    model_config = ConfigDict(frozen=True, extra="forbid")

    new: int = 0
    updated: int = 0
