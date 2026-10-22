# Sample run: latest record wins when a member moves country

A real run of the pipeline on two small member files, so you can see the rule work without running
anything. The files are `tests/fixtures/split/move/`; the output below is copied from the CSV export
of the run, not typed by hand. To reproduce it, copy the two files into an empty folder, run the
pipeline once with the first file and again after adding the second.

## The input

| File (the date and time are in the name) | Lines |
|---|---|
| `SKYPOINTS_MEMBERS_20240114_09000000.dat` (day 1) | Ravi `223458` in **IND**, Elena `223457` in USA |
| `SKYPOINTS_MEMBERS_20240115_09000000.dat` (day 2) | Ravi `223458` in **USA** |

## After day 1

`members_ind.csv`

| member_key | member_name | country_code | file_name |
|---|---|---|---|
| SKYPOINTS:223458 | Ravi | IND | SKYPOINTS_MEMBERS_20240114_09000000.dat |

`members_usa.csv`

| member_key | member_name | country_code | file_name |
|---|---|---|---|
| SKYPOINTS:223457 | Elena | USA | SKYPOINTS_MEMBERS_20240114_09000000.dat |

## After day 2 (same database, second run)

Ravi is now only in USA. He is gone from India:

`members_ind.csv`

_(no rows)_

`members_usa.csv`

| member_key | member_name | country_code | file_name |
|---|---|---|---|
| SKYPOINTS:223457 | Elena | USA | SKYPOINTS_MEMBERS_20240114_09000000.dat |
| SKYPOINTS:223458 | Ravi | USA | SKYPOINTS_MEMBERS_20240115_09000000.dat |

The move is recorded once, in `member_country_history.csv`, dated by the file that caused it:

| member_key | old_country_code | new_country_code | effective_file_ts | batch_id |
|---|---|---|---|---|
| SKYPOINTS:223458 | IND | USA | 2024-01-15 09:00:00 | SKYPOINTS_MEMBERS_20240115_09000000.dat |

The run report for day 2 says `Country moves recorded: 1` and still reconciles:

```
  RAW member rows: 3
  STAGING members: 3
  Quarantined member rows: 0
  CORE members: 2
```

## Why it works

- `CORE.MEMBER_CURRENT` has one row per `member_key`; Ravi's day-2 record is **strictly newer** by
  `(file_ts, file_row_number)`, so it replaces the day-1 row.
- The country tables are rebuilt from `MEMBER_CURRENT`, not edited one by one, so a member cannot be
  in two of them, and the old row disappears by itself.
- If the files arrive in the other order, day 1 after day 2, the older record does **not** overwrite
  the newer one. The tests `test_late_arrival_*` in `tests/integration/test_country_split.py` cover
  that, re-running the same file, a member who moves back, and a blank country.
