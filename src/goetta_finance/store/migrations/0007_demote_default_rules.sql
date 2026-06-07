-- Demote 0004's merchant-specific default rules to a universal minimal
-- seed. The 38 rules 0004 shipped were tuned to US merchants (Kroger,
-- Starbucks, Shell, DoorDash, etc.) — a stranger installing tomorrow
-- with a non-US bank sees zero matches AND has to figure out which
-- defaults to delete to clean up the noise.
--
-- This migration deletes every is_default=TRUE rule EXCEPT the small
-- universal-by-merchant-name set:
--   - `(?i)transfer` (regex) — the only pattern that's truly portable
--     across banks. Most banks include "transfer" in inter-account
--     descriptions regardless of locale.
--   - Spotify, Netflix, Hulu, Disney Plus, Amazon Prime — global
--     subscriptions with consistent merchant names worldwide. Even
--     these will miss some bank formats (e.g. "DISNEY+ STREAMING") but
--     they hit often enough to be a positive day-one experience.
--
-- The 14 default categories + color palette stay (those are universal).
-- User-added rules (is_default=FALSE) are untouched — anyone who's
-- curated their own rules through the CLI or MCP keeps everything.
--
-- See feedback_stranger_test_for_open_source_slices for the editorial
-- principle that motivated this change. README + CUSTOMIZATION.md
-- document the intentional-minimalism: the system ships expecting you
-- to curate via `top_uncategorized_patterns` + Claude conversation,
-- not via pre-baked defaults.

DELETE FROM category_rules
WHERE is_default = TRUE
  AND NOT (
        pattern = '(?i)transfer'
     OR pattern = 'SPOTIFY'
     OR pattern = 'NETFLIX'
     OR pattern = 'HULU'
     OR pattern = 'DISNEY PLUS'
     OR pattern = 'AMAZON PRIME'
  );
