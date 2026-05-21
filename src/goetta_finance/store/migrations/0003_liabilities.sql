-- See 0002_manual_accounts.sql for why NOT NULL is omitted (DuckDB's ALTER
-- TABLE does not yet support adding columns with constraints). DEFAULT FALSE
-- backfills existing rows; the Python layer reads NULL as False
-- (``bool(None) is False``) and ``upsert_accounts`` always writes an
-- explicit boolean.
ALTER TABLE accounts ADD COLUMN is_liability BOOLEAN DEFAULT FALSE;
