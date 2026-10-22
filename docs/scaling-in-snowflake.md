# Scaling to billions of records a day in Snowflake

The design in `docs/architecture.md` is already shaped for volume: raw is append-only text, work is
done per batch, and the "current" tables are bounded by the number of members, not by the feed size.
This note says what changes between the DuckDB run on a laptop and Snowflake in production.

## 1. Ingestion
- **File size:** split feeds into compressed files of about 100-250 MB. Snowflake loads files in parallel,
  one per warehouse thread, so one 50 GB file is slow and fifty 1 GB files are not. Ask the feed owner
  for chunked, line-delimited files (ADR 0008) rather than one JSON array.
- **Snowpipe or scheduled COPY:** Snowpipe (auto-ingest from the bucket's events) when files arrive all
  day and latency matters; a scheduled `COPY INTO` task for a daily drop. Either way the loader writes
  `file_name`, `file_row_number` (`METADATA$FILE_ROW_NUMBER`) and `file_ts` into raw.
- **Idempotency:** COPY remembers loaded files for 64 days (`LOAD_HISTORY`), Snowpipe for 14. Beyond
  that, or after a reload, the pipeline's own rule applies: a file name already in raw is skipped.

## 2. Raw
Append-only and all text, so a load never fails on content and nothing is lost to a type error. It is
the replay source: keep it under a retention policy, and use Time Travel for recovery. Because the
rest is rebuilt from raw, any rule change can be re-applied to history.

## 3. Incremental processing
Do not rescan the base. Put a **stream** on each raw table and run a **task graph** (or Dynamic
Tables) that wakes only when `SYSTEM$STREAM_HAS_DATA` is true. Staging processes the new batch only;
`CORE.MEMBER_CURRENT` is changed with a `MERGE` on `member_key`; a second stream on `MEMBER_CURRENT`
drives the country tables so only members whose row changed are touched
(`sql/snowflake/04_country_split.sql`). The `as_of` date is a task parameter, never `CURRENT_DATE()`.

## 4. Clustering and MERGE cost
- `MEMBER_CURRENT` and the country tables are clustered by `MEMBER_KEY`: the MERGE joins on it, so Snowflake
  prunes micro-partitions and touches only those holding changed members instead of the whole table.
- `REDEMPTION_TXN` is clustered by `MEMBER_KEY` for the summary join; add `TXN_DATE` if reporting is by period.
- Raw and staging are not clustered: loads arrive in time order, which already clusters them by `FILE_TS`.
- A MERGE is only as expensive as the partitions it rewrites. Keep the incoming batch deduplicated first
  (`QUALIFY ROW_NUMBER() ... = 1`), compare `(file_ts, file_row_number)` in the `WHEN MATCHED` condition so
  stale rows rewrite nothing, and check `SYSTEM$CLUSTERING_INFORMATION` before adding more keys.

## 5. Warehouses
Separate warehouses for load, transform and reporting, so a heavy backfill does not slow the daily run
or the people querying. Size by measured spill (`bytes spilled to disk` in the query profile), not by
guesswork; auto-suspend after about a minute. Multi-cluster only for the reporting warehouse.

## 6. Validation at scale
The rules are set-based SQL, so they scale like any query. Full checks stay on: mandatory fields, key
duplicates, lengths and row-count reconciliation (raw = staged + quarantined) cost one pass. Expensive
checks can run on a sample per batch with the sample size in config (`DQ016` completeness is a count per
column, which is cheap). A FAIL_BATCH blocks promotion to CORE and leaves raw untouched, so a bad batch
is stopped before it reaches consumers.

## 7. One table per country, or one clustered table
Per-country tables are delivered as asked. At many countries they mean many MERGE jobs, uneven table
sizes and a pipeline change for every new country. One `MEMBER_CURRENT` clustered by country, with a
secure view per country, needs one MERGE, prunes just as well and still gives each country its own
access control (ADR 0007). Swapping is invisible to consumers because the names stay.

## 8. Monitoring and cost
Alert on: a FAIL_BATCH or FAIL_FILE, a quarantine rate above its usual level, `MEMBERS_UNASSIGNED` growing
(a missing country mapping), a stream that stops draining, and task failures. Watch credit use per
warehouse and per task, and the storage of raw and Time Travel; raw is cheap, but a long retention on
billions of rows is not free.
