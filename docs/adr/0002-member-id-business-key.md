# 0002. The member key is the source-namespaced member_key (source_system:member_id)

## Context
The spec names Member Name as the key column. Names repeat: "Mike" already appears in both the USA
and AUS files with different tiers, so matching on name would merge different people or split one.

The IND, USA and AUS extracts each number their members 1, 2, 3, so the bare `member_id` collides
across the three files (9 rows, 3 distinct IDs). The first version of this ADR used `member_id`
alone; that would merge three different people into one.

## Decision
- The business key is `member_key = source_system + ':' + member_id` (for example `IND:1`,
  `USA:1`, `AUS:1`). `source_system` is the extract the row came from (`IND`, `USA`, `AUS`); for
  the main pipe-delimited member feed it is that feed's own source system name.
- `member_id` stays as a plain column holding the ID as sent by the source. It is no longer unique
  on its own.
- "Latest record wins" (ADR 0003), `member_current` and the country tables use `member_key`.
- Rows with a missing `member_id` cannot form a key and go to quarantine as `MISSING_MEMBER_ID`.
- Member Name is not used as a key. This contradicts the spec and is flagged to the spec owner.

### Master feed and the extracts
- The `.dat` member feed (`source_system = 'SKYPOINTS'`) is the **master feed**. "Latest record
  wins" (ADR 0003) runs on its `member_key`, for example `SKYPOINTS:223457`.
- IND, USA and AUS are separate sources with their own keys (`IND:1`, `USA:1`, `AUS:1`). They are
  loaded and kept apart; **linking them to master members is out of scope for now**. No rule
  merges, matches or deduplicates a member across source systems.
- `member_key` is the primary and merge key of `member_current`; `member_id` stays a plain column.

### Redemptions
The redemption JSON carries a bare `member_id`, with no source system. Redemptions join to the
member feed IDs, that is, to members whose `source_system` is the main member feed. A redemption
with no matching member there is an orphan: it is kept with `member_matched = false` and also
recorded in quarantine as `UNMATCHED_MEMBER`, never dropped. A redemption `member_id` is never
matched to a member from the IND, USA or AUS extracts, because `1` there is a different person.

## Consequences
- IDs 1, 2 and 3 from different countries no longer collide.
- A member who appears in two extracts is two members unless a rule links them. This is accepted;
  linking would be guessing. A member who moves country inside the main feed keeps the same
  `member_id` and the same source system, so "latest wins" still moves them.
- Redemption `member_id` `1` in the sample file is an orphan unless the main member feed has a
  member `1`; that is correct, not an error.
- `member_current`, the history table and every country table need a `member_key` column. The DDL
  currently keys on `member_id` and has to be updated; this ADR does not change the DDL.
- Queries and tests that group by `member_id` alone must use `member_key`.
