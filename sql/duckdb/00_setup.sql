-- =============================================================================
-- 00_setup.sql  (DuckDB)
-- Purpose : Create the schemas used by every later script. Same names as the
--           Snowflake version in sql/snowflake/00_setup.sql. Safe to re-run.
-- Differences from Snowflake:
--   * No database to create: the DuckDB file (or :memory:) is the database.
--   * No stages, pipes or storage integrations: files are read straight from
--     data/raw with read_csv / read_json (see 01_raw.sql).
--   * No named file formats: the delimiter and options are given per read.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS RAW;       -- landing: every column text, append-only, plus load metadata
CREATE SCHEMA IF NOT EXISTS STAGING;   -- typed, derived Age/Stale_Member, DQ flags
CREATE SCHEMA IF NOT EXISTS CORE;      -- conformed: one row per member (member_current), history
CREATE SCHEMA IF NOT EXISTS MART;      -- per-country member tables + members_unassigned
CREATE SCHEMA IF NOT EXISTS DQ;        -- quarantine table and data-quality checks
CREATE SCHEMA IF NOT EXISTS REF;       -- reference / configuration tables (country map)
