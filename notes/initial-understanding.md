# Initial understanding

## What they want

SkyPoints keeps all its members in one big database. They want members split into one table per country (India, USA, etc.). Two files arrive every day:

- a pipe-delimited member file
- a JSON file of mileage redemptions from partner airlines

I need to load both, add Age and a Stale_Member flag, split members by country, flatten the JSON, and add data checks. If a member moves country, only the newest record should count. It also has to work for billions of records a day.

## Rough plan

Load the raw file as it is, clean it into a staging table, then route members into country tables. Bad rows go to a separate quarantine table instead of being dropped. The JSON gets flattened into one row per redemption and joined to members on member id.

I'll write the SQL for Snowflake. For something I can actually run and demo, I'll use DuckDB with Python.

## Things that look off in the sample data

- The spec says Member Name is the key column, but names can repeat. Member ID makes more sense.
- The spec has 11 columns (including Post Code), but the sample header only has 10.
- DOB `03051985` turned into `3051985` in staging, so the leading zero got lost.
- Agent_Name is "Sam" in the file but blank in staging for most rows.
- The staging column says `County`, probably meant `Country`.
- Country codes are mixed: USA, IND, CAN, but also PHIL and AU.
- Some member IDs (22345, 2256) are shorter than the others.
- IND, USA and AUS files all use different column names and date formats. All three also use IDs 1, 2, 3, so IDs clash across countries.
- USA dates like `1052022` could be Jan 5 or Oct 5.
- AUS has a DOB of "NULL" and a date with month 13 (`2021-13-13`).

## Questions I'd ask

- Is Member ID the real key?
- Is DOB day-first or month-first?
- How do we decide which record is "latest"? File date seems safest. Last flight date isn't really a record date.
- When a member moves, do we keep any history in the old country?
- Where do members with no country (or an unknown one) go?
- Stale_Member: 90 days from today, or from the file date? And what if there's no flight date?
- Can the same redemption come again later with a changed status?

## Assumptions for now

- Member ID is the key.
- DOB is month-first (MMDDYYYY).
- Latest record = newest file date, then last row in the file.
- Age and stale are worked out from a date I pass in, not today's date, so results are repeatable.
- Dates that can't be read become empty and get flagged, never guessed.
- Missing/unknown country goes to an "unassigned" table.
- The IND/USA/AUS files are extra sources that I map to one common layout.