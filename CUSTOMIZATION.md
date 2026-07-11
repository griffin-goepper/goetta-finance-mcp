# Customizing goetta-finance

A map of everything you can tune, where it lives, and how to change it. The design split: **your data and preferences live in your DB and config home** (`~/.local/share/goetta-finance/` by default, or `$GOETTA_FINANCE_HOME`); **the codebase ships minimal, broadly-applicable defaults** with the extension paths documented here.

## Categorization rules

What maps transaction descriptions to categories. The shipped defaults are deliberately tiny (a `(?i)transfer` regex plus five global streaming subscriptions) — expect to add your own.

```bash
# See what shipped vs. what you added
goetta-finance category default-rules        # is_default=TRUE seed
goetta-finance category list                 # everything, with txn + rule counts

# Add a rule (applies retroactively to all matching transactions)
goetta-finance category set-rule Utilities --match contains --pattern "Dukeenergy"
goetta-finance category set-rule Transfers --match regex --pattern "^contribution$" --priority 5

# Remove one (defaults need --force + typed-pattern confirmation)
goetta-finance category remove-rule <rule_id>
```

Or skip the CLI entirely: ask Claude *"what's still uncategorized?"* — the `top_uncategorized_patterns` MCP tool surfaces the biggest gaps, and `add_category_rule` / `remove_category_rule` / `categorize_transaction` apply your decisions from the conversation (default seeded rules can only be removed via the CLI's `--force` path above).

**Finding what needs a rule:** the dashboard's "By category" page shows the Uncategorized share; `top_uncategorized_patterns` (via Claude) or the suggested commands in its output close the loop.

## Description-prefix strip list (`prefixes.txt`)

`top_uncategorized_patterns` normalizes descriptions before grouping, so `"Web Authorized Pmt Spotify"` and `"Spotify"` count as one merchant. The strip list lives at:

```
$GOETTA_FINANCE_HOME/prefixes.txt      (~/.local/share/goetta-finance/prefixes.txt)
```

One regex per line, `#` comments, matched case-insensitively against the start of each description. The shipped default contains only the three universal payment-processor prefixes (`TST*` Toast, `SQ *` Square, `AplPay` Apple Pay) plus commented-out examples for common US bank templates. **Uncomment or add the prefixes your bank uses** — check a few raw descriptions via `goetta-finance status` or the dashboard's Transactions page to see your institution's wrapper text.

The file is written once by `goetta-finance init` and never overwritten — your edits are safe across upgrades. Invalid regex lines are skipped with a warning.

## Categories

```bash
goetta-finance category list                          # see all 14 defaults + yours
goetta-finance category add --name "Pets" --color "#8e44ad"
goetta-finance category add --name "Charity" --no-spending   # excluded from spending pie
goetta-finance category set-spending Travel false     # flip an existing one
```

`--no-spending` / `set-spending false` marks a category as not-consumption (like the built-in `Transfers` and `Income`): its transactions are excluded from `spending_by_category` and the dashboard pie by default.

## Account flags

```bash
goetta-finance account set-hidden <id> true       # exclude from all default views
goetta-finance account set-liability <id> true    # subtract from net worth
goetta-finance account add --currency EUR ...     # manual accounts in any ISO 4217 currency
```

All three flags survive syncs — SimpleFIN can't overwrite them. `account list` shows hidden accounts with a `[hidden]` tag so you can find them to unhide.

## Goals

Pure user-state (nothing is seeded): spending caps per category/period and balance targets per account, evaluated at read time.

```bash
goetta-finance goal add-spending Groceries --limit 400 --period month
goetta-finance goal add-balance <account-id> --target 10000 --direction at_least --by 2027-06-01
goetta-finance goal list
goetta-finance goal remove <id>
```

Goals live in the `goals` table in `data.duckdb` and travel with the database. Deleting an account that a goal references is refused until you remove the goal.

## Dashboard colors

Category badge colors come from each category's `display_color` (set at `category add --color`, or update via `sql_query`-visible `categories.display_color`). Page-level styling lives in `src/goetta_finance/web/static/styles.css` — the badge/table/nav classes are plain CSS, no build step.

## Currency display

Aggregate labels (net worth, chart axes) derive from your accounts automatically: single-currency installs see that currency; mixed-currency installs see "mixed" (cross-currency totals are raw sums — no FX conversion yet). Per-account rows always show the account's own currency.

## Where everything lives

```
~/.local/share/goetta-finance/
├── config.json        # SimpleFIN access URL (sensitive!), backend choice
├── prefixes.txt       # description-prefix strip list (yours to edit)
└── data.duckdb        # all data: accounts, transactions, categories, rules, overrides
```

Override the directory with `GOETTA_FINANCE_HOME`. Back up by copying the directory; everything is in those three files. Your rules, categories, overrides, account flags, and goals are rows in `data.duckdb` — they travel with the database.
