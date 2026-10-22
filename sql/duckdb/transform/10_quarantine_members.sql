-- =============================================================================
-- 10_quarantine_members.sql  (DuckDB)
-- Purpose : Copy the rows that cannot be members into DQ.QUARANTINE with a reason code.
-- Params  : $batch_id  the raw batch to process
-- Run as one statement (DuckDB allows prepared parameters only on the last statement).
-- Re-runnable: a row already quarantined for the same reason is not added twice.
-- =============================================================================
INSERT INTO DQ.QUARANTINE
    (SOURCE_TABLE, FILE_NAME, FILE_ROW_NUMBER, BATCH_ID, REASON_CODE, REASON_DETAIL, RAW_PAYLOAD)
SELECT 'RAW.MEMBER_FEED_RAW', p.FILE_NAME, p.FILE_ROW_NUMBER, p.BATCH_ID, p.REJECT_REASON,
       'Rejected while staging the member feed', p.RAW_PAYLOAD
FROM STAGING.MEMBER_PREPARED_V p
WHERE p.BATCH_ID = $batch_id
  AND p.REJECT_REASON IS NOT NULL
  AND NOT EXISTS (
      SELECT 1 FROM DQ.QUARANTINE q
      WHERE q.SOURCE_TABLE = 'RAW.MEMBER_FEED_RAW'
        AND q.FILE_NAME = p.FILE_NAME
        AND q.FILE_ROW_NUMBER = p.FILE_ROW_NUMBER
        AND q.REASON_CODE = p.REJECT_REASON);
