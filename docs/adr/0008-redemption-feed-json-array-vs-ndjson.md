# 0008. Redemption feed: a JSON array today, line-delimited JSON at scale

## Context
The brief shows the redemption feed as one JSON array of member objects, each with a nested
`redemptions` array. An array file is a single JSON document: it cannot be cut into chunks, a parser
has to read it from the first bracket to the last, and one malformed character fails the whole file.
At billions of records a day that is the wrong shape for a warehouse load. The same data as
line-delimited JSON (NDJSON, one member object per line) can be split at any newline into files of
100-250 MB for parallel loading, appended to, and a bad line spoils one member object, not the file.

## Decision
- The pipeline reads what the brief shows: `read_json_objects(..., format = 'array')` in DuckDB and a
  JSON file format with `STRIP_OUTER_ARRAY = TRUE` in Snowflake. Either way one raw row is one member
  object, so everything after raw (flatten, rules, staging, promotion) is independent of the file shape.
- We recommend that the feed owner delivers NDJSON, one member object per line, split into chunks. Moving
  to it is two small edits: `format = 'newline_delimited'` in `sql/duckdb/transform/40` and `44`, and
  `STRIP_OUTER_ARRAY = FALSE` in `RAW.REDEMPTION_FEED_FORMAT`. No other layer changes.

## Consequences
- Today's sample feeds load as they are, with no wait for the supplier.
- Until the feed changes, one invalid JSON file stops that whole file (it fails loudly, and the other files
  in the run continue).
- The recommendation belongs in the hand-over to the feed owner, next to the request for a record-count
  trailer on the member feed.
