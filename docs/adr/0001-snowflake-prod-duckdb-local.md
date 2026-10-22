# 0001. Snowflake for production DDL; DuckDB for the local pipeline and tests

## Context
The brief asks for a solution that works at billions of records a day, which means a cloud
warehouse. The assessment also needs something that can be run, demoed and tested on a laptop
without a Snowflake account.

## Decision
- Production DDL and transforms are written for Snowflake, in `sql/snowflake`.
- The runnable pipeline and all automated tests use DuckDB and Python, in `sql/duckdb` and
  `src/skypoints`.
- Both implement the same layers and rules (raw -> staging -> member_current -> country tables,
  plus quarantine). Dialect differences are limited to loading, JSON handling and incremental
  mechanics.

## Consequences
- Anyone can run `pytest` and the pipeline offline.
- Two SQL dialects must be kept in step; a rule changed in one needs the same change in the other.
- The Snowflake SQL is not executed by the test suite, so it carries more review risk than the
  DuckDB version. Features with no DuckDB equivalent (Streams, Tasks, `COPY INTO` metadata) are
  documented in `docs/architecture.md`, not tested.
