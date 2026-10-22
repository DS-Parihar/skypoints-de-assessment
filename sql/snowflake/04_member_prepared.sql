-- =============================================================================
-- 04_member_prepared.sql  (Snowflake)
-- Purpose : STAGING.MEMBER_PREPARED_V, the cleaning and typing step in front of
--           STAGING.MEMBER_STG. It has no parameters; the two load statements in
--           sql/snowflake/transform/ read it and add the run parameters (as_of, batch_id).
-- Rules   :
--   * Blank and NULL are the same thing: every business text column goes through
--     NULLIF(TRIM(col), ''), so '', '   ' and NULL all become NULL.
--   * Dates are typed here only. member feed formats: enrollment and flight date
--     YYYYMMDD, DOB MMDDYYYY (ADR 0005). Anything else becomes NULL plus a DQ flag.
--     The IND/USA/AUS extract formats are NOT handled by this view.
--   * REJECT_REASON is NULL for a usable row. A non-NULL value sends the row to
--     DQ.QUARANTINE instead of staging; nothing is dropped.
-- Order of REJECT_REASON checks: first match wins.
-- Same logic and column names as sql/duckdb/04_member_prepared.sql.
-- =============================================================================

USE DATABASE SKYPOINTS;

CREATE OR REPLACE VIEW STAGING.MEMBER_PREPARED_V AS
WITH cleaned AS (
    SELECT
        NULLIF(TRIM(r.MEMBER_NAME), '')      AS MEMBER_NAME,
        NULLIF(TRIM(r.MEMBER_ID), '')        AS MEMBER_ID,
        NULLIF(TRIM(r.ENROLLMENT_DATE), '')  AS ENROLLMENT_DATE_TXT,
        NULLIF(TRIM(r.LAST_FLIGHT_DATE), '') AS LAST_FLIGHT_DATE_TXT,
        NULLIF(TRIM(r.TIER_CODE), '')        AS TIER_CODE,
        NULLIF(TRIM(r.AGENT_NAME), '')       AS AGENT_NAME,
        NULLIF(TRIM(r.STATE), '')            AS STATE,
        NULLIF(TRIM(r.COUNTRY), '')          AS COUNTRY,
        NULLIF(TRIM(r.POST_CODE), '')        AS POST_CODE_TXT,
        NULLIF(TRIM(r.DATE_OF_BIRTH), '')    AS DATE_OF_BIRTH_TXT,
        NULLIF(TRIM(r.ACTIVE_MEMBER), '')    AS ACTIVE_MEMBER,
        NULLIF(TRIM(r.SOURCE_SYSTEM), '')    AS SOURCE_SYSTEM,
        r.FILE_NAME, r.FILE_ROW_NUMBER, r.FILE_TS, r.BATCH_ID,
        OBJECT_CONSTRUCT_KEEP_NULL(
            'RECORD_TYPE', r.RECORD_TYPE, 'MEMBER_NAME', r.MEMBER_NAME, 'MEMBER_ID', r.MEMBER_ID,
            'ENROLLMENT_DATE', r.ENROLLMENT_DATE, 'LAST_FLIGHT_DATE', r.LAST_FLIGHT_DATE,
            'TIER_CODE', r.TIER_CODE, 'AGENT_NAME', r.AGENT_NAME, 'STATE', r.STATE,
            'COUNTRY', r.COUNTRY, 'POST_CODE', r.POST_CODE, 'DATE_OF_BIRTH', r.DATE_OF_BIRTH,
            'ACTIVE_MEMBER', r.ACTIVE_MEMBER, 'SOURCE_SYSTEM', r.SOURCE_SYSTEM)  AS RAW_PAYLOAD
    FROM RAW.MEMBER_FEED_RAW r
    WHERE r.RECORD_TYPE = 'D'
),
typed AS (
    SELECT
        c.*,
        -- TRY_TO_DATE returns NULL for an impossible date such as 20240230.
        CASE WHEN REGEXP_LIKE(ENROLLMENT_DATE_TXT, '[0-9]{8}')
             THEN TRY_TO_DATE(ENROLLMENT_DATE_TXT, 'YYYYMMDD') END    AS ENROLLMENT_DATE,
        CASE WHEN REGEXP_LIKE(LAST_FLIGHT_DATE_TXT, '[0-9]{8}')
             THEN TRY_TO_DATE(LAST_FLIGHT_DATE_TXT, 'YYYYMMDD') END   AS LAST_FLIGHT_DATE,
        CASE WHEN REGEXP_LIKE(DATE_OF_BIRTH_TXT, '[0-9]{8}')
             THEN TRY_TO_DATE(DATE_OF_BIRTH_TXT, 'MMDDYYYY') END      AS DATE_OF_BIRTH,
        -- POST_CODE stays text so leading zeros survive; anything but 1-5 digits becomes NULL + flag.
        CASE WHEN REGEXP_LIKE(POST_CODE_TXT, '[0-9]{1,5}') THEN POST_CODE_TXT END AS POST_CODE,
        m.COUNTRY_CODE
    FROM cleaned c
    LEFT JOIN REF.COUNTRY_CODE_MAP m
           ON m.RAW_COUNTRY_CODE = UPPER(c.COUNTRY) AND m.IS_ACTIVE
)
SELECT
    MEMBER_NAME, MEMBER_ID, ENROLLMENT_DATE, LAST_FLIGHT_DATE, TIER_CODE, AGENT_NAME,
    STATE, COUNTRY, POST_CODE, DATE_OF_BIRTH, ACTIVE_MEMBER,
    SOURCE_SYSTEM,
    SOURCE_SYSTEM || ':' || MEMBER_ID                         AS MEMBER_KEY,   -- NULL if either part is NULL
    COUNTRY_CODE,
    (DATE_OF_BIRTH_TXT IS NOT NULL AND DATE_OF_BIRTH IS NULL)           AS DQ_DOB_INVALID,
    (LAST_FLIGHT_DATE_TXT IS NOT NULL AND LAST_FLIGHT_DATE IS NULL)     AS DQ_LAST_FLIGHT_DATE_INVALID,
    (COUNTRY_CODE IS NULL)                                              AS DQ_COUNTRY_UNKNOWN,
    (POST_CODE_TXT IS NOT NULL AND POST_CODE IS NULL)                   AS DQ_POST_CODE_INVALID,
    (LENGTH(MEMBER_ID) < 6)                                             AS DQ_MEMBER_ID_SHORT,       -- warning only; 6 is an assumption
    (ACTIVE_MEMBER IS NOT NULL AND ACTIVE_MEMBER NOT IN ('A', 'I'))     AS DQ_ACTIVE_FLAG_UNKNOWN,   -- A/I values are an assumption
    CASE
        WHEN SOURCE_SYSTEM IS NULL          THEN 'MISSING_SOURCE_SYSTEM'
        WHEN MEMBER_ID IS NULL              THEN 'MISSING_MEMBER_ID'
        WHEN MEMBER_NAME IS NULL            THEN 'MISSING_MEMBER_NAME'
        WHEN FILE_TS IS NULL                THEN 'NO_FILE_TS'
        WHEN ENROLLMENT_DATE_TXT IS NULL    THEN 'MISSING_ENROLLMENT_DATE'
        WHEN ENROLLMENT_DATE IS NULL        THEN 'INVALID_ENROLLMENT_DATE'
        -- A value longer than the staging column would make the INSERT fail in Snowflake.
        WHEN LENGTH(MEMBER_NAME) > 255 OR LENGTH(MEMBER_ID) > 18 OR LENGTH(AGENT_NAME) > 255
          OR LENGTH(TIER_CODE) > 5 OR LENGTH(STATE) > 5 OR LENGTH(COUNTRY) > 5
          OR LENGTH(ACTIVE_MEMBER) > 1 OR LENGTH(SOURCE_SYSTEM) > 20
                                            THEN 'FIELD_TOO_LONG'
    END                                                                 AS REJECT_REASON,
    FILE_NAME, FILE_ROW_NUMBER, FILE_TS, BATCH_ID, RAW_PAYLOAD
FROM typed;
