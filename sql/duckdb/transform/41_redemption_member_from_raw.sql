-- =============================================================================
-- 41_redemption_member_from_raw.sql  (DuckDB)
-- Purpose : Same temp table as 40, but read from RAW.REDEMPTION_FEED_RAW, the stored VARIANT
--           (JSON in DuckDB), so staging is built from raw and not from the file again.
-- Params  : $file_name
-- =============================================================================
CREATE OR REPLACE TEMP TABLE _redemption_member AS
SELECT FILE_NAME, FILE_ROW_NUMBER, FILE_TS, BATCH_ID, PAYLOAD
FROM RAW.REDEMPTION_FEED_RAW
WHERE FILE_NAME = CAST($file_name AS VARCHAR);
