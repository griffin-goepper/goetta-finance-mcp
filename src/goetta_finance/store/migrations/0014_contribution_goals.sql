-- Third goal kind: 'contribution' -- "contribute at least $X into
-- account Y per calendar month/year", counted from the DESTINATION
-- account's own data (matched transactions and/or the transfer-link
-- applications ledger), evaluated at read time like every other kind.
--
-- New columns (contribution-only, enforced by CHECKs below):
--   match_type / match_pattern -- optional matcher for the account's own
--     feed, same 'contains'/'regex' semantics as transfer_links, matched
--     against description OR payee. Progress sums the ABSOLUTE value of
--     matched settled amounts: brokerages commonly sign cash-in negative
--     (e.g. a cash contribution row on an investment feed), so the sign
--     is presentation, not direction. Manual accounts fed by transfer
--     links need no pattern at all -- the applications ledger already
--     records money in.
--   baseline_amount / baseline_date -- contributions made before the
--     feed's history starts (or off-feed); counted into the period that
--     contains baseline_date.
--
-- DuckDB cannot ALTER a table's CHECK constraints, so this is a full
-- table rebuild. Deliberately WITHOUT the usual create-copy-drop-RENAME
-- shape: renaming a table that carries FOREIGN KEY references leaves
-- DuckDB's FK metadata on the PARENT side pointing at the old name --
-- after `ALTER TABLE goals_new RENAME TO goals`, any DELETE FROM
-- accounts fails with "Table with name goals_new does not exist"
-- (observed live under DuckDB 1.x while building this migration).
-- Instead: back the rows up into a constraint-free scratch table, DROP
-- the old table, CREATE the new shape under its final name (so the FKs
-- register correctly), copy back, drop the scratch. Same goals_id_seq
-- default, so ids keep incrementing past the existing max. Existing
-- rows are spending_cap/balance shaped and the new columns are NULL for
-- them, so the copy cannot violate any CHECK.
-- No seeded rows, as ever: goals are pure user-state (stranger test).

CREATE TABLE goals_migration_backup AS
SELECT id, name, kind, amount, category_id, period,
       account_id, direction, target_date, created_at
FROM goals;

DROP TABLE goals;

CREATE TABLE goals (
    id INTEGER PRIMARY KEY DEFAULT nextval('goals_id_seq'),
    name TEXT NOT NULL UNIQUE,
    kind TEXT NOT NULL CHECK (kind IN ('spending_cap', 'balance', 'contribution')),
    amount DECIMAL(18,2) NOT NULL CHECK (amount > 0),
    -- spending_cap columns
    category_id INTEGER REFERENCES categories(id),
    period TEXT CHECK (period IN ('month', 'year')),
    -- balance columns (account_id is shared with contribution)
    account_id TEXT REFERENCES accounts(id),
    direction TEXT CHECK (direction IN ('at_least', 'at_most')),
    target_date DATE,
    -- contribution columns
    match_type TEXT CHECK (match_type IN ('contains', 'regex')),
    match_pattern TEXT,
    baseline_amount DECIMAL(18,2),
    baseline_date TIMESTAMP,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    CHECK (
        (kind = 'spending_cap'
            AND category_id IS NOT NULL AND period IS NOT NULL
            AND account_id IS NULL AND direction IS NULL AND target_date IS NULL)
        OR
        (kind = 'balance'
            AND account_id IS NOT NULL AND direction IS NOT NULL
            AND category_id IS NULL AND period IS NULL)
        OR
        (kind = 'contribution'
            AND account_id IS NOT NULL AND period IS NOT NULL
            AND category_id IS NULL AND direction IS NULL AND target_date IS NULL)
    ),
    -- Matcher fields travel as a pair, and only on contribution goals.
    CHECK ((match_type IS NULL) = (match_pattern IS NULL)),
    CHECK (match_type IS NULL OR kind = 'contribution'),
    -- Baseline fields likewise.
    CHECK ((baseline_amount IS NULL) = (baseline_date IS NULL)),
    CHECK (baseline_amount IS NULL OR kind = 'contribution')
);

INSERT INTO goals
    (id, name, kind, amount, category_id, period,
     account_id, direction, target_date, created_at)
SELECT id, name, kind, amount, category_id, period,
       account_id, direction, target_date, created_at
FROM goals_migration_backup;

DROP TABLE goals_migration_backup;
