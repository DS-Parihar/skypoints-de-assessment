-- =============================================================================
-- 21_reconcile_counts.sql  (DuckDB)
-- Purpose : Refresh DQ.RULE_COUNTS and DQ.RULE_COLUMN_COUNTS for the batch in DQ.RULE_PARAM,
--           the inputs of DQ015 (row counts) and DQ016 (non-blank counts per text column).
-- Raw side: rows of files that did not fail, minus rejected rows, so a rejection is never
--           mistaken for lost data. Staging side: rows staged for the batch.
-- Same logic as sql/snowflake/transform/21_reconcile_counts.sql.
-- =============================================================================

DELETE FROM DQ.RULE_COUNTS;
INSERT INTO DQ.RULE_COUNTS (BATCH_ID, RAW_ROWS, STAGED_ROWS, REJECTED_ROWS)
SELECT p.BATCH_ID,
       (SELECT count(*) FROM DQ.V_RULE_ROW r
         WHERE NOT EXISTS (SELECT 1 FROM DQ.V_FAILED_FILE f WHERE f.FILE_NAME = r.FILE_NAME)),
       (SELECT count(*) FROM STAGING.MEMBER_STG s
         WHERE s.BATCH_ID = p.BATCH_ID
           AND NOT EXISTS (SELECT 1 FROM DQ.V_FAILED_FILE f WHERE f.FILE_NAME = s.FILE_NAME)),
       (SELECT count(*) FROM DQ.V_ROW_REJECT)
FROM DQ.RULE_PARAM p;

DELETE FROM DQ.RULE_COLUMN_COUNTS;
INSERT INTO DQ.RULE_COLUMN_COUNTS (BATCH_ID, COLUMN_NAME, RAW_NONBLANK, STAGED_NONBLANK)
WITH raw_c AS (
    SELECT 'member_name' AS COLUMN_NAME, count(MEMBER_NAME) AS N FROM DQ.V_ACCEPTED_ROW
    UNION ALL SELECT 'member_id', count(MEMBER_ID) FROM DQ.V_ACCEPTED_ROW
    UNION ALL SELECT 'tier_code', count(TIER_CODE) FROM DQ.V_ACCEPTED_ROW
    UNION ALL SELECT 'agent_name', count(AGENT_NAME) FROM DQ.V_ACCEPTED_ROW
    UNION ALL SELECT 'state', count(STATE) FROM DQ.V_ACCEPTED_ROW
    UNION ALL SELECT 'country', count(COUNTRY) FROM DQ.V_ACCEPTED_ROW
    UNION ALL SELECT 'active_member', count(ACTIVE_MEMBER) FROM DQ.V_ACCEPTED_ROW
),
stg AS (
    SELECT s.* FROM STAGING.MEMBER_STG s JOIN DQ.RULE_PARAM p ON s.BATCH_ID = p.BATCH_ID
),
stg_c AS (
    SELECT 'member_name' AS COLUMN_NAME, count(NULLIF(TRIM(MEMBER_NAME), '')) AS N FROM stg
    UNION ALL SELECT 'member_id', count(NULLIF(TRIM(MEMBER_ID), '')) FROM stg
    UNION ALL SELECT 'tier_code', count(NULLIF(TRIM(TIER_CODE), '')) FROM stg
    UNION ALL SELECT 'agent_name', count(NULLIF(TRIM(AGENT_NAME), '')) FROM stg
    UNION ALL SELECT 'state', count(NULLIF(TRIM(STATE), '')) FROM stg
    UNION ALL SELECT 'country', count(NULLIF(TRIM(COUNTRY), '')) FROM stg
    UNION ALL SELECT 'active_member', count(NULLIF(TRIM(ACTIVE_MEMBER), '')) FROM stg
)
SELECT p.BATCH_ID, r.COLUMN_NAME, r.N, s.N
FROM raw_c r JOIN stg_c s ON s.COLUMN_NAME = r.COLUMN_NAME
CROSS JOIN DQ.RULE_PARAM p;
