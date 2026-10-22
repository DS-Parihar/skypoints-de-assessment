-- =============================================================================
-- 44_load_redemption_raw.sql  (DuckDB)
-- Purpose : Append the feed file to RAW.REDEMPTION_FEED_RAW: one row per member object, the
--           object kept whole as JSON, plus the load metadata. Raw is append-only.
-- Params  : $file, $file_name, $file_ts, $batch_id
-- =============================================================================
INSERT INTO RAW.REDEMPTION_FEED_RAW (PAYLOAD, FILE_NAME, FILE_ROW_NUMBER, FILE_TS, BATCH_ID)
SELECT json, CAST($file_name AS VARCHAR), row_number() OVER (), CAST($file_ts AS TIMESTAMP),
       CAST($batch_id AS VARCHAR)
FROM read_json_objects($file, format = 'array');
