-- Transaction categorization: read-time resolution via a SQL view.
--
-- Three tables, one view, default seeds. Categorization is resolved at
-- query time (override > rule > 'Uncategorized'), NOT stored on the
-- transactions table — so adding/editing a rule applies retroactively to
-- every transaction without any data migration. Same retroactive-by-design
-- shape as is_liability (migration 0003). Do not "optimize" this by adding
-- a category_id column to transactions; the retroactivity is the feature.
--
-- See CLAUDE.md "Don'ts" for the security note on category_rules.pattern:
-- it is an MCP-reachable write surface (Claude can land a rule via the
-- CLI tool exposure path), so patterns must be validated through a
-- timeout-bounded re.compile check at insert time. That check lives in
-- the Python CLI layer, not in this migration.

CREATE SEQUENCE categories_id_seq START 1;
CREATE TABLE categories (
    id INTEGER PRIMARY KEY DEFAULT nextval('categories_id_seq'),
    name TEXT NOT NULL UNIQUE,
    display_color TEXT,
    is_default BOOLEAN DEFAULT FALSE
);

CREATE SEQUENCE category_rules_id_seq START 1;
CREATE TABLE category_rules (
    id INTEGER PRIMARY KEY DEFAULT nextval('category_rules_id_seq'),
    category_id INTEGER NOT NULL REFERENCES categories(id),
    match_type TEXT NOT NULL CHECK (match_type IN ('contains', 'regex')),
    pattern TEXT NOT NULL,
    priority INTEGER NOT NULL DEFAULT 100,
    is_default BOOLEAN DEFAULT FALSE
);
CREATE INDEX idx_category_rules_priority ON category_rules(priority ASC, id ASC);

CREATE TABLE transaction_overrides (
    transaction_id TEXT PRIMARY KEY REFERENCES transactions(id),
    category_id INTEGER NOT NULL REFERENCES categories(id),
    created_at TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Read-time resolution: override > rule (by ascending priority) > 'Uncategorized'.
--
-- The text_to_match CTE is the single change site for any future broadening
-- of what rules match against. Today: description only. Payee is excluded
-- because it's often derived from description (duplicate matching effort);
-- memo is excluded by design — per docs/SECURITY_AUDIT_2026-05.md memos
-- can carry third-party attacker-controlled text and are the documented
-- prompt-injection surface, not a place to anchor categorization.
CREATE VIEW transactions_with_category AS
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
    COALESCE(ov_cat.display_color, rule_cat.display_color) AS category_color
FROM transactions t
LEFT JOIN transaction_overrides ov ON ov.transaction_id = t.id
LEFT JOIN categories ov_cat ON ov_cat.id = ov.category_id
LEFT JOIN matched_rule mr ON mr.transaction_id = t.id AND mr.rn = 1
LEFT JOIN categories rule_cat ON rule_cat.id = mr.category_id;

-- Default categories (14). is_default = TRUE marks them so the CLI can refuse
-- destructive operations on defaults without --force, same shape as
-- account remove on non-manual accounts.
INSERT INTO categories (name, display_color, is_default) VALUES
    ('Groceries',     '#27ae60', TRUE),
    ('Dining',        '#e67e22', TRUE),
    ('Transportation','#2980b9', TRUE),
    ('Gas',           '#34495e', TRUE),
    ('Utilities',     '#16a085', TRUE),
    ('Subscriptions', '#8e44ad', TRUE),
    ('Rent/Mortgage', '#c0392b', TRUE),
    ('Healthcare',    '#e84393', TRUE),
    ('Entertainment', '#fdcb6e', TRUE),
    ('Shopping',      '#0984e3', TRUE),
    ('Travel',        '#00b894', TRUE),
    ('Transfers',     '#7f8c8d', TRUE),
    ('Income',        '#2ecc71', TRUE),
    ('Uncategorized', '#bdc3c7', TRUE);

-- Default rules. Priority lower = higher precedence. Specific rules get
-- priority 10–50 so they beat generic 100s. Income gets NO default rule
-- by design — it must be assigned via explicit override or a user-added
-- rule. This keeps spending_by_category honest by default (the tool
-- excludes Income unless include_income=True is passed).
--
-- Transfers uses a regex anchor on 'TRANSFER' anywhere in the description.
-- Other defaults use 'contains' (case-insensitive via the view's lower()).
INSERT INTO category_rules (category_id, match_type, pattern, priority, is_default)
SELECT id, 'contains', pattern, priority, TRUE FROM (VALUES
    ('Groceries',      'KROGER',           20),
    ('Groceries',      'TRADER JOE',       20),
    ('Groceries',      'WHOLE FOODS',      20),
    ('Groceries',      'COSTCO',           20),
    ('Groceries',      'SAFEWAY',          20),
    ('Groceries',      'ALDI',             20),
    ('Groceries',      'PUBLIX',           20),
    ('Dining',         'STARBUCKS',        20),
    ('Dining',         'CHIPOTLE',         20),
    ('Dining',         'DOORDASH',         20),
    ('Dining',         'UBER EATS',        20),
    ('Dining',         'GRUBHUB',          20),
    ('Dining',         'MCDONALDS',        20),
    ('Dining',         'CHICK-FIL-A',      20),
    ('Gas',            'SHELL',            20),
    ('Gas',            'EXXON',            20),
    ('Gas',            'BP ',              20),
    ('Gas',            'CHEVRON',          20),
    ('Gas',            'MARATHON',         20),
    ('Subscriptions',  'SPOTIFY',          20),
    ('Subscriptions',  'NETFLIX',          20),
    ('Subscriptions',  'HULU',             20),
    ('Subscriptions',  'DISNEY PLUS',      20),
    ('Subscriptions',  'AMAZON PRIME',     20),
    ('Transportation', 'UBER ',            30),
    ('Transportation', 'LYFT',             30),
    ('Healthcare',     'CVS PHARMACY',     20),
    ('Healthcare',     'WALGREENS',        20),
    ('Shopping',       'AMAZON.COM',       50),
    ('Shopping',       'TARGET',           50),
    ('Shopping',       'WALMART',          50),
    ('Entertainment',  'STEAM',            20),
    ('Entertainment',  'AMC ',             20),
    ('Travel',         'DELTA AIR',        20),
    ('Travel',         'UNITED AIRLINES',  20),
    ('Travel',         'AIRBNB',           20),
    ('Travel',         'MARRIOTT',         20)
) AS seed(category_name, pattern, priority)
JOIN categories ON categories.name = seed.category_name;

INSERT INTO category_rules (category_id, match_type, pattern, priority, is_default)
SELECT id, 'regex', pattern, priority, TRUE FROM (VALUES
    ('Transfers', '(?i)transfer', 10)
) AS seed(category_name, pattern, priority)
JOIN categories ON categories.name = seed.category_name;
