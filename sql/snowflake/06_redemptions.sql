-- =============================================================================
-- 06_redemptions.sql  (Snowflake)
-- Purpose : Redemption feed: RAW.REDEMPTION_FEED_RAW (one VARIANT per member object) ->
--           flattened with LATERAL FLATTEN -> validated STAGING.REDEMPTION_STG ->
--           CORE.REDEMPTION_TXN -> MART.MEMBER_REDEMPTION_SUMMARY.
--           Same rules and names as sql/duckdb/transform/40..47_*.sql and
--           sql/duckdb/06_redemption_summary.sql, which are the versions the test suite runs
--           (DuckDB: read_json_objects + UNNEST). This file is NOT executed by the tests:
--           review it with care before the first run.
-- Loading : RAW.LOAD_REDEMPTION_FEED(batch_id) in 01_raw.sql copies the files from the stage
--           (STRIP_OUTER_ARRAY = TRUE, so one row per member object). A file name already in
--           raw is not copied again.
-- Rules   : RD001 WARN    member with an empty redemptions array       -> DQ.ROW_FINDING
--           RD002 REJECT  duplicate txn_id, older version              -> DQ.QUARANTINE
--           RD003 REJECT  miles_redeemed < 0                           -> DQ.QUARANTINE
--           RD004 REJECT  status not an allowed status (REF.REDEMPTION_STATUS) -> DQ.QUARANTINE
--           RD005 WARN    txn_date after feed_date (row still staged)  -> DQ.ROW_FINDING
--           RD006 REJECT  a required value missing or unreadable       -> DQ.QUARANTINE
-- Key     : member_key = 'SKYPOINTS:' || member_id (ADR 0002). Redemptions join to the member
--           FEED ids, so a redemption with no matching member is an orphan, kept and visible.
-- =============================================================================

USE DATABASE SKYPOINTS;

-- Allowed statuses: config, not code.
CREATE TABLE IF NOT EXISTS REF.REDEMPTION_STATUS (STATUS VARCHAR(20) PRIMARY KEY);
MERGE INTO REF.REDEMPTION_STATUS t
USING (SELECT * FROM VALUES ('COMPLETED'), ('PENDING') AS v(STATUS)) s ON t.STATUS = s.STATUS
WHEN NOT MATCHED THEN INSERT (STATUS) VALUES (s.STATUS);

-- ---------------------------------------------------------------------------
-- 1. Flatten: one row per transaction, every value as text. FLATTEN explodes the redemptions
--    array; f.INDEX is 0-based, so +1 is the position inside the member. A member with an empty
--    or missing array produces no row (no OUTER => TRUE), but is listed by V_REDEMPTION_MEMBER.
--    Casting a VARIANT number or string to VARCHAR keeps 7890 as '7890' and "007890" as '007890'.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW STAGING.V_REDEMPTION_FLAT AS
SELECT r.PAYLOAD:member_id::VARCHAR        AS MEMBER_ID,
       r.PAYLOAD:feed_date::VARCHAR        AS FEED_DATE,
       f.VALUE:txn_id::VARCHAR             AS TXN_ID,
       f.VALUE:txn_date::VARCHAR           AS TXN_DATE,
       f.VALUE:partner::VARCHAR            AS PARTNER,
       f.VALUE:miles_redeemed::VARCHAR     AS MILES_REDEEMED,
       f.VALUE:status::VARCHAR             AS STATUS,
       r.FILE_NAME, r.FILE_ROW_NUMBER, f.INDEX + 1 AS TXN_SEQ, r.FILE_TS, r.BATCH_ID
FROM RAW.REDEMPTION_FEED_RAW r,
     LATERAL FLATTEN(INPUT => r.PAYLOAD:redemptions) f;

CREATE OR REPLACE VIEW STAGING.V_REDEMPTION_MEMBER AS
SELECT PAYLOAD:member_id::VARCHAR AS MEMBER_ID, PAYLOAD:feed_date::VARCHAR AS FEED_DATE,
       COALESCE(ARRAY_SIZE(PAYLOAD:redemptions), 0) AS REDEMPTION_COUNT,
       FILE_NAME, FILE_ROW_NUMBER, FILE_TS, BATCH_ID
