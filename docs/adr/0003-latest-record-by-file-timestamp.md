# 0003. "Latest" means latest file timestamp, then file row number

## Context
When a member appears more than once, the newest record must win. The candidates are a date inside
the record (such as Last_Flight_Date) or the position of the record in the load.

## Decision
Rank rows per `member_id` by `file_ts DESC, file_row_number DESC` and keep the first.
`file_ts` is taken from the file name, not from `now()` or load time. `file_row_number` breaks ties
inside one file, so the last row of a file wins. Last_Flight_Date is not used: it is a business
event, not a record version, and a member can have a newer record with an older or missing flight
date.

## Consequences
- The result is deterministic and repeatable; re-running on the same files gives the same output.
- It depends on a reliable timestamp in the file name. A file with no parseable timestamp is
  rejected rather than guessed.
- Two files with the same `file_ts` fail a uniqueness check instead of one being picked arbitrarily.
- A late-arriving older file cannot overwrite a newer record, because ranking uses the file
  timestamp, not arrival order.
