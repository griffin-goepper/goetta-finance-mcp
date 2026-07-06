-- Optional amount bounds on categorization rules.
--
-- Two nullable DECIMAL(18,2) columns on category_rules; NULL means
-- unbounded on that side, so every pre-existing rule keeps exactly its
-- current behavior (no backfill, no seed changes — bounds are user-state
-- per the stranger test; no default rule ships with bounds).
--
-- Semantics, enforced by the view predicate below:
--   * Bounds compare against abs(t.amount) — sign-agnostic, so a $12
--     refund (+12.00) resolves to the same category as the $12 purchase
--     and nets correctly in spending totals.
--   * Half-open interval: min_amount <= abs(amount) < max_amount.
--     A rule with max_amount = 20 and a complementary rule with
--     min_amount = 20 have no gap and no overlap at exactly 20.00.
--   * Bounds only REFINE a pattern match; pattern remains required.
--     A rule never matches on amount alone.
--
-- NOT NULL is deliberately absent (unlike the is_X boolean pattern):
-- NULL is the meaningful "unbounded" value, not a missing default.
-- One ALTER per statement (DuckDB-safe).
ALTER TABLE category_rules ADD COLUMN min_amount DECIMAL(18,2);
ALTER TABLE category_rules ADD COLUMN max_amount DECIMAL(18,2);

-- Rebuild the categorization view (the established pattern — 0005 did
-- the same to add account_is_hidden; re-issue the FULL body). Two
-- deltas from 0005's version:
--   1. text_to_match (the documented single change site for broadening
--      what rules match against) now also carries amount.
--   2. The rule JOIN ANDs the amount-bounds predicates against the
--      pattern-match OR group. The parentheses around the OR group are
--      load-bearing: without them the bounds would AND onto only the
--      regex arm and 'contains' rules would bypass them.
-- Everything else — accounts JOIN, account_is_hidden, resolution order,
-- rn tie-break — is byte-identical to 0005.
CREATE OR REPLACE VIEW transactions_with_category AS
WITH text_to_match AS (
    SELECT id, description AS text, amount FROM transactions
),
matched_rule AS (
    SELECT
        t.id AS transaction_id,
        r.category_id,
        ROW_NUMBER() OVER (PARTITION BY t.id ORDER BY r.priority ASC, r.id ASC) AS rn
    FROM text_to_match t
    JOIN category_rules r ON
        (
            (r.match_type = 'contains' AND lower(t.text) LIKE '%' || lower(r.pattern) || '%')
            OR (r.match_type = 'regex' AND regexp_matches(t.text, r.pattern))
        )
        AND (r.min_amount IS NULL OR abs(t.amount) >= r.min_amount)
        AND (r.max_amount IS NULL OR abs(t.amount) < r.max_amount)
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
