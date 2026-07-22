-- Rule-match cache: stop re-running every rule against every transaction
-- on every read of transactions_with_category.
--
-- Why: the 0004/0005/0009 view resolved rule matches inline — a
-- transactions x category_rules join of LIKE/regexp predicates evaluated
-- on EVERY scan. At 9k transactions x 247 rules that was ~2.2M pattern
-- matches (~0.7s of one core) per query, and /goals ran the scan once
-- per spending cap. The cost grows as transactions x rules — unbounded
-- over the life of the database.
--
-- What changes: the matched rule per transaction now lives in
-- category_match_cache, maintained WRITE-THROUGH by the store inside the
-- same locked call as every mutation of the matching inputs:
--   * upsert_transactions      -> targeted refresh of the upserted ids
--   * delete_stale_pending     -> deletes the ids' cache rows
--   * add_rule / remove_rule   -> full rebuild (rules change rarely)
--   * init()                   -> full rebuild after applying migrations
-- The match SQL itself has ONE home: _match_cache_insert_sql in
-- duckdb_store.py (predicates byte-equivalent to 0009's view: contains =
-- case-insensitive substring on description, regex = regexp_matches on
-- description, amount bounds refine as half-open [min, max) on
-- abs(amount), lowest priority wins with id as tie-break).
--
-- What does NOT change — the retroactivity contract (CLAUDE.md "Don't
-- add a category_id column to transactions"):
--   * Overrides stay read-time: the view still LEFT JOINs
--     transaction_overrides directly, so an override applies with no
--     cache involvement at all.
--   * Rule adds/removes still apply retroactively to every transaction —
--     the rebuild happens synchronously inside the same add_rule /
--     remove_rule call, so no reader can observe a rule without its
--     retroactive effect.
--   * Category renames/recolors stay read-time: the cache stores
--     category_id; names/colors resolve through the categories join
--     below.
-- This is a CACHE of the rule-match computation, not a write-time
-- category assignment on transactions — which remains forbidden.
--
-- This migration ships the table EMPTY on purpose: init() rebuilds the
-- cache whenever it has just applied migrations (this one included), so
-- the population SQL lives only in duckdb_store.py rather than being
-- duplicated here. Between this migration and the end of init() nothing
-- reads the view.
--
-- No FK to transactions(id): the parent-side delete+insert of
-- INSERT ... ON CONFLICT DO UPDATE in upsert_transactions would trip it
-- on every re-synced row — the exact failure 0011 removed from
-- transaction_overrides. The FK to categories(id) is safe: categories
-- are never upserted (0011's note), never deleted, and rule writes
-- rebuild the whole cache anyway. The PRIMARY KEY is a constraint index,
-- which deserializes correctly (0010's explicit-ART-index bug does not
-- apply).
CREATE TABLE category_match_cache (
    transaction_id VARCHAR PRIMARY KEY,
    category_id INTEGER NOT NULL,
    FOREIGN KEY (category_id) REFERENCES categories(id)
);

-- Rebuild the view over the cache (the established full-body re-issue
-- pattern — 0005 and 0009 did the same). Output columns and resolution
-- order are identical to 0009's version: override > matched rule >
-- 'Uncategorized', with account_is_hidden from the accounts join. Only
-- the source of "matched rule" changes: the cache table instead of the
-- inline matched_rule CTE.
CREATE OR REPLACE VIEW transactions_with_category AS
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
LEFT JOIN category_match_cache mc ON mc.transaction_id = t.id
LEFT JOIN categories rule_cat ON rule_cat.id = mc.category_id;
