-- Adds is_spending to categories so non-spending categories (Transfers,
-- Income, future categories like "Loan principal paydown") are excluded
-- from spending_by_category and the dashboard's "By category" pie by
-- default.
--
-- Why this exists: pre-0006, the spending_by_category SQL had a
-- hardcoded `category <> 'Income'` filter — single-purpose, hardcoded,
-- doesn't extend to Transfers. Categorizing inter-account transfers
-- (Apple GS Savings, Fidelity contributions, CC payoffs) as Transfers
-- would make the pie chart 49% Transfers, which is technically correct
-- but misleads: transfers aren't spending. The schema-level flag is the
-- principled answer — a category is or isn't part of spending math,
-- and that's a property of the category itself.
--
-- The migration matches the existing is_X BOOLEAN DEFAULT pattern from
-- 0002 (is_manual), 0003 (is_liability), 0005 (is_hidden). DEFAULT TRUE
-- because most categories ARE spending; the migration immediately flips
-- the two known non-spending defaults (Transfers and Income).
--
-- See 0002_manual_accounts.sql / 0003_liabilities.sql for why NOT NULL
-- is omitted (DuckDB ALTER TABLE does not yet support adding columns
-- with constraints). DEFAULT TRUE backfills existing rows.
ALTER TABLE categories ADD COLUMN is_spending BOOLEAN DEFAULT TRUE;

-- Flip the two non-spending defaults. New non-spending categories the
-- user adds later go through `goetta-finance category add --no-spending`
-- (or `category set-spending <name> false` for existing ones).
UPDATE categories SET is_spending = FALSE WHERE name IN ('Transfers', 'Income');
