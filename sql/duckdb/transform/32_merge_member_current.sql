-- =============================================================================
-- 32_merge_member_current.sql  (DuckDB)
-- Purpose : Step 1c. Merge the candidates (_split_candidate) into CORE.MEMBER_CURRENT.
--           1. Keep ONE record per member_key: the newest by (file_ts, file_row_number), with
--              file_name as a last, repeatable tie-break (QUALIFY ROW_NUMBER() = 1).
--           2. Compare it with the CORE row: INSERT when new, UPDATE only when STRICTLY newer by
--              (file_ts, file_row_number), STALE when older, UNCHANGED when equal.
--           3. Record a CORE.MEMBER_COUNTRY_HISTORY row for every update that changes the
--              country. First appearances are not moves. Unassigned counts as a value: the
--              effective country is the country_code if it is active in REF.COUNTRY_CODE_MAP,
--              else NULL. Moves inside one batch collapse to one because step 1 already
--              reduced the batch to one record per member.
-- No MERGE statement: DuckDB 1.1.3 has none, so UPDATE + INSERT run in one transaction (the
-- caller wraps them). sql/snowflake/04_country_split.sql has the MERGE version.
-- Leaves _split_decision for the caller to count results and read the moves.
-- =============================================================================

CREATE OR REPLACE TEMP TABLE _split_winner AS
SELECT * FROM _split_candidate
QUALIFY row_number() OVER (PARTITION BY MEMBER_KEY
                           ORDER BY FILE_TS DESC, FILE_ROW_NUMBER DESC, FILE_NAME DESC) = 1;

CREATE OR REPLACE TEMP TABLE _split_decision AS
SELECT w.MEMBER_KEY, w.MEMBER_ID, w.FILE_TS, w.BATCH_ID,
       CASE WHEN c.MEMBER_KEY IS NULL THEN 'INSERT'
            WHEN w.FILE_TS > c.FILE_TS
              OR (w.FILE_TS = c.FILE_TS AND w.FILE_ROW_NUMBER > c.FILE_ROW_NUMBER) THEN 'UPDATE'
            WHEN w.FILE_TS < c.FILE_TS
              OR (w.FILE_TS = c.FILE_TS AND w.FILE_ROW_NUMBER < c.FILE_ROW_NUMBER) THEN 'STALE'
            ELSE 'UNCHANGED' END AS ACTION,
       CASE WHEN c.COUNTRY_CODE IN (SELECT COUNTRY_CODE FROM REF.COUNTRY_CODE_MAP WHERE IS_ACTIVE)
            THEN c.COUNTRY_CODE END AS OLD_COUNTRY,
       CASE WHEN w.COUNTRY_CODE IN (SELECT COUNTRY_CODE FROM REF.COUNTRY_CODE_MAP WHERE IS_ACTIVE)
            THEN w.COUNTRY_CODE END AS NEW_COUNTRY
FROM _split_winner w
LEFT JOIN CORE.MEMBER_CURRENT c ON c.MEMBER_KEY = w.MEMBER_KEY;

-- Must run before the UPDATE: the history needs the country the member had.
INSERT INTO CORE.MEMBER_COUNTRY_HISTORY
    (MEMBER_KEY, MEMBER_ID, OLD_COUNTRY_CODE, NEW_COUNTRY_CODE, EFFECTIVE_FILE_TS, BATCH_ID)
SELECT MEMBER_KEY, MEMBER_ID, OLD_COUNTRY, NEW_COUNTRY, FILE_TS, BATCH_ID
FROM _split_decision
WHERE ACTION = 'UPDATE' AND OLD_COUNTRY IS DISTINCT FROM NEW_COUNTRY;

