-- =============================================================================
-- 30_split_candidates.sql  (DuckDB)
-- Purpose : Step 1a of the country split. Copy the staging rows to merge into a temp table.
-- Params  : $batch_id  one batch, or NULL for every staging row
-- Run as a single statement (DuckDB allows prepared parameters only on the last statement).
-- =============================================================================
CREATE OR REPLACE TEMP TABLE _split_candidate AS
SELECT s.*
FROM STAGING.MEMBER_STG s
WHERE CAST($batch_id AS VARCHAR) IS NULL OR s.BATCH_ID = CAST($batch_id AS VARCHAR);
