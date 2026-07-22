from __future__ import annotations

from datetime import date, datetime
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
    is_hidden: bool = False


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


class Category(BaseModel):
    model_config = ConfigDict(frozen=False, extra="forbid")

    id: int
    name: str
    display_color: str | None = None
    is_default: bool = False
    is_spending: bool = True


class GoalKind(StrEnum):
    SPENDING_CAP = "spending_cap"
    BALANCE = "balance"
    CONTRIBUTION = "contribution"


class GoalPeriod(StrEnum):
    MONTH = "month"
    YEAR = "year"


class GoalDirection(StrEnum):
    AT_LEAST = "at_least"
    AT_MOST = "at_most"


class GoalStatus(StrEnum):
    """Computed goal state, never persisted.

    Spending caps use ON_TRACK | AT_RISK | OVER. Balance goals use
    MET | ON_TRACK | AT_RISK, plus OVER for a breached at_most goal
    (owing more than the ceiling). An unmet at_least goal is not a
    failure state -- it's ON_TRACK or AT_RISK depending on pace.
    Contribution goals use MET | ON_TRACK | AT_RISK and never OVER:
    funding ahead of the clock is ON_TRACK (the INVERSE of caps, where
    ahead-of-pace is AT_RISK).
    """

    ON_TRACK = "on_track"
    AT_RISK = "at_risk"
    OVER = "over"
    MET = "met"


class Goal(BaseModel):
    """A user-defined threshold (migration 0008). Progress is computed
    at read time by ``goals.py`` -- this model carries only the
    definition. ``category_name``/``account_name`` are display
    denormalizations resolved by ``list_goals``'s JOIN."""

    model_config = ConfigDict(frozen=False, extra="forbid")

    id: int
    name: str
    kind: GoalKind
    amount: Decimal
    # spending_cap
    category_id: int | None = None
    category_name: str | None = None
    period: GoalPeriod | None = None
    # balance (account_id/account_name shared with contribution)
    account_id: str | None = None
    account_name: str | None = None
    direction: GoalDirection | None = None
    target_date: date | None = None
    # contribution (migration 0014): optional matcher against the
    # account's own feed (description OR payee, transfer-link semantics,
    # ABS amounts) and an optional pre-history baseline counted into the
    # period containing baseline_date.
    match_type: str | None = None
    match_pattern: str | None = None
    baseline_amount: Decimal | None = None
    baseline_date: datetime | None = None
    # contribution (migration 0015): DECLARED recurring schedule —
    # recurring_amount accrues per payday generated from
    # recurring_anchor at recurring_interval ('weekly'|'biweekly'|
    # 'monthly'), by calculation, never observed in any feed. The
    # triple travels all-or-none (application-enforced; 0015 is plain
    # ALTERs, no table CHECK).
    recurring_amount: Decimal | None = None
    recurring_interval: str | None = None
    recurring_anchor: date | None = None
    created_at: datetime


class GoalProgress(BaseModel):
    """Snapshot of a read-time goal evaluation (like SyncResult, frozen).

    ``percent`` may be negative (refunds exceeding spending) or over
    100 (cap breached / target passed). Spending-cap fields are None on
    balance goals and vice versa.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    goal: Goal
    status: GoalStatus
    current: Decimal
    target: Decimal
    percent: Decimal
    # spending caps only
    period_start: datetime | None = None
    period_end: datetime | None = None
    period_elapsed_percent: Decimal | None = None
    # balance goals only
    monthly_delta: Decimal | None = None
    required_monthly: Decimal | None = None
    projected_date: date | None = None
    # still-pending linked transfers, counted into the balance when they
    # settle; positive = approaching. None when the account has no links.
    pending_delta: Decimal | None = None
    # contribution goals with a recurring schedule (0015): the DECLARED
    # portion of ``current`` — paydays elapsed this period x
    # recurring_amount. Internal (feeds the shared prose disclosure);
    # NOT part of the list_goals wire shape.
    declared_total: Decimal | None = None


class TransferLink(BaseModel):
    """Roll-forward config for a manual account (migration 0012).

    Matching transactions on ``source_account_id`` posted strictly after
    ``anchor`` are applied to the manual account's balance by
    ``transfers.apply_transfer_links`` -- the definition carries no
    progress state (the applications ledger and balance_snapshots do).
    ``account_name``/``source_account_name`` are display denormalizations
    resolved by ``list_transfer_links``'s JOIN, same as ``Goal``.
    """

    model_config = ConfigDict(frozen=False, extra="forbid")

    id: int
    account_id: str
    account_name: str | None = None
    source_account_id: str
    source_account_name: str | None = None
    match_type: str
    pattern: str
    anchor: datetime
    created_at: datetime


class TransferLinkSuggestion(BaseModel):
    """A detected link candidate (like GoalProgress: read-time, frozen).

    Produced by ``transfers.transfer_link_suggestions`` when a synced
    account's transactions carry a payee that exactly matches a linkless
    manual account's name (case-insensitive). Never auto-applied --
    surfaces show it with ``suggested_command`` and the user confirms.
    """

    model_config = ConfigDict(frozen=True, extra="forbid")

    account_id: str
    account_name: str
    source_account_id: str
    source_account_name: str | None = None
    payee: str
    transaction_count: int
    total: Decimal
    first_posted: datetime
    last_posted: datetime
    # How many of those transactions post-date the account's balance_date
    # and would roll forward immediately on linking.
    pending_count: int
    pending_total: Decimal
    suggested_command: str


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