UPDATE CORE.MEMBER_CURRENT c
SET MEMBER_NAME = w.MEMBER_NAME, MEMBER_ID = w.MEMBER_ID, ENROLLMENT_DATE = w.ENROLLMENT_DATE,
    LAST_FLIGHT_DATE = w.LAST_FLIGHT_DATE, TIER_CODE = w.TIER_CODE, AGENT_NAME = w.AGENT_NAME,
    STATE = w.STATE, COUNTRY = w.COUNTRY, POST_CODE = w.POST_CODE, DATE_OF_BIRTH = w.DATE_OF_BIRTH,
    ACTIVE_MEMBER = w.ACTIVE_MEMBER, SOURCE_SYSTEM = w.SOURCE_SYSTEM, COUNTRY_CODE = w.COUNTRY_CODE,
    AS_OF_DATE = w.AS_OF_DATE, AGE = w.AGE, STALE_MEMBER = w.STALE_MEMBER,
    DQ_DOB_INVALID = w.DQ_DOB_INVALID, DQ_LAST_FLIGHT_DATE_INVALID = w.DQ_LAST_FLIGHT_DATE_INVALID,
    DQ_COUNTRY_UNKNOWN = w.DQ_COUNTRY_UNKNOWN, DQ_POST_CODE_INVALID = w.DQ_POST_CODE_INVALID,
    DQ_MEMBER_ID_SHORT = w.DQ_MEMBER_ID_SHORT, DQ_ACTIVE_FLAG_UNKNOWN = w.DQ_ACTIVE_FLAG_UNKNOWN,
    FILE_NAME = w.FILE_NAME, FILE_ROW_NUMBER = w.FILE_ROW_NUMBER, FILE_TS = w.FILE_TS,
    BATCH_ID = w.BATCH_ID, UPDATED_AT = CURRENT_TIMESTAMP
FROM _split_winner w JOIN _split_decision d ON d.MEMBER_KEY = w.MEMBER_KEY
WHERE c.MEMBER_KEY = w.MEMBER_KEY AND d.ACTION = 'UPDATE';

INSERT INTO CORE.MEMBER_CURRENT
    (MEMBER_NAME, MEMBER_ID, ENROLLMENT_DATE, LAST_FLIGHT_DATE, TIER_CODE, AGENT_NAME, STATE,
     COUNTRY, POST_CODE, DATE_OF_BIRTH, ACTIVE_MEMBER, SOURCE_SYSTEM, MEMBER_KEY, COUNTRY_CODE,
     AS_OF_DATE, AGE, STALE_MEMBER,
     DQ_DOB_INVALID, DQ_LAST_FLIGHT_DATE_INVALID, DQ_COUNTRY_UNKNOWN, DQ_POST_CODE_INVALID,
     DQ_MEMBER_ID_SHORT, DQ_ACTIVE_FLAG_UNKNOWN,
     FILE_NAME, FILE_ROW_NUMBER, FILE_TS, BATCH_ID)
SELECT w.MEMBER_NAME, w.MEMBER_ID, w.ENROLLMENT_DATE, w.LAST_FLIGHT_DATE, w.TIER_CODE, w.AGENT_NAME,
       w.STATE, w.COUNTRY, w.POST_CODE, w.DATE_OF_BIRTH, w.ACTIVE_MEMBER, w.SOURCE_SYSTEM,
       w.MEMBER_KEY, w.COUNTRY_CODE, w.AS_OF_DATE, w.AGE, w.STALE_MEMBER,
       w.DQ_DOB_INVALID, w.DQ_LAST_FLIGHT_DATE_INVALID, w.DQ_COUNTRY_UNKNOWN, w.DQ_POST_CODE_INVALID,
       w.DQ_MEMBER_ID_SHORT, w.DQ_ACTIVE_FLAG_UNKNOWN,
       w.FILE_NAME, w.FILE_ROW_NUMBER, w.FILE_TS, w.BATCH_ID
FROM _split_winner w JOIN _split_decision d ON d.MEMBER_KEY = w.MEMBER_KEY
WHERE d.ACTION = 'INSERT';
