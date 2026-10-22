-- =============================================================================
-- 04_country_split.sql  (Snowflake)
-- Purpose : STAGING.MEMBER_STG -> CORE.MEMBER_CURRENT -> CORE.MEMBER_COUNTRY_HISTORY and the
--           per-country tables MART.MEMBERS_<ISO3> / MART.MEMBERS_UNASSIGNED.
--           Same rules as sql/duckdb/transform/30..34_*.sql and skypoints.transform.country_split
--           (the version the test suite runs). This file is NOT executed by the tests: review it
--           with care before the first run.
-- Order   : the file name starts with 04_ like 04_member_prepared.sql; the two are independent.
-- Key     : MEMBER_KEY (source_system:member_id, ADR 0002), not MEMBER_ID: member_id repeats
--           across source systems, so partitioning by it would merge different people.
-- Rules   : * one record per member: ROW_NUMBER() OVER (PARTITION BY MEMBER_KEY
--             ORDER BY FILE_TS DESC, FILE_ROW_NUMBER DESC) = 1 (QUALIFY);
--           * the CORE row is replaced only if the record is STRICTLY newer by
--             (FILE_TS, FILE_ROW_NUMBER); equal or older records change nothing;
--           * country tables are derived from CORE and driven by REF.COUNTRY_CODE_MAP: no
--             country list in this file; a country added to the map gets its table;
--           * unassigned = COUNTRY_CODE NULL or not an active code in the map;
--           * history: one row per country change against the previous CORE state; a first
--             appearance is not a move; unassigned counts as a value (NULL).
-- Parts   : 1. guards and merge into CORE   2. full refresh of the country tables (initial load,
--           repair)   3. INCREMENTAL refresh: STREAM on CORE.MEMBER_CURRENT + TASK.
-- =============================================================================

USE DATABASE SKYPOINTS;

