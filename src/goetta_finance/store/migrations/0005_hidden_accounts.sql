-- Adds is_hidden to accounts and rebuilds transactions_with_category to
-- expose it. Two changes ship together because every read path that
-- filters hidden accounts needs the view JOIN to surface the flag —
-- splitting them would leave a half-functional state where the column
-- exists but the view can't see it.
--
-- See 0002_manual_accounts.sql / 0003_liabilities.sql for why NOT NULL
-- is omitted (DuckDB ALTER TABLE does not yet support adding columns
-- with constraints). DEFAULT FALSE backfills existing rows; the Python
-- layer reads NULL as False (``bool(None) is False``) and
-- ``upsert_accounts`` always writes an explicit boolean.
ALTER TABLE accounts ADD COLUMN is_hidden BOOLEAN DEFAULT FALSE;

-- Rebuild the categorization view with a JOIN to accounts so its rows
-- carry ``account_is_hidden``. Read paths (get_transactions_with_category,
-- query_spending_by_category, dashboard) filter on this column for their
-- default behavior; raw sql_query callers see the column and can choose
-- to filter or not.
--
-- The JOIN to accounts is INNER (``JOIN``), not LEFT JOIN: every row in
-- transactions has an account_id FK constraint that points at accounts,
-- so the INNER JOIN cannot drop transactions in practice. The
-- ``test_view_returns_all_transactions_after_account_join`` regression
-- test pins this — if a future change makes the FK nullable or breaks
-- a fixture so a transaction's account_id no longer matches, the test
-- catches it instead of the dashboard silently losing rows.
CREATE OR REPLACE VIEW transactions_with_category AS
WITH text_to_match AS (
    SELECT id, description AS text FROM transactions
),
matched_rule AS (
    SELECT
        t.id AS transaction_id,
        r.category_id,
        ROW_NUMBER() OVER (PARTITION BY t.id ORDER BY r.priority ASC, r.id ASC) AS rn
    FROM text_to_match t
    JOIN category_rules r ON
        (r.match_type = 'contains' AND lower(t.text) LIKE '%' || lower(r.pattern) || '%')
        OR (r.match_type = 'regex' AND regexp_matches(t.text, r.pattern))
)
SELECT
    t.id,
    t.account_id,
    t.posted,
    t.transacted_at,
    t.amount,
    t.description,
    t.payee,
    t.memo,
    t.pending,
    t.extra,
    t.created_at,
    COALESCE(ov_cat.name, rule_cat.name, 'Uncategorized') AS category,
    COALESCE(ov_cat.display_color, rule_cat.display_color) AS category_color,
    COALESCE(a.is_hidden, FALSE) AS account_is_hidden
FROM transactions t
JOIN accounts a ON a.id = t.account_id
LEFT JOIN transaction_overrides ov ON ov.transaction_id = t.id
LEFT JOIN categories ov_cat ON ov_cat.id = ov.category_id
LEFT JOIN matched_rule mr ON mr.transaction_id = t.id AND mr.rn = 1
LEFT JOIN categories rule_cat ON rule_cat.id = mr.category_id;
