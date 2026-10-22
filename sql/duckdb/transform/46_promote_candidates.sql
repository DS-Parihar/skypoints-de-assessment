-- =============================================================================
-- 46_promote_candidates.sql  (DuckDB)
-- Purpose : Step 1 of the promotion to CORE.REDEMPTION_TXN: the staged redemptions to promote.
-- Params  : $batch_id  one batch, or NULL for all staged rows
-- =============================================================================
CREATE OR REPLACE TEMP TABLE _promote_candidate AS
SELECT s.*
FROM STAGING.REDEMPTION_STG s
WHERE CAST($batch_id AS VARCHAR) IS NULL OR s.BATCH_ID = CAST($batch_id AS VARCHAR);
