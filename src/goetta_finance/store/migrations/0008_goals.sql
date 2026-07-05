-- User-defined goals: spending caps per category/period and balance
-- targets per account. Progress/status is NOT stored -- it is computed
-- at read time (goals.py) so recategorization, rule edits, and new
-- syncs retroactively change goal progress with no backfill, the same
-- design as the transactions_with_category view (0004). Do not add
-- status/progress columns or a goal_events table; read-time evaluation
-- is the feature.
--
-- Single table with a kind discriminator. The table-level CHECK
-- enforces the per-kind column shape; the Python layer
-- (DuckDBStore.add_goal) re-checks with friendlier errors.
--
-- No seeded rows: goals are pure user-state (stranger test). Amounts
-- compare against the account/display currency as-is -- no FX, same
-- posture as the rest of the app.

CREATE SEQUENCE goals_id_seq START 1;

CREATE TABLE goals (
    id INTEGER PRIMARY KEY DEFAULT nextval('goals_id_seq'),
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('spending_cap', 'balance')),
    amount DECIMAL(18,2) NOT NULL CHECK (amount > 0),
    -- spending_cap columns
    category_id INTEGER REFERENCES categories(id),
    period TEXT CHECK (period IN ('month', 'year')),
    -- balance columns
    account_id TEXT REFERENCES accounts(id),
    direction TEXT CHECK (direction IN ('at_least', 'at_most')),
    target_date DATE,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (kind = 'spending_cap'
            AND category_id IS NOT NULL AND period IS NOT NULL
            AND account_id IS NULL AND direction IS NULL AND target_date IS NULL)
        OR
        (kind = 'balance'
            AND account_id IS NOT NULL AND direction IS NOT NULL
            AND category_id IS NULL AND period IS NULL)
    )
);
