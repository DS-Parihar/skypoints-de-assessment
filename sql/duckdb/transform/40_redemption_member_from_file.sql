-- =============================================================================
-- 40_redemption_member_from_file.sql  (DuckDB)
-- Purpose : Read a redemption feed file (a JSON array of member objects) into the temp table
--           _redemption_member, one row per member object, payload kept whole as JSON.
--           Used by flatten_redemption_feed (parse only, no database tables involved).
-- Params  : $file, $file_name, $file_ts, $batch_id
-- Run as a single statement (DuckDB allows prepared parameters only on the last statement).
-- =============================================================================
CREATE OR REPLACE TEMP TABLE _redemption_member AS
SELECT CAST($file_name AS VARCHAR)  AS FILE_NAME,
       row_number() OVER ()         AS FILE_ROW_NUMBER,   -- 1-based position in the array
       CAST($file_ts AS TIMESTAMP)  AS FILE_TS,
       CAST($batch_id AS VARCHAR)   AS BATCH_ID,
       json                         AS PAYLOAD
FROM read_json_objects($file, format = 'array');