FROM RAW.REDEMPTION_FEED_RAW;

-- ---------------------------------------------------------------------------
-- 2. Validate and stage one batch. CALL STAGING.STAGE_REDEMPTIONS('<batch id>');
--    Newest version per txn_id wins by (feed_date, file_ts, file_row_number, txn_seq), against
--    the rest of the batch and against what is already staged. Nothing is dropped silently.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE STAGING.STAGE_REDEMPTIONS(BATCH_ID VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
AS
$$
BEGIN
    BEGIN TRANSACTION;

    CREATE OR REPLACE TEMPORARY TABLE STAGING.RD_CHECK AS
    WITH typed AS (
        SELECT f.*,
               IFF(REGEXP_LIKE(f.FEED_DATE, '[0-9]{8}'), TRY_TO_DATE(f.FEED_DATE, 'YYYYMMDD'), NULL) AS FEED_DATE_D,
               IFF(REGEXP_LIKE(f.TXN_DATE, '[0-9]{8}'), TRY_TO_DATE(f.TXN_DATE, 'YYYYMMDD'), NULL)   AS TXN_DATE_D,
               TRY_CAST(f.MILES_REDEEMED AS NUMBER(38,0))                                              AS MILES_I
        FROM STAGING.V_REDEMPTION_FLAT f
        WHERE f.BATCH_ID = :BATCH_ID
    )
    SELECT t.*,
           COALESCE(t.MILES_I < 0, FALSE) AS R3,
           (NULLIF(TRIM(t.STATUS), '') IS NULL
            OR NOT EXISTS (SELECT 1 FROM REF.REDEMPTION_STATUS s WHERE s.STATUS = TRIM(t.STATUS))) AS R4,
           (NULLIF(TRIM(t.MEMBER_ID), '') IS NULL OR NULLIF(TRIM(t.TXN_ID), '') IS NULL
            OR t.FEED_DATE_D IS NULL OR t.TXN_DATE_D IS NULL OR t.MILES_I IS NULL)                  AS R6
    FROM typed t;

    CREATE OR REPLACE TEMPORARY TABLE STAGING.RD_RANKED AS
    SELECT c.*, ROW_NUMBER() OVER (PARTITION BY c.TXN_ID
                                   ORDER BY c.FEED_DATE_D DESC, c.FILE_TS DESC,
                                            c.FILE_ROW_NUMBER DESC, c.TXN_SEQ DESC) AS RN
    FROM STAGING.RD_CHECK c
    WHERE NOT (c.R3 OR c.R4 OR c.R6);

    CREATE OR REPLACE TEMPORARY TABLE STAGING.RD_WINNER AS
    SELECT r.*,
           CASE WHEN s.TXN_ID IS NULL THEN 'INSERT'
                WHEN r.FEED_DATE_D > s.FEED_DATE
                  OR (r.FEED_DATE_D = s.FEED_DATE AND (r.FILE_TS > s.FILE_TS
                  OR (r.FILE_TS = s.FILE_TS AND (r.FILE_ROW_NUMBER > s.FILE_ROW_NUMBER
                  OR (r.FILE_ROW_NUMBER = s.FILE_ROW_NUMBER AND r.TXN_SEQ > s.TXN_SEQ))))) THEN 'REPLACE'
                ELSE 'DROP' END AS ACTION
    FROM STAGING.RD_RANKED r
    LEFT JOIN STAGING.REDEMPTION_STG s ON s.TXN_ID = r.TXN_ID
    WHERE r.RN = 1;

    -- Quarantine: rejected rows, older duplicates in the batch, a loser against the staged row,
    -- and the staged row that a newer version replaces. Original values kept as JSON.
    INSERT INTO DQ.QUARANTINE
        (SOURCE_TABLE, FILE_NAME, FILE_ROW_NUMBER, BATCH_ID, REASON_CODE, REASON_DETAIL, RULE_CODE,
         SEVERITY, RAW_PAYLOAD)
    SELECT 'RAW.REDEMPTION_FEED_RAW', FILE_NAME, FILE_ROW_NUMBER, BATCH_ID,
           CASE WHEN R3 THEN 'NEGATIVE_MILES' WHEN R4 THEN 'UNKNOWN_STATUS' ELSE 'INVALID_VALUE' END,
           CONCAT_WS('; ', IFF(R3, 'miles_redeemed is negative', NULL),
                           IFF(R4, 'status is not one of the allowed statuses', NULL),
                           IFF(R6, 'a required value is missing or unreadable', NULL)),
           CONCAT_WS(',', IFF(R3, 'RD003', NULL), IFF(R4, 'RD004', NULL), IFF(R6, 'RD006', NULL)),
           'REJECT',
           OBJECT_CONSTRUCT_KEEP_NULL('member_id', MEMBER_ID, 'feed_date', FEED_DATE, 'txn_id', TXN_ID,
                                      'txn_date', TXN_DATE, 'partner', PARTNER,
                                      'miles_redeemed', MILES_REDEEMED, 'status', STATUS)
    FROM STAGING.RD_CHECK WHERE R3 OR R4 OR R6
    UNION ALL
    SELECT 'RAW.REDEMPTION_FEED_RAW', FILE_NAME, FILE_ROW_NUMBER, BATCH_ID, 'DUPLICATE_TXN_OLDER',
           'txn_id ' || TXN_ID || ' has a newer version; this older one is dropped', 'RD002', 'REJECT',
           OBJECT_CONSTRUCT_KEEP_NULL('member_id', MEMBER_ID, 'feed_date', FEED_DATE, 'txn_id', TXN_ID,
                                      'txn_date', TXN_DATE, 'partner', PARTNER,
                                      'miles_redeemed', MILES_REDEEMED, 'status', STATUS)
    FROM STAGING.RD_RANKED WHERE RN > 1
    UNION ALL
    SELECT 'RAW.REDEMPTION_FEED_RAW', FILE_NAME, FILE_ROW_NUMBER, BATCH_ID, 'DUPLICATE_TXN_OLDER',
           'txn_id ' || TXN_ID || ' is already staged with a newer version; this older one is dropped',
           'RD002', 'REJECT',
           OBJECT_CONSTRUCT_KEEP_NULL('member_id', MEMBER_ID, 'feed_date', FEED_DATE, 'txn_id', TXN_ID,
                                      'txn_date', TXN_DATE, 'partner', PARTNER,
                                      'miles_redeemed', MILES_REDEEMED, 'status', STATUS)
    FROM STAGING.RD_WINNER WHERE ACTION = 'DROP'
    UNION ALL
    SELECT 'RAW.REDEMPTION_FEED_RAW', s.FILE_NAME, s.FILE_ROW_NUMBER, w.BATCH_ID, 'DUPLICATE_TXN_OLDER',
           'txn_id ' || s.TXN_ID || ' was replaced by a newer version', 'RD002', 'REJECT',
           OBJECT_CONSTRUCT_KEEP_NULL('member_id', s.MEMBER_ID, 'feed_date', s.FEED_DATE::VARCHAR,
                                      'txn_id', s.TXN_ID, 'txn_date', s.TXN_DATE::VARCHAR,
                                      'partner', s.PARTNER, 'miles_redeemed', s.MILES_REDEEMED::VARCHAR,
                                      'status', s.STATUS)
    FROM STAGING.RD_WINNER w JOIN STAGING.REDEMPTION_STG s ON s.TXN_ID = w.TXN_ID
    WHERE w.ACTION = 'REPLACE';

    -- Stage: MERGE replaces the staged version in place and inserts the new ones.
    MERGE INTO STAGING.REDEMPTION_STG t
    USING (SELECT * FROM STAGING.RD_WINNER WHERE ACTION IN ('INSERT', 'REPLACE')) w
       ON t.TXN_ID = TRIM(w.TXN_ID)
    WHEN MATCHED THEN UPDATE SET
        MEMBER_ID = TRIM(w.MEMBER_ID), MEMBER_KEY = 'SKYPOINTS:' || TRIM(w.MEMBER_ID),
        FEED_DATE = w.FEED_DATE_D, TXN_DATE = w.TXN_DATE_D, PARTNER = NULLIF(TRIM(w.PARTNER), ''),
        MILES_REDEEMED = w.MILES_I, STATUS = TRIM(w.STATUS), DQ_TXN_AFTER_FEED = w.TXN_DATE_D > w.FEED_DATE_D,
        FILE_NAME = w.FILE_NAME, FILE_ROW_NUMBER = w.FILE_ROW_NUMBER, TXN_SEQ = w.TXN_SEQ,
        FILE_TS = w.FILE_TS, BATCH_ID = w.BATCH_ID
    WHEN NOT MATCHED THEN INSERT
        (MEMBER_ID, MEMBER_KEY, FEED_DATE, TXN_ID, TXN_DATE, PARTNER, MILES_REDEEMED, STATUS,
         DQ_TXN_AFTER_FEED, FILE_NAME, FILE_ROW_NUMBER, TXN_SEQ, FILE_TS, BATCH_ID)
    VALUES
        (TRIM(w.MEMBER_ID), 'SKYPOINTS:' || TRIM(w.MEMBER_ID), w.FEED_DATE_D, TRIM(w.TXN_ID), w.TXN_DATE_D,
         NULLIF(TRIM(w.PARTNER), ''), w.MILES_I, TRIM(w.STATUS), w.TXN_DATE_D > w.FEED_DATE_D,
         w.FILE_NAME, w.FILE_ROW_NUMBER, w.TXN_SEQ, w.FILE_TS, w.BATCH_ID);

    -- RD001: a member object with no transactions is recorded, not rejected.
    INSERT INTO DQ.ROW_FINDING
        (BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, RULE_CODE, SEVERITY, COLUMN_NAME, VALUE, REASON)
    SELECT BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, 'SKYPOINTS:' || MEMBER_ID, 'RD001', 'WARN', NULL, NULL,
           'the member has an empty redemptions array'
    FROM STAGING.V_REDEMPTION_MEMBER
    WHERE BATCH_ID = :BATCH_ID AND REDEMPTION_COUNT = 0;

    -- RD005: txn_date after feed_date; the row is staged with the flag set.
    INSERT INTO DQ.ROW_FINDING
        (BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, RULE_CODE, SEVERITY, COLUMN_NAME, VALUE, REASON)
    SELECT BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, 'SKYPOINTS:' || TRIM(MEMBER_ID), 'RD005', 'WARN',
           'txn_date', TXN_DATE, 'txn_date is after feed_date'
    FROM STAGING.RD_WINNER
    WHERE ACTION IN ('INSERT', 'REPLACE') AND TXN_DATE_D > FEED_DATE_D;

    COMMIT;
    RETURN 'redemptions staged for batch ' || :BATCH_ID;
END;
$$;

-- ---------------------------------------------------------------------------
-- 3. Promote STAGING.REDEMPTION_STG -> CORE.REDEMPTION_TXN (one row per txn_id).
--    UPDATE only when STRICTLY newer; re-running changes nothing. NULL batch = all staged rows.
--    CALL CORE.PROMOTE_REDEMPTIONS('<batch id>');
-- ---------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE CORE.PROMOTE_REDEMPTIONS(BATCH_ID VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
AS
$$
BEGIN
    MERGE INTO CORE.REDEMPTION_TXN t
    USING (SELECT * FROM STAGING.REDEMPTION_STG WHERE :BATCH_ID IS NULL OR BATCH_ID = :BATCH_ID) s
       ON t.TXN_ID = s.TXN_ID
    WHEN MATCHED AND (s.FEED_DATE > t.FEED_DATE
         OR (s.FEED_DATE = t.FEED_DATE AND (s.FILE_TS > t.FILE_TS
         OR (s.FILE_TS = t.FILE_TS AND (s.FILE_ROW_NUMBER > t.FILE_ROW_NUMBER
         OR (s.FILE_ROW_NUMBER = t.FILE_ROW_NUMBER AND s.TXN_SEQ > t.TXN_SEQ)))))) THEN UPDATE SET
        MEMBER_ID = s.MEMBER_ID, MEMBER_KEY = s.MEMBER_KEY, FEED_DATE = s.FEED_DATE, TXN_DATE = s.TXN_DATE,
        PARTNER = s.PARTNER, MILES_REDEEMED = s.MILES_REDEEMED, STATUS = s.STATUS,
        DQ_TXN_AFTER_FEED = s.DQ_TXN_AFTER_FEED, FILE_NAME = s.FILE_NAME, FILE_ROW_NUMBER = s.FILE_ROW_NUMBER,
        TXN_SEQ = s.TXN_SEQ, FILE_TS = s.FILE_TS, BATCH_ID = s.BATCH_ID, UPDATED_AT = CURRENT_TIMESTAMP()
    WHEN NOT MATCHED THEN INSERT
        (TXN_ID, MEMBER_ID, MEMBER_KEY, FEED_DATE, TXN_DATE, PARTNER, MILES_REDEEMED, STATUS,
         DQ_TXN_AFTER_FEED, FILE_NAME, FILE_ROW_NUMBER, TXN_SEQ, FILE_TS, BATCH_ID)
    VALUES
        (s.TXN_ID, s.MEMBER_ID, s.MEMBER_KEY, s.FEED_DATE, s.TXN_DATE, s.PARTNER, s.MILES_REDEEMED, s.STATUS,
         s.DQ_TXN_AFTER_FEED, s.FILE_NAME, s.FILE_ROW_NUMBER, s.TXN_SEQ, s.FILE_TS, s.BATCH_ID);
    RETURN 'redemptions promoted';
END;
$$;

-- ---------------------------------------------------------------------------
-- 4. MART.MEMBER_REDEMPTION_SUMMARY: one row per member_key with redemptions.
--    LEFT JOIN from the transactions, so an orphan is visible (MEMBER_MATCHED = FALSE, no name,
--    no country). Joined on member_key, not on the bare member_id, which repeats across sources.
--    CURRENT_COUNTRY_CODE is the member's country now, NULL when unassigned (not active in
--    REF.COUNTRY_CODE_MAP). LAST_REDEMPTION_DATE is the latest txn_date over all statuses.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW MART.MEMBER_REDEMPTION_SUMMARY AS
SELECT t.MEMBER_KEY,
       t.MEMBER_ID,
       (m.MEMBER_KEY IS NOT NULL)                                         AS MEMBER_MATCHED,
       m.MEMBER_NAME,
       IFF(m.COUNTRY_CODE IN (SELECT COUNTRY_CODE FROM REF.COUNTRY_CODE_MAP WHERE IS_ACTIVE),
           m.COUNTRY_CODE, NULL)                                          AS CURRENT_COUNTRY_CODE,
       COALESCE(SUM(IFF(t.STATUS = 'COMPLETED', t.MILES_REDEEMED, NULL)), 0) AS TOTAL_COMPLETED_MILES,
       COALESCE(SUM(IFF(t.STATUS = 'PENDING', t.MILES_REDEEMED, NULL)), 0)   AS PENDING_MILES,
       COUNT(*)                                                           AS TXN_COUNT,
       MAX(t.TXN_DATE)                                                    AS LAST_REDEMPTION_DATE
FROM CORE.REDEMPTION_TXN t
LEFT JOIN CORE.MEMBER_CURRENT m ON m.MEMBER_KEY = t.MEMBER_KEY
GROUP BY t.MEMBER_KEY, t.MEMBER_ID, m.MEMBER_KEY, m.MEMBER_NAME, m.COUNTRY_CODE;