-- ---------------------------------------------------------------------------
-- Part 1. Merge staging into CORE.MEMBER_CURRENT
-- ---------------------------------------------------------------------------
-- Usage:  CALL CORE.MERGE_MEMBER_CURRENT('<batch id>');   -- or NULL for all staging rows
-- Raises if the batch has an open FAIL_BATCH finding, or if the newest record of a member is
-- not unique (same FILE_TS and FILE_ROW_NUMBER, different content). Nothing is written then.
CREATE OR REPLACE PROCEDURE CORE.MERGE_MEMBER_CURRENT(BATCH_ID VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
AS
$$
DECLARE
    blocked   NUMBER;
    conflicts NUMBER;
    blocked_batch EXCEPTION (-20001, 'A FAIL_BATCH finding is open for this batch');
    ambiguous     EXCEPTION (-20002, 'The newest record of a member is not unique');
BEGIN
    -- Guard 1: nothing resolves a FAIL_BATCH finding, so any recorded one is open.
    SELECT COUNT(*) INTO :blocked FROM DQ.ROW_FINDING
    WHERE SEVERITY = 'FAIL_BATCH'
      AND IFF(:BATCH_ID IS NULL, BATCH_ID IN (SELECT BATCH_ID FROM STAGING.MEMBER_STG), BATCH_ID = :BATCH_ID);
    IF (blocked > 0) THEN RAISE blocked_batch; END IF;

    -- Candidates: the batch (or everything) from staging.
    CREATE OR REPLACE TEMPORARY TABLE CORE.SPLIT_CANDIDATE AS
    SELECT * FROM STAGING.MEMBER_STG WHERE :BATCH_ID IS NULL OR BATCH_ID = :BATCH_ID;

    -- Guard 2: tie at the top with different content. Identical content collapses silently.
    SELECT COUNT(*) INTO :conflicts FROM (
        SELECT MEMBER_KEY FROM (
            SELECT c.*, RANK() OVER (PARTITION BY MEMBER_KEY ORDER BY FILE_TS DESC, FILE_ROW_NUMBER DESC) AS RNK
            FROM CORE.SPLIT_CANDIDATE c)
        WHERE RNK = 1
        GROUP BY MEMBER_KEY, FILE_TS, FILE_ROW_NUMBER
        HAVING COUNT(DISTINCT MD5(CONCAT_WS('|',
            COALESCE(MEMBER_NAME, '<null>'), COALESCE(MEMBER_ID, '<null>'),
            COALESCE(ENROLLMENT_DATE::VARCHAR, '<null>'), COALESCE(LAST_FLIGHT_DATE::VARCHAR, '<null>'),
            COALESCE(TIER_CODE, '<null>'), COALESCE(AGENT_NAME, '<null>'), COALESCE(STATE, '<null>'),
            COALESCE(COUNTRY, '<null>'), COALESCE(POST_CODE, '<null>'), COALESCE(DATE_OF_BIRTH::VARCHAR, '<null>'),
            COALESCE(ACTIVE_MEMBER, '<null>'), COALESCE(SOURCE_SYSTEM, '<null>'), COALESCE(COUNTRY_CODE, '<null>'),
            COALESCE(AS_OF_DATE::VARCHAR, '<null>'), COALESCE(AGE::VARCHAR, '<null>'),
            COALESCE(STALE_MEMBER::VARCHAR, '<null>'), COALESCE(DQ_DOB_INVALID::VARCHAR, '<null>'),
            COALESCE(DQ_LAST_FLIGHT_DATE_INVALID::VARCHAR, '<null>'), COALESCE(DQ_COUNTRY_UNKNOWN::VARCHAR, '<null>'),
            COALESCE(DQ_POST_CODE_INVALID::VARCHAR, '<null>'), COALESCE(DQ_MEMBER_ID_SHORT::VARCHAR, '<null>'),
            COALESCE(DQ_ACTIVE_FLAG_UNKNOWN::VARCHAR, '<null>')))) > 1);
    IF (conflicts > 0) THEN RAISE ambiguous; END IF;

    BEGIN TRANSACTION;

    -- The newest record per member (QUALIFY), with the decision against the current CORE row.
    -- Effective country = the code if it is active in the map, else NULL (unassigned).
    CREATE OR REPLACE TEMPORARY TABLE CORE.SPLIT_DECISION AS
    SELECT w.*,
           CASE WHEN c.MEMBER_KEY IS NULL THEN 'INSERT'
                WHEN w.FILE_TS > c.FILE_TS
                  OR (w.FILE_TS = c.FILE_TS AND w.FILE_ROW_NUMBER > c.FILE_ROW_NUMBER) THEN 'UPDATE'
                WHEN w.FILE_TS < c.FILE_TS
                  OR (w.FILE_TS = c.FILE_TS AND w.FILE_ROW_NUMBER < c.FILE_ROW_NUMBER) THEN 'STALE'
                ELSE 'UNCHANGED' END AS ACTION,
           IFF(c.COUNTRY_CODE IN (SELECT COUNTRY_CODE FROM REF.COUNTRY_CODE_MAP WHERE IS_ACTIVE),
               c.COUNTRY_CODE, NULL) AS OLD_COUNTRY,
           IFF(w.COUNTRY_CODE IN (SELECT COUNTRY_CODE FROM REF.COUNTRY_CODE_MAP WHERE IS_ACTIVE),
               w.COUNTRY_CODE, NULL) AS NEW_COUNTRY
    FROM (SELECT * FROM CORE.SPLIT_CANDIDATE
          QUALIFY ROW_NUMBER() OVER (PARTITION BY MEMBER_KEY
                                     ORDER BY FILE_TS DESC, FILE_ROW_NUMBER DESC, FILE_NAME DESC) = 1) w
    LEFT JOIN CORE.MEMBER_CURRENT c ON c.MEMBER_KEY = w.MEMBER_KEY;

    -- History first: it needs the country the member had before the MERGE.
    INSERT INTO CORE.MEMBER_COUNTRY_HISTORY
        (MEMBER_KEY, MEMBER_ID, OLD_COUNTRY_CODE, NEW_COUNTRY_CODE, EFFECTIVE_FILE_TS, BATCH_ID)
    SELECT MEMBER_KEY, MEMBER_ID, OLD_COUNTRY, NEW_COUNTRY, FILE_TS, BATCH_ID
    FROM CORE.SPLIT_DECISION
    WHERE ACTION = 'UPDATE' AND NOT EQUAL_NULL(OLD_COUNTRY, NEW_COUNTRY);

    MERGE INTO CORE.MEMBER_CURRENT t
    USING (SELECT * FROM CORE.SPLIT_DECISION WHERE ACTION IN ('INSERT', 'UPDATE')) s
       ON t.MEMBER_KEY = s.MEMBER_KEY
    WHEN MATCHED AND s.ACTION = 'UPDATE' THEN UPDATE SET
        MEMBER_NAME = s.MEMBER_NAME, MEMBER_ID = s.MEMBER_ID, ENROLLMENT_DATE = s.ENROLLMENT_DATE,
        LAST_FLIGHT_DATE = s.LAST_FLIGHT_DATE, TIER_CODE = s.TIER_CODE, AGENT_NAME = s.AGENT_NAME,
        STATE = s.STATE, COUNTRY = s.COUNTRY, POST_CODE = s.POST_CODE, DATE_OF_BIRTH = s.DATE_OF_BIRTH,
        ACTIVE_MEMBER = s.ACTIVE_MEMBER, SOURCE_SYSTEM = s.SOURCE_SYSTEM, COUNTRY_CODE = s.COUNTRY_CODE,
        AS_OF_DATE = s.AS_OF_DATE, AGE = s.AGE, STALE_MEMBER = s.STALE_MEMBER,
        DQ_DOB_INVALID = s.DQ_DOB_INVALID, DQ_LAST_FLIGHT_DATE_INVALID = s.DQ_LAST_FLIGHT_DATE_INVALID,
        DQ_COUNTRY_UNKNOWN = s.DQ_COUNTRY_UNKNOWN, DQ_POST_CODE_INVALID = s.DQ_POST_CODE_INVALID,
        DQ_MEMBER_ID_SHORT = s.DQ_MEMBER_ID_SHORT, DQ_ACTIVE_FLAG_UNKNOWN = s.DQ_ACTIVE_FLAG_UNKNOWN,
        FILE_NAME = s.FILE_NAME, FILE_ROW_NUMBER = s.FILE_ROW_NUMBER, FILE_TS = s.FILE_TS,
        BATCH_ID = s.BATCH_ID, UPDATED_AT = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT
        (MEMBER_NAME, MEMBER_ID, ENROLLMENT_DATE, LAST_FLIGHT_DATE, TIER_CODE, AGENT_NAME, STATE,
         COUNTRY, POST_CODE, DATE_OF_BIRTH, ACTIVE_MEMBER, SOURCE_SYSTEM, MEMBER_KEY, COUNTRY_CODE,
         AS_OF_DATE, AGE, STALE_MEMBER,
         DQ_DOB_INVALID, DQ_LAST_FLIGHT_DATE_INVALID, DQ_COUNTRY_UNKNOWN, DQ_POST_CODE_INVALID,
         DQ_MEMBER_ID_SHORT, DQ_ACTIVE_FLAG_UNKNOWN, FILE_NAME, FILE_ROW_NUMBER, FILE_TS, BATCH_ID)
    VALUES
        (s.MEMBER_NAME, s.MEMBER_ID, s.ENROLLMENT_DATE, s.LAST_FLIGHT_DATE, s.TIER_CODE, s.AGENT_NAME, s.STATE,
         s.COUNTRY, s.POST_CODE, s.DATE_OF_BIRTH, s.ACTIVE_MEMBER, s.SOURCE_SYSTEM, s.MEMBER_KEY, s.COUNTRY_CODE,
         s.AS_OF_DATE, s.AGE, s.STALE_MEMBER,
         s.DQ_DOB_INVALID, s.DQ_LAST_FLIGHT_DATE_INVALID, s.DQ_COUNTRY_UNKNOWN, s.DQ_POST_CODE_INVALID,
         s.DQ_MEMBER_ID_SHORT, s.DQ_ACTIVE_FLAG_UNKNOWN, s.FILE_NAME, s.FILE_ROW_NUMBER, s.FILE_TS, s.BATCH_ID);

    COMMIT;
    RETURN (SELECT 'inserted ' || COUNT_IF(ACTION = 'INSERT') || ', updated ' || COUNT_IF(ACTION = 'UPDATE')
                   || ', stale ignored ' || COUNT_IF(ACTION = 'STALE') FROM CORE.SPLIT_DECISION);
END;
$$;

-- ---------------------------------------------------------------------------
-- Part 2. Full refresh of the country tables (initial load or repair)
-- ---------------------------------------------------------------------------
-- One MART.MEMBERS_<ISO3> per DISTINCT active COUNTRY_CODE in REF.COUNTRY_CODE_MAP, created on
-- demand, rebuilt from CORE.MEMBER_CURRENT with INSERT OVERWRITE, so a member who moved is gone
-- from the old table. MEMBERS_UNASSIGNED gets everyone whose code is NULL or not active.
CREATE OR REPLACE PROCEDURE MART.REFRESH_COUNTRY_TABLES_FULL()
RETURNS VARCHAR
LANGUAGE SQL
AS
$$
DECLARE
    countries CURSOR FOR SELECT DISTINCT COUNTRY_CODE FROM REF.COUNTRY_CODE_MAP WHERE IS_ACTIVE ORDER BY 1;
    n NUMBER DEFAULT 0;
BEGIN
    FOR rec IN countries DO
        LET tbl VARCHAR := 'MART.MEMBERS_' || rec.COUNTRY_CODE;
        EXECUTE IMMEDIATE 'CREATE TABLE IF NOT EXISTS ' || :tbl || ' LIKE CORE.MEMBER_CURRENT';
        EXECUTE IMMEDIATE 'ALTER TABLE ' || :tbl || ' CLUSTER BY (MEMBER_KEY)';
        EXECUTE IMMEDIATE 'INSERT OVERWRITE INTO ' || :tbl
            || ' SELECT * FROM CORE.MEMBER_CURRENT WHERE COUNTRY_CODE = ''' || rec.COUNTRY_CODE || '''';
        n := n + 1;
    END FOR;
    INSERT OVERWRITE INTO MART.MEMBERS_UNASSIGNED
    SELECT * FROM CORE.MEMBER_CURRENT
    WHERE COUNTRY_CODE IS NULL
       OR COUNTRY_CODE NOT IN (SELECT COUNTRY_CODE FROM REF.COUNTRY_CODE_MAP WHERE IS_ACTIVE);
    RETURN n || ' country table(s) rebuilt';
END;
$$;

-- ---------------------------------------------------------------------------
-- Part 3. Incremental refresh: STREAM on CORE.MEMBER_CURRENT + TASK
-- ---------------------------------------------------------------------------
-- A standard stream shows each changed CORE row as METADATA$ACTION = 'INSERT' (the new image)
-- and, for an UPDATE or DELETE, also a 'DELETE' row (the old image; METADATA$ISUPDATE = TRUE
-- for the pair of an update). Only members CORE changed are touched, so the cost follows the
-- size of the batch, not the size of the member base.
CREATE STREAM IF NOT EXISTS CORE.MEMBER_CURRENT_STREAM ON TABLE CORE.MEMBER_CURRENT;

CREATE OR REPLACE PROCEDURE MART.REFRESH_COUNTRY_TABLES_INCREMENTAL()
RETURNS VARCHAR
LANGUAGE SQL
AS
$$
DECLARE
    countries CURSOR FOR SELECT DISTINCT COUNTRY_CODE FROM REF.COUNTRY_CODE_MAP WHERE IS_ACTIVE ORDER BY 1;
    changed NUMBER;
BEGIN
    BEGIN TRANSACTION;
    -- Reading the stream into a table inside the transaction consumes it when the transaction commits.
    CREATE OR REPLACE TEMPORARY TABLE MART.COUNTRY_CHANGE AS
    SELECT * FROM CORE.MEMBER_CURRENT_STREAM;
    SELECT COUNT(DISTINCT MEMBER_KEY) INTO :changed FROM MART.COUNTRY_CHANGE;

    -- 1. Make sure every active country has its table (a country added to the map).
    FOR rec IN countries DO
        EXECUTE IMMEDIATE 'CREATE TABLE IF NOT EXISTS MART.MEMBERS_' || rec.COUNTRY_CODE
            || ' LIKE CORE.MEMBER_CURRENT';
        EXECUTE IMMEDIATE 'ALTER TABLE MART.MEMBERS_' || rec.COUNTRY_CODE || ' CLUSTER BY (MEMBER_KEY)';
    END FOR;

    -- 2. Remove every changed member from wherever they were: this is how a moved member
    --    disappears from the old country table (old images = METADATA$ACTION 'DELETE').
    FOR rec IN countries DO
        EXECUTE IMMEDIATE 'DELETE FROM MART.MEMBERS_' || rec.COUNTRY_CODE
            || ' t USING (SELECT DISTINCT MEMBER_KEY FROM MART.COUNTRY_CHANGE) c WHERE t.MEMBER_KEY = c.MEMBER_KEY';
    END FOR;
    DELETE FROM MART.MEMBERS_UNASSIGNED t USING (SELECT DISTINCT MEMBER_KEY FROM MART.COUNTRY_CHANGE) c
    WHERE t.MEMBER_KEY = c.MEMBER_KEY;

    -- 3. Insert the new images where they belong.
    FOR rec IN countries DO
        EXECUTE IMMEDIATE 'INSERT INTO MART.MEMBERS_' || rec.COUNTRY_CODE
            || ' SELECT * EXCLUDE (METADATA$ACTION, METADATA$ISUPDATE, METADATA$ROW_ID)'
            || ' FROM MART.COUNTRY_CHANGE WHERE METADATA$ACTION = ''INSERT'' AND COUNTRY_CODE = '''
            || rec.COUNTRY_CODE || '''';
    END FOR;
    INSERT INTO MART.MEMBERS_UNASSIGNED
    SELECT * EXCLUDE (METADATA$ACTION, METADATA$ISUPDATE, METADATA$ROW_ID)
    FROM MART.COUNTRY_CHANGE
    WHERE METADATA$ACTION = 'INSERT'
      AND (COUNTRY_CODE IS NULL
           OR COUNTRY_CODE NOT IN (SELECT COUNTRY_CODE FROM REF.COUNTRY_CODE_MAP WHERE IS_ACTIVE));
    COMMIT;
    RETURN changed || ' member(s) refreshed';
END;
$$;

-- Runs only when CORE.MEMBER_CURRENT changed. Resume it once the full refresh above has been run:
--   ALTER TASK MART.REFRESH_COUNTRY_TABLES_TASK RESUME;
CREATE OR REPLACE TASK MART.REFRESH_COUNTRY_TABLES_TASK
    WAREHOUSE = SKYPOINTS_WH                              -- TODO: your warehouse
    SCHEDULE  = '5 MINUTE'
    WHEN SYSTEM$STREAM_HAS_DATA('CORE.MEMBER_CURRENT_STREAM')
AS
    CALL MART.REFRESH_COUNTRY_TABLES_INCREMENTAL();

-- Alternative: chain it to the load instead of polling, with AFTER <the task that runs
-- CORE.MERGE_MEMBER_CURRENT> so the country tables are refreshed right after each merge.
