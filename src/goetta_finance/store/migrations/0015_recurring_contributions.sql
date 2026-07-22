-- Declared recurring contributions on contribution goals (0014):
-- "payroll deducts $X every biweekly paycheck straight into this
-- account; no feed can ever see it, so accrue it by calculation."
--
-- The triple declares a schedule, not observations: paydays are
-- generated from recurring_anchor at recurring_interval
-- ('weekly'|'biweekly'|'monthly'; monthly = the anchor's day-of-month
-- clamped to each month's end), extending BOTH directions from the
-- anchor, and each payday at or before "now" accrues recurring_amount
-- into the goal's current period and its history bucket (goals.py owns
-- the math, read-time as ever).
--
-- Plain ALTERs — no CHECK changes needed on kind, so no table rebuild
-- (the 0014 rebuild exists because kind CHECKs can't be altered; these
-- columns need no CHECK edits). DuckDB ALTER TABLE can't add table-level
-- CHECKs either, so the shape rules for the new columns
-- (all-three-or-none, contribution kind only, amount > 0, interval
-- whitelist) are enforced in the application layer only — validators.py
-- and DuckDBStore.add_goal — a deliberate deviation from the goals
-- table's other constraints, noted in SQL_SCHEMA_HINT.

ALTER TABLE goals ADD COLUMN recurring_amount DECIMAL(18,2);
ALTER TABLE goals ADD COLUMN recurring_interval TEXT;
ALTER TABLE goals ADD COLUMN recurring_anchor DATE;
