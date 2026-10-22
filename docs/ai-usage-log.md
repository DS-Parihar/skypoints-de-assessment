# How I used AI

I used Claude Code for the boilerplate and for first drafts, and checked everything myself. This log records what I asked, what came back, and what I changed.

## Phase 1: profiling the source files

- **Asked for:** a script that reports columns, blanks, distinct values and date formats for every file in `data/raw`.
- **What came back:** a working script. It reported formats per column, not per value, and it didn't read the pipe-delimited member feed.
- **What I changed:** asked it to read the `.dat` file, check the header against the spec, and list the ambiguous date values by name (like `1052022`).
- **What I learned:** a column-level summary hides the ambiguous values. You only notice if you read the counts.

> **Draft entries for the later phases.** The entries below were drafted from what actually happened
> while building, with the real error messages and test results. Read each one, rewrite it in your own
> words, and delete any you do not recognise or cannot explain.

## Phase 3: the DuckDB loader for the pipe-delimited file

- **Asked for:** DuckDB equivalents of the Snowflake landing tables, plus the load statement.
- **What came back:** a `read_csv` call that let DuckDB sniff the dialect.
- **Problem:** the header has 10 fields and the detail lines 11, and the sniffer failed with "It was not possible to automatically detect the CSV Parsing dialect". Found by running the statement on the fixture, not by reading it.
- **What I changed:** explicit column list, `auto_detect = false`, `null_padding = true`. Later the parser mapped columns by header name instead, because a positional layout silently shifts when `Post_Code` is missing.

## Phase 5: Stale_Member for a member with no flight date

- **What came back:** SQL that treated a missing `Last_Flight_Date` as stale (`IS NULL OR < as_of - 90`).
- **Problem:** "never flew" and "flew long ago" are different facts, and the brief says the field is optional. The tests I then wrote for `is_stale` returned NULL (unknown) for it.
- **What I changed:** the SQL, both DDL files (the column became nullable), the staging tests and `docs/architecture.md`, so the Python function and the SQL answer the same way. A test compares them for six as-of dates and nine edge cases.
- **What I learned:** a rule written in two places drifts unless a test compares the two.

## Phase 5: age in SQL

- **What came back:** `DATEDIFF('year')` was the obvious Snowflake choice.
- **Problem:** `DATEDIFF('year')` counts calendar-year boundaries, so a 31 December birthday is "1" on 1 January. For DuckDB `date_sub('year', ...)` counts completed years (checked in a shell: 38 the day before the birthday, 39 on it), while `date_diff` counts boundaries like `DATEDIFF`.
- **What I changed:** one rule in both dialects: year difference minus one if the month-day of `as_of` is before the month-day of the DOB. A 29 February birthday counts from 1 March. Tested at the edges.

## Phase 6: speed of the rule engine

- **What came back:** rules as SQL views over work tables, which passed the 228 rule tests after one fix (an empty-list `executemany` that DuckDB rejects).
- **Problem:** each pipeline run took about 5 seconds. Profiling showed the decision views were re-evaluating the 17 rule views several times, and `executemany` into a table with a primary key cost about 50 ms a row.
- **What I changed:** materialise the findings and parsed dates once per run, and insert config rows in a single multi-row statement. A run is about 1 second. Profile before tuning: my first guess was the wrong statement.

## Phase 7: the key in "latest wins"

- **What came back (from my prompt):** `PARTITION BY member_id`, as written in the playbook.
- **Problem:** ADR 0002 had since changed the key to `source_system:member_id`, because ids 1, 2, 3 repeat across the IND, USA and AUS files. Partitioning by `member_id` would merge different people.
- **What I changed:** `member_key` everywhere, and a test that two sources with the same `member_id` stay two members.

## Phase 8: replacing a staged redemption

- **What came back:** `DELETE` the old row, then `INSERT` the newer version of the same `txn_id`.
- **Problem:** DuckDB raised "Duplicate key ... violates primary key constraint": it rejects re-inserting a key it deleted in the same transaction. Found when the cross-file duplicate test ran.
- **What I changed:** an `UPDATE` in place for a replaced row, an `INSERT` only for new ones.

## Phase 9: what the first pipeline tests assumed

- **What came back:** a failing-file test that expected the rejected file's rows to count inside "raw", so that `raw = staged + quarantined + failed`.
- **Problem:** the loader (correctly) inserts nothing for a file whose header does not match, so those rows never reach raw. The test encoded a wrong model of my own design.
- **What I changed:** the test and the report: "Rows in failed files" is counted from the rejected file and sits outside the raw equation. Writing the implementation showed what the test had got wrong.

## Phase 9: the client files and Git

- **Problem found while writing checksums:** `core.autocrlf=true` and no `.gitattributes`, so a Windows checkout would convert the client CSV files from LF to CRLF and any checksum test would fail on someone else's machine.
- **What I changed:** `data/raw/** -text` in `.gitattributes`, and a test that the file is present.

## Where the AI was wrong about its own tests

- A test used a SKYPOINTS-format enrollment date for a USA-format row, so the row was rejected and the test failed for the wrong reason.
- One test ended with `assert "…" in output or True`, which can never fail. I caught it when re-reading the diff and replaced it with a real assertion.
- Several shell edits through heredocs mangled quotes and backslashes (a `\n` in an f-string became a real newline). The syntax error made the mistake obvious, but only because the tests ran straight after.
