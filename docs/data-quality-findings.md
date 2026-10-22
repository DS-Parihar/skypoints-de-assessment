# Data quality findings

What I found after profiling every file in `data/raw` (see `scripts/profile_sources.py`).

## Member feed (pipe-delimited sample from the brief)

| Issue | Why it matters | What I'll do |
|---|---|---|
| Spec says Member Name is the key column | Names repeat (there are already two "Mike"s across files) | Use Member ID as the key, flag it as a spec question |
| Header has 10 fields, spec has 11 (Post Code missing) | Positional parsing would shift columns | Check the header against the spec and fail the file if it doesn't match |
| DOB `03051985` became `3051985` in staging | Leading zero lost, so the date can't be read reliably | Keep raw as text, convert dates only in staging |
| DOB format not documented | 5 March or 3 May? Every age would be wrong | Assume MMDDYYYY, write it down, flag values that don't parse |
| Agent_Name is "Sam" in the file but blank in staging | The existing ETL lost data | Compare raw vs staging column by column |
| Staging column is called `County` | Typo for Country | Use `country` in my tables |
| Country codes mixed (`USA`, `IND`, `CAN`, `PHIL`, `AU`) | Routing by country would miss or split members | Map everything to ISO-3 through a lookup table |
| Country isn't mandatory but drives routing | A blank country would lose the member | Send to an "unassigned" table, never drop |
| IDs `22345` and `2256` are shorter than the others | Might be truncated | Warning only, the spec allows variable length |
| No trailer record | Can't prove the whole file arrived | Suggest a row-count trailer, reconcile counts after load |

## Country files (IND.csv, USA.csv, AUS.xlsx)

| File | Issue |
|---|---|
| All three | Different column names and layouts, none match the member feed spec |
| All three | IDs are 1, 2, 3 in each file, so they collide. Need source system + ID as the key |
| IND | Dates are M/D/YYYY with no zero padding. Month-first is confirmed by `8/13/1982` |
| IND | Extra column "Individual or Corporate" that isn't in the spec |
| USA | Dates like `1052022` have no separators or padding. `1052022` (Jan 5 or Oct 5) and `1152022` (Jan 15 or Nov 5) are ambiguous |
| USA | No DOB column, so Age can't be calculated |
| AUS | DOB is the text `NULL` for one member |
| AUS | Enrollment date `2021-13-13` has month 13, so it's invalid |
| AUS | Dates stored as text in Excel, not real dates |
| USA + AUS | A "Mike" in both with different tiers, so matching on name would be unsafe |
| All three | Every flight date is in 2022, so every member counts as stale |

## Redemptions JSON

- Clean in the sample: dates are YYYYMMDD, statuses are COMPLETED / PENDING.
- A redemption for member `1` (kept as the fixture `tests/fixtures/redemptions/member_1_orphan/`) shows the ID problem: ID 1 also exists in IND, USA and AUS, so a bare `member_id` can't be matched to one person. Redemptions are joined to the member feed by `member_key` (`SKYPOINTS:<id>`), and one with no matching member is an orphan.
- Every other check is now a rule (`RD001` to `RD006`), applied when the feed is loaded. Rules are in `sql/duckdb/transform/45_stage_redemptions.sql` (Snowflake: `sql/snowflake/06_redemptions.sql`) and described at the top of `src/skypoints/ingest/redemptions.py`; each one has tests in `tests/integration/test_redemption_load.py`.

| Rule | Problem | What it does |
|---|---|---|
| `RD001` | Member with an empty `redemptions` array | No rows, but the member is recorded as a warning (`DQ.ROW_FINDING`) and kept in raw |
| `RD002` | Duplicate transaction ID | The newest version (feed date, then file time, then position) is kept; the older one goes to `DQ.QUARANTINE` as `DUPLICATE_TXN_OLDER`, never dropped silently |
| `RD003` | Negative `miles_redeemed` | Row quarantined with its original values; not staged |
| `RD004` | Status that is not `COMPLETED` or `PENDING` | Row quarantined; the allowed statuses are config (`RedemptionConfig`) |
| `RD005` | `txn_date` after `feed_date` | Warning only; the row is staged with `dq_txn_after_feed` set |
| `RD006` | A required value missing or unreadable (member ID, transaction ID, a date, miles) | Row quarantined |

- Members with no profile: not a rejection. The redemption is kept, and `MART.MEMBER_REDEMPTION_SUMMARY` shows it with `member_matched = false` (tested in `tests/integration/test_redemption_summary.py`).
- A row that breaks several reject rules is quarantined once, with all its rule codes.

## How I'll handle the dates

- Never guess. If a date can't be read, or could mean two different days, it becomes empty and gets a flag.
- A file's format can be worked out from its clear values (IND is month-first because of the 13), but I'll write that down as an assumption instead of doing it quietly.
- Age and stale are calculated against a date I pass in, so results are repeatable.
