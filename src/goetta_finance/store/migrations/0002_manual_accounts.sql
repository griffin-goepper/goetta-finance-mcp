-- DuckDB's ALTER TABLE ADD COLUMN does not support inline NOT NULL constraints
-- ("Adding columns with constraints not yet supported"). DEFAULT alone is
-- supported and backfills existing rows. The application layer treats
-- is_manual NULL as False (``bool(None) is False``) and ``upsert_accounts``
-- always writes an explicit boolean, so missing-NOT-NULL is not load-bearing.
ALTER TABLE accounts ADD COLUMN is_manual BOOLEAN DEFAULT FALSE;
