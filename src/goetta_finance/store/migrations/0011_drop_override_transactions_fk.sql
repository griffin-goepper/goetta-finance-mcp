-- Rebuild transaction_overrides without the FOREIGN KEY to transactions(id).
--
-- Why: DuckDB refuses INSERT ... ON CONFLICT DO UPDATE on any row whose key
-- is referenced by a foreign key ("Violates foreign key constraint because
-- key ... is still referenced", a documented DuckDB limitation — the
-- conflict-update path is a physical delete+insert and the parent-side
-- delete trips the check regardless of whether the key changes). With this
-- FK in place, categorize-overriding any transaction newer than the sync
-- re-pull window (~30 days) made every subsequent sync fail on that row's
-- re-upsert until it aged out. Found 2026-07-06 while root-causing the
-- index incident; the two overrides existing at the time only survived
-- because they were on transactions older than the window.
--
-- Referential integrity is app-level instead: set_transaction_override
-- already verifies the transaction exists before writing, manual-account
-- deletion never touches transactions, and the transactions_with_category
-- view LEFT JOINs overrides by id, so a hypothetical orphan row is inert.
-- The FK to categories(id) stays: categories are never upserted (plain
-- in-place UPDATEs don't trip the parent-side check).
--
-- DuckDB has no ALTER TABLE ... DROP CONSTRAINT, so this is a rebuild.
-- The rename preserves the name the view binds to (views late-bind).

CREATE TABLE transaction_overrides_new (
    transaction_id VARCHAR PRIMARY KEY,
    category_id INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (category_id) REFERENCES categories(id)
);
INSERT INTO transaction_overrides_new (transaction_id, category_id, created_at)
    SELECT transaction_id, category_id, created_at FROM transaction_overrides;
DROP TABLE transaction_overrides;
ALTER TABLE transaction_overrides_new RENAME TO transaction_overrides;
