-- Repair the dangling catalog dependency migration 0011 left on `categories`.
--
-- 0011 rebuilt `transaction_overrides` the create-copy-drop-RENAME way. The
-- rename left DuckDB's foreign-key metadata on the PARENT side (`categories`)
-- still naming the staging table `transaction_overrides_new`, which no longer
-- exists. Every statement that makes DuckDB validate a constraint on
-- `categories` then dereferences that name and fails with:
--
--     Catalog Error: Table with name transaction_overrides_new does not exist!
--
-- DELETE of one row, DELETE of all rows, TRUNCATE, INSERT ... ON CONFLICT DO
-- UPDATE, and UPDATE of the UNIQUE `name` column are all affected — even for a
-- category no rule, override or goal references. The record is persisted in the
-- catalog, so closing and reopening the database does not clear it. Plain
-- INSERT and UPDATEs that touch no constrained column are unaffected, which is
-- why `set_category_spending` still works and why nothing surfaced this until
-- backup/restore needed to clear the table.
--
-- 0014's comment documents the same class of bug (it hit `accounts` via
-- `goals_new` while that migration was being written) and 0014 deliberately
-- avoided reintroducing it -- but the damage 0011 had already done was never
-- repaired. Every database that has run 0011 carries it, including fresh
-- installs, because migrations replay in order.
--
-- The repair: make the phantom real, then remove it through the supported
-- path. Recreating a table under the name the catalog still believes in and
-- then DROPping it deregisters the dependency properly -- DROP is the clean
-- removal path the rename bypassed. Verified to survive close-and-reopen, and
-- to leave row counts in `categories`, `category_rules`,
-- `transaction_overrides`, `goals` and `category_match_cache` untouched.
--
-- The stand-in is deliberately identical to the table 0011 created, FK
-- included: the FK is what registers the dependency, so it has to be present
-- for DROP to clear it. Nothing reads or writes the stand-in -- it exists for
-- two statements inside one transaction.
--
-- If `transaction_overrides_new` somehow already exists (a half-applied 0011),
-- this CREATE fails and the migration rolls back rather than dropping a table
-- that might hold rows. Failing loudly is correct there.
--
-- What this does NOT change: a category that rules, overrides or goals still
-- reference cannot be deleted or renamed. That is DuckDB's ordinary
-- foreign-key rule (and an UPDATE of a UNIQUE column is internally a
-- delete+insert, so it trips the same parent-side check) -- correct behavior,
-- not the defect. Any future `category delete` / `category rename` command
-- needs the same reference guard `delete_account` already has.

CREATE TABLE transaction_overrides_new (
    transaction_id VARCHAR PRIMARY KEY,
    category_id INTEGER NOT NULL,
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP,
    FOREIGN KEY (category_id) REFERENCES categories(id)
);

DROP TABLE transaction_overrides_new;
