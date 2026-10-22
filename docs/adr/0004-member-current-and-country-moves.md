# 0004. Country moves: maintain member_current, derive country tables from it

## Context
A member who moves country appears in a new country's data while an older record still exists for
the old country. Only the newest record may count, so the member must not remain in the old
country's table.

## Decision
- `member_current` holds exactly one row per `member_key` (ADR 0002), maintained with `MERGE` in Snowflake
  (a full `QUALIFY` rebuild in DuckDB), using the ordering from ADR 0003.
- Country tables are derived from `member_current` and never updated independently. A member can
  therefore be in only one country table; a moved member disappears from the old one on the next
  run.
- The change is recorded in `member_country_history (member_key, member_id, old_country_code,
  new_country_code, effective_file_ts, batch_id)`: one row per country change, keyed by
  `member_key`; `member_id` is kept as the plain ID column.

## Consequences
- No separate "delete from old country" logic that could drift out of sync.
- Snowflake's `MERGE` has no `WHEN NOT MATCHED BY SOURCE`, so the removal is driven by a change set
  comparing the new and previous `member_current` (a stream on it).
- Country tables hold current state only; history lives in `member_country_history` and raw.
- Post-run checks: no `member_key` in two country tables, and the country table counts sum to the
  `member_current` count.
