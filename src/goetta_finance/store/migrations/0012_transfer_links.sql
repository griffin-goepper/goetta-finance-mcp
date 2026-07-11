-- Transfer links: roll a manual account's balance forward from matched
-- transactions on a synced source account (e.g. checking debits with
-- payee "Apple Savings" credit the manual "Apple Savings" account).
--
-- Design: write-time roll-forward, NOT read-time effective balance.
-- Applying matched transfers goes through the same store paths as
-- `account set-balance` (accounts.balance + a balance_snapshots row),
-- so every consumer -- net worth, the over-time series, goal progress,
-- monthly_delta -- agrees automatically and "balance is authoritative"
-- stays true. The link merely automates the true-up between manual
-- ones.
--
-- `anchor` is the boundary of trust: transactions posted at or before
-- it are assumed already reflected in the balance (it starts at the
-- account's balance_date when the link is created, and every
-- `set-balance` true-up resets it). Only transactions posted strictly
-- after the anchor are eligible to roll forward.
--
-- No seeded rows: links are pure user-state (stranger test).

CREATE SEQUENCE transfer_links_id_seq START 1;

CREATE TABLE transfer_links (
    id INTEGER PRIMARY KEY DEFAULT nextval('transfer_links_id_seq'),
    -- The manual account whose balance rolls forward.
    account_id TEXT NOT NULL REFERENCES accounts(id),
    -- The synced account whose transactions are scanned.
    source_account_id TEXT NOT NULL REFERENCES accounts(id),
    -- Same match vocabulary as category_rules; matched against the
    -- transaction's payee OR description (payee is where bank transfer
    -- counterparties actually live; category_rules matches description
    -- only and that choice does not fit here).
    match_type TEXT NOT NULL CHECK (match_type IN ('contains', 'regex')),
    pattern TEXT NOT NULL,
    anchor TIMESTAMP NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Ledger of transactions already applied to a manual account, so
-- re-syncs, posted-date drift inside the overlap window, and
-- remove-then-relink can never double-count. PRIMARY KEY on
-- (transaction_id, account_id): a transaction credits a manual account
-- at most once ever, regardless of which link matched it.
--
-- Deliberately NO foreign key to transactions(id) -- the 0011 lesson:
-- a DuckDB FK onto the frequently re-upserted transactions table
-- breaks `INSERT ... ON CONFLICT DO UPDATE` during sync. Referential
-- integrity is enforced in Python (transfers.py only records ids it
-- just read from the transactions table). No FK to accounts either:
-- rows are derived bookkeeping, cleaned up by delete_account.
CREATE TABLE transfer_link_applications (
    transaction_id VARCHAR NOT NULL,
    account_id TEXT NOT NULL,
    link_id INTEGER NOT NULL,
    -- Signed delta applied to the manual balance (= -transactions.amount:
    -- an outflow from the source credits the destination).
    amount DECIMAL(18,2) NOT NULL,
    posted TIMESTAMP NOT NULL,
    applied_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    PRIMARY KEY (transaction_id, account_id)
);
