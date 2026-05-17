CREATE TABLE accounts (
    id TEXT PRIMARY KEY,
    org_id TEXT,
    org_name TEXT,
    name TEXT NOT NULL,
    currency TEXT NOT NULL DEFAULT 'USD',
    balance DECIMAL(18,2) NOT NULL,
    available_balance DECIMAL(18,2),
    balance_date TIMESTAMP NOT NULL,
    type TEXT,
    extra JSON,
    updated_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE transactions (
    id TEXT PRIMARY KEY,
    account_id TEXT NOT NULL REFERENCES accounts(id),
    posted TIMESTAMP NOT NULL,
    transacted_at TIMESTAMP,
    amount DECIMAL(18,2) NOT NULL,
    description TEXT NOT NULL,
    payee TEXT,
    memo TEXT,
    pending BOOLEAN NOT NULL DEFAULT FALSE,
    extra JSON,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);
CREATE INDEX idx_transactions_account_posted ON transactions(account_id, posted DESC);
CREATE INDEX idx_transactions_posted ON transactions(posted DESC);

CREATE TABLE balance_snapshots (
    account_id TEXT NOT NULL REFERENCES accounts(id),
    timestamp TIMESTAMP NOT NULL,
    balance DECIMAL(18,2) NOT NULL,
    PRIMARY KEY (account_id, timestamp)
);

CREATE SEQUENCE sync_runs_id_seq START 1;
CREATE TABLE sync_runs (
    id INTEGER PRIMARY KEY DEFAULT nextval('sync_runs_id_seq'),
    started_at TIMESTAMP NOT NULL,
    finished_at TIMESTAMP,
    accounts_touched INTEGER NOT NULL DEFAULT 0,
    transactions_new INTEGER NOT NULL DEFAULT 0,
    transactions_updated INTEGER NOT NULL DEFAULT 0,
    warnings JSON,
    errors JSON
);
