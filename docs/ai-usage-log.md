# How I used AI

I used Claude Code for scaffolding and SQL boilerplate, and Claude.ai for planning and review. Everything was reviewed before it was committed. This log records what I asked, what came back, and what I changed.

## 1. Profiling the source files (Phase 1)

- **What came back:** Claude's first script didn't read the pipe-delimited `.dat` file, and it reported date formats per column, which hid the ambiguous USA values.
- **What I changed:** I asked for per-value reporting. That is how `1052022` and `1152022` showed up, each with two possible dates.
- **Result:** ADR 0005, "never guess": an unreadable or ambiguous date becomes NULL with a flag.

## 2. The member key (ADR 0002)

- **What came back:** Claude first keyed everything on `member_id` alone.
- **Problem:** IDs 1, 2 and 3 exist in IND, USA and AUS, so that would have merged three different people into one.
- **What I changed:** the key is `source_system:member_id` (`member_key`), and I rewrote ADR 0002.

## 3. The header rule

- **What came back:** Claude's first parser rejected any missing column.
- **Problem:** that would have failed the brief's own 10-field sample, which has no `Post_Code`.
- **What I changed:** columns are mapped by header name. A missing optional column is a warning; a missing mandatory column fails the file.

## 4. Age formula

- **First idea:** `DATEDIFF('year', dob, as_of)`. It counts year boundaries, so someone born on 31 December is "1" on 1 January.
- **What I changed:** both SQL dialects use a month-day adjusted formula, and I added a test for a 29 February birthday in a non-leap year (counted from 1 March).

## 5. One quarantine table

- **First suggestion:** a new `DQ.MEMBER_QUARANTINE` table for the validation framework.
- **What I decided:** keep the existing `DQ.QUARANTINE` and add `rule_code` and `severity` columns, so there is one audit trail and not two.

## 6. Not renaming redemptions.json

- **First idea:** rename `redemptions.json` so it matches a feed file pattern and the pipeline picks it up.
- **What I decided:** no. Raw data from the client is immutable. I added a source manifest in `config.py` that says how to read each file, and the client files stay byte-identical (a test checks their checksums).

## 7. The first pipeline run

- **Problem:** the first run reported all zeros. No file in `data/raw` matched the member feed file name, and everything was skipped without a word.
- **What I changed:** the source manifest, and every unmatched file is now listed in the run report with the reason "no matching feed rule".

## 8. The country split

- **What came back:** Claude's first API sketch ranked the incoming batch, but it handled only a NULL country and ignored late-arriving files.
- **What I changed:** an older file never overwrites a newer record (strictly-newer rule), `stale_ignored` is counted, and two different records that tie for newest raise an error and write nothing. The reasoning is in ADR 0003.
