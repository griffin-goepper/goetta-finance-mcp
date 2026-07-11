-- Drop every explicit (non-constraint) ART index.
--
-- Why: DuckDB 1.5.x has an unresolved bug in explicit non-unique ART
-- indexes in their deserialized-from-disk state. After enough incremental
-- upsert churn over heavily duplicated keys (goetta stamps `posted` at
-- 12:00:00 noon, so idx_transactions_posted packs ~all rows into a few
-- dozen keys), the serialized index reaches a form where ANY
-- INSERT ... ON CONFLICT DO UPDATE on the table fails inside the
-- index-append revert path with "Failed to delete all rows from index.
-- Only deleted 0 out of 1 rows." On duckdb 1.5.2 that aborted the whole
-- process (0xC0000409); on 1.5.4 it raises FatalException and invalidates
-- the database for the process. Live incidents: 2026-07-02 and 2026-07-06.
-- Table data is never damaged; the failure is index-only.
--
-- These indexes were also pure overhead: at this database's scale the
-- planner answers point lookups and range scans with table scans (verified
-- via EXPLAIN — the indexes were not chosen), so dropping them costs
-- nothing and removes the entire detonation surface. PRIMARY KEY and
-- FOREIGN KEY constraint indexes remain; they deserialize correctly.
--
-- DROP INDEX doubles as the repair: it discards the poisoned ART, so a
-- database currently in the failing state is healed by this migration.
--
-- If a future dataset genuinely needs one of these indexes back, re-adding
-- it must wait until the upstream bug is fixed (track duckdb/duckdb — the
-- strict revert check is new in 1.5.0, PR #20430) and must re-verify with
-- EXPLAIN that the planner even uses it.
--
-- Note: keep the daemon's checkpoint-after-migrations behavior (init()
-- checkpoints) — WAL replay of DROP INDEX is itself buggy upstream
-- (duckdb/duckdb#22044), so this DDL must not linger in the WAL.

DROP INDEX idx_transactions_posted;
DROP INDEX idx_transactions_account_posted;
DROP INDEX idx_category_rules_priority;
