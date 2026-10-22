# 0005. Date parsing: DOB assumed MMDDYYYY; ambiguous or invalid dates become NULL and are flagged

## Context
The date format is not documented and varies by source: `03051985` (leading zero lost in the
existing staging, becoming `3051985`), `M/D/YYYY`, `YYYY-MM-DD`, unpadded `MDDYYYY`, and `YYYYMMDD`.
Some values are ambiguous (`1052022`, `1152022`) or invalid (`2021-13-13`, the text `NULL`).

## Decision
- Raw keeps every date as text; parsing happens only in staging.
- DOB is assumed month-first (MMDDYYYY). IND's `8/13/1982` supports month-first for that source.
  Formats inferred from clear values are written down as assumptions, not applied quietly.
- A value that cannot be parsed, or could mean two different days, becomes NULL with a DQ flag
  (`dob_invalid`, `enrollment_date_invalid`, `flight_date_invalid`). Nothing is guessed.
- Age and Stale_Member are calculated from an explicit `as_of` date, never today's date.

## Consequences
- Results are repeatable and every NULL date can be traced to a flag and to the raw text.
- Some valid-looking dates are lost to ambiguity until the source owner confirms the format.
- Members with no usable DOB have a NULL Age but are not dropped.
- Age and Stale_Member change only when `as_of` changes.
