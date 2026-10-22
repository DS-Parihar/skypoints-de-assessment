-- =============================================================================
-- 20_apply_validations.sql  (Snowflake)
-- Purpose : APPLY the decisions of the rules (sql/snowflake/05_validations.sql):
--           1. REJECT rows go to DQ.QUARANTINE, once per row, with every rule code.
--           2. Accepted rows go to STAGING.MEMBER_STG, typed, with AGE and STALE_MEMBER.
-- Params  : none. The batch and the as-of date come from DQ.RULE_PARAM (loaded by the
--           orchestrator); the rows come from DQ.RULE_INPUT. Never CURRENT_DATE in the logic.
-- Run     : after loading the work tables and materialising DQ.WORK_FINDING / DQ.WORK_DATE.
-- Re-runnable: a row already quarantined or staged is not added twice.
-- Rows of a file that failed its header check (FAIL_FILE) are neither quarantined nor staged.
-- Rows with no file timestamp or no source system cannot be staged; they are not hidden:
-- DQ015 (raw rows <> staged + rejected) fails the batch.
-- Same logic as sql/duckdb/transform/20_apply_validations.sql (the version the tests run).
-- =============================================================================

INSERT INTO DQ.QUARANTINE
    (SOURCE_TABLE, FILE_NAME, FILE_ROW_NUMBER, BATCH_ID, REASON_CODE, REASON_DETAIL, RULE_CODE,
     SEVERITY, RAW_PAYLOAD)
SELECT 'RAW.MEMBER_FEED_RAW', x.FILE_NAME, x.FILE_ROW_NUMBER, x.BATCH_ID,
       SPLIT_PART(x.RULE_CODES, ',', 1), x.REASONS, x.RULE_CODES, 'REJECT', r.RAW_PAYLOAD
FROM DQ.V_ROW_REJECT x
JOIN DQ.V_RULE_ROW r
  ON r.FILE_NAME = x.FILE_NAME AND r.FILE_ROW_NUMBER = x.FILE_ROW_NUMBER
 AND EQUAL_NULL(r.BATCH_ID, x.BATCH_ID)
WHERE NOT EXISTS (
    SELECT 1 FROM DQ.QUARANTINE q
    WHERE q.SOURCE_TABLE = 'RAW.MEMBER_FEED_RAW' AND q.FILE_NAME = x.FILE_NAME
      AND q.FILE_ROW_NUMBER = x.FILE_ROW_NUMBER AND EQUAL_NULL(q.BATCH_ID, x.BATCH_ID));

INSERT INTO STAGING.MEMBER_STG
    (MEMBER_NAME, MEMBER_ID, ENROLLMENT_DATE, LAST_FLIGHT_DATE, TIER_CODE, AGENT_NAME, STATE,
     COUNTRY, POST_CODE, DATE_OF_BIRTH, ACTIVE_MEMBER, SOURCE_SYSTEM, MEMBER_KEY, COUNTRY_CODE,
     AS_OF_DATE, AGE, STALE_MEMBER,
     DQ_DOB_INVALID, DQ_LAST_FLIGHT_DATE_INVALID, DQ_COUNTRY_UNKNOWN, DQ_POST_CODE_INVALID,
     DQ_MEMBER_ID_SHORT, DQ_ACTIVE_FLAG_UNKNOWN,
     FILE_NAME, FILE_ROW_NUMBER, FILE_TS, BATCH_ID)
SELECT a.MEMBER_NAME, a.MEMBER_ID, enr.PARSED_DATE, fl.PARSED_DATE, a.TIER_CODE, a.AGENT_NAME,
       a.STATE, a.COUNTRY,
       CASE WHEN REGEXP_LIKE(a.POST_CODE, '[0-9]{1,5}') THEN a.POST_CODE END,   -- text: keeps leading zeros
       dob.PARSED_DATE, a.ACTIVE_MEMBER, a.SOURCE_SYSTEM, a.MEMBER_KEY, m.ISO3,
       p.AS_OF_DATE,
       -- AGE: whole years at as-of; the month-day comparison is the birthday adjustment.
       CASE WHEN dob.PARSED_DATE IS NULL OR dob.PARSED_DATE > p.AS_OF_DATE THEN NULL
            ELSE YEAR(p.AS_OF_DATE) - YEAR(dob.PARSED_DATE)
                 - IFF(MONTH(p.AS_OF_DATE) * 100 + DAY(p.AS_OF_DATE)
                       < MONTH(dob.PARSED_DATE) * 100 + DAY(dob.PARSED_DATE), 1, 0)
       END,
       -- STALE_MEMBER: more than 90 days since the last flight; NULL = unknown.
       CASE WHEN fl.PARSED_DATE IS NULL OR fl.PARSED_DATE > p.AS_OF_DATE THEN NULL
            ELSE DATEDIFF(day, fl.PARSED_DATE, p.AS_OF_DATE) > 90 END,
       dob.STATUS IN ('INVALID', 'AMBIGUOUS'),
       fl.STATUS IN ('INVALID', 'AMBIGUOUS'),
       m.ISO3 IS NULL,
       a.POST_CODE IS NOT NULL AND NOT REGEXP_LIKE(a.POST_CODE, '[0-9]{1,5}'),
       LENGTH(a.MEMBER_ID) < p.EXPECTED_MEMBER_ID_LENGTH,
       a.ACTIVE_MEMBER IS NOT NULL
           AND NOT EXISTS (SELECT 1 FROM DQ.CFG_ACTIVE c WHERE c.ACTIVE_VALUE = a.ACTIVE_MEMBER),
       a.FILE_NAME, a.FILE_ROW_NUMBER, a.FILE_TS, a.BATCH_ID
FROM DQ.V_ACCEPTED_ROW a
CROSS JOIN DQ.RULE_PARAM p
JOIN DQ.WORK_DATE enr ON enr.FILE_NAME = a.FILE_NAME AND enr.FILE_ROW_NUMBER = a.FILE_ROW_NUMBER
                     AND EQUAL_NULL(enr.BATCH_ID, a.BATCH_ID) AND enr.COLUMN_NAME = 'enrollment_date'
JOIN DQ.WORK_DATE fl  ON fl.FILE_NAME = a.FILE_NAME AND fl.FILE_ROW_NUMBER = a.FILE_ROW_NUMBER
                     AND EQUAL_NULL(fl.BATCH_ID, a.BATCH_ID) AND fl.COLUMN_NAME = 'last_flight_date'
JOIN DQ.WORK_DATE dob ON dob.FILE_NAME = a.FILE_NAME AND dob.FILE_ROW_NUMBER = a.FILE_ROW_NUMBER
                     AND EQUAL_NULL(dob.BATCH_ID, a.BATCH_ID) AND dob.COLUMN_NAME = 'date_of_birth'
LEFT JOIN DQ.CFG_COUNTRY_MAP m ON m.ALIAS = UPPER(a.COUNTRY)
WHERE a.FILE_TS IS NOT NULL
  AND a.SOURCE_SYSTEM IS NOT NULL
  AND a.MEMBER_KEY IS NOT NULL
  AND enr.PARSED_DATE IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM STAGING.MEMBER_STG s
                  WHERE s.FILE_NAME = a.FILE_NAME AND s.FILE_ROW_NUMBER = a.FILE_ROW_NUMBER);
