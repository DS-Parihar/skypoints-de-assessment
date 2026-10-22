-- =============================================================================
-- 22_record_findings.sql  (DuckDB)
-- Purpose : Record the findings of the batch in DQ.RULE_PARAM, and the DQ summary table.
--           DQ.ROW_FINDING : WARN, ROUTE, FAIL_FILE and FAIL_BATCH findings. REJECT findings are
--                            in DQ.QUARANTINE, so they are not repeated here. Findings of rows
--                            that were rejected or that sit in a failed file are not recorded.
--           DQ.RUN_SUMMARY : (batch_id, rule_code, failed_count) for every rule that fired,
--                            counting the REJECT findings of rejected rows too.
-- Re-runnable: the batch's findings and summary are replaced, never duplicated.
-- Same logic as sql/snowflake/transform/22_record_findings.sql.
-- =============================================================================

DELETE FROM DQ.ROW_FINDING WHERE BATCH_ID IN (SELECT BATCH_ID FROM DQ.RULE_PARAM);

INSERT INTO DQ.ROW_FINDING
    (BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, RULE_CODE, SEVERITY, COLUMN_NAME, VALUE, REASON)
SELECT COALESCE(f.BATCH_ID, p.BATCH_ID), f.FILE_NAME, f.FILE_ROW_NUMBER, f.MEMBER_KEY, f.RULE_CODE,
       f.SEVERITY, f.COLUMN_NAME, f.VALUE, f.REASON
FROM DQ.V_RUN_FINDINGS f
CROSS JOIN DQ.RULE_PARAM p
WHERE f.SEVERITY <> 'REJECT'
  AND (f.FILE_ROW_NUMBER IS NULL
       OR EXISTS (SELECT 1 FROM DQ.V_ACCEPTED_ROW a
                  WHERE a.FILE_NAME = f.FILE_NAME AND a.FILE_ROW_NUMBER = f.FILE_ROW_NUMBER
                    AND a.BATCH_ID IS NOT DISTINCT FROM f.BATCH_ID));

DELETE FROM DQ.RUN_SUMMARY WHERE BATCH_ID IN (SELECT BATCH_ID FROM DQ.RULE_PARAM);

INSERT INTO DQ.RUN_SUMMARY (BATCH_ID, RULE_CODE, FAILED_COUNT)
SELECT BATCH_ID, RULE_CODE, count(*)
FROM (
    SELECT g.BATCH_ID, g.RULE_CODE FROM DQ.ROW_FINDING g
    WHERE g.BATCH_ID IN (SELECT BATCH_ID FROM DQ.RULE_PARAM)
    UNION ALL
    SELECT p.BATCH_ID, f.RULE_CODE
    FROM DQ.WORK_FINDING f
    JOIN DQ.V_ROW_REJECT x
      ON x.FILE_NAME = f.FILE_NAME AND x.FILE_ROW_NUMBER = f.FILE_ROW_NUMBER
     AND x.BATCH_ID IS NOT DISTINCT FROM f.BATCH_ID
    CROSS JOIN DQ.RULE_PARAM p
    WHERE f.SEVERITY = 'REJECT'
) t
GROUP BY BATCH_ID, RULE_CODE;
