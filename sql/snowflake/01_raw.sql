-- =============================================================================
-- 01_raw.sql  (Snowflake)
-- Purpose : Landing tables and the COPY statements that fill them.
-- Rules   : Every business column is unlimited VARCHAR so nothing is truncated
--           or rejected at load; typing happens only in STAGING. Tables are
--           append-only: CREATE ... IF NOT EXISTS, never OR REPLACE.
-- member_key = source_system || ':' || member_id (ADR 0002). The .dat member feed is source
-- system 'SKYPOINTS'; the IND/USA/AUS extracts use their own code.
-- Metadata: file_name, file_row_number, file_ts, batch_id, loaded_at.
--           file_ts comes from the YYYYMMDD_HHMMSSTT timestamp in the FILE NAME, never
--           from now(); loaded_at is audit-only and never used in logic.
-- =============================================================================

USE DATABASE SKYPOINTS;

-- -----------------------------------------------------------------------------
-- RAW.MEMBER_FEED_RAW : one row per line of the pipe-delimited file
-- (header |H| rows included). Columns follow the SPEC order (11 fields).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS RAW.MEMBER_FEED_RAW (
    RECORD_TYPE      VARCHAR,          -- 'H' header, 'D' detail (no trailer in the spec)
    MEMBER_NAME      VARCHAR,
    MEMBER_ID        VARCHAR,
    ENROLLMENT_DATE  VARCHAR,
    LAST_FLIGHT_DATE VARCHAR,
    TIER_CODE        VARCHAR,
    AGENT_NAME       VARCHAR,
    STATE            VARCHAR,
    COUNTRY          VARCHAR,          -- named COUNTRY, not the 'County' typo of the existing ETL
    POST_CODE        VARCHAR,          -- position 9 of the 11-field spec layout
    DATE_OF_BIRTH    VARCHAR,          -- feed header calls it DOB
    ACTIVE_MEMBER    VARCHAR,          -- feed header calls it Is_Active
    SOURCE_SYSTEM    VARCHAR       NOT NULL,   -- 'SKYPOINTS' for the .dat feed; 'IND' / 'USA' / 'AUS' for the extracts
    MEMBER_KEY       VARCHAR,          -- source_system || ':' || member_id; NULL for header rows and blank IDs
    FILE_NAME        VARCHAR       NOT NULL,
    FILE_ROW_NUMBER  NUMBER(38,0)  NOT NULL,
    FILE_TS          TIMESTAMP_NTZ,    -- NULL if the name has no timestamp; such rows are quarantined in staging
    BATCH_ID         VARCHAR       NOT NULL,
    LOADED_AT        TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Raw member feed, all text, append-only';

-- -----------------------------------------------------------------------------
-- RAW.REDEMPTION_FEED_RAW : one row per member object in the JSON array.
-- The nested redemptions[] array is flattened later (STAGING).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS RAW.REDEMPTION_FEED_RAW (
    PAYLOAD          VARIANT       NOT NULL,
    FILE_NAME        VARCHAR       NOT NULL,
    FILE_ROW_NUMBER  NUMBER(38,0)  NOT NULL,   -- position of the member object in the array
    FILE_TS          TIMESTAMP_NTZ,
    BATCH_ID         VARCHAR       NOT NULL,
    LOADED_AT        TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
)
COMMENT = 'Raw redemption feed, one VARIANT per member object, append-only';

-- -----------------------------------------------------------------------------
-- Loaders. Wrapped in procedures so the run's batch_id can be passed in; a COPY
-- transformation cannot take a bind variable. COPY remembers loaded files for
-- 64 days, so re-running does not load the same file twice.
--
-- Token positions in a member line (the leading pipe makes $1 empty):
--   $1 ''  $2 record_type  $3 Member_Name  $4 Member_Id  $5 Enrollment_Date
--   $6 Last_Flight_Date  $7 Tier_Code  $8 Agent_Name  $9 State  $10 Country
--   $11 Post_Code  $12 Date of Birth (DOB)  $13 Active Member (Is_Active)
-- 11-field SPEC layout, read by position. A header that does not match is caught by
-- DQ.MEMBER_HEADER_CHECK in 02_staging.sql.
-- NOTE: the brief's 10-field SAMPLE header (no Post_Code) is NOT loaded by this SQL. It is
-- handled by the Python parser (skypoints.ingest.flat_file), which maps columns by header
-- NAME, fills the missing optional column with '' and returns a warning. This SQL assumes
-- the 11-field SPEC layout, by position.
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE RAW.LOAD_MEMBER_FEED(BATCH_ID VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
AS
$$
BEGIN
    EXECUTE IMMEDIATE
    'COPY INTO RAW.MEMBER_FEED_RAW
       (RECORD_TYPE, MEMBER_NAME, MEMBER_ID, ENROLLMENT_DATE, LAST_FLIGHT_DATE, TIER_CODE,
        AGENT_NAME, STATE, COUNTRY, POST_CODE, DATE_OF_BIRTH, ACTIVE_MEMBER, SOURCE_SYSTEM, MEMBER_KEY,
        FILE_NAME, FILE_ROW_NUMBER, FILE_TS, BATCH_ID, LOADED_AT)
     FROM (
       SELECT $2, $3, $4, $5, $6, $7, $8, $9, $10,
              $11, $12, $13,
              ''SKYPOINTS'',
              CASE WHEN $2 = ''D'' AND NULLIF(TRIM($4), '''') IS NOT NULL
                   THEN ''SKYPOINTS:'' || TRIM($4) END,
              METADATA$FILENAME,
              METADATA$FILE_ROW_NUMBER,
              TRY_TO_TIMESTAMP_NTZ(REGEXP_SUBSTR(METADATA$FILENAME, ''[0-9]{8}_[0-9]{8}''), ''YYYYMMDD_HH24MISSFF2''),
              ''' || :BATCH_ID || ''',
              CURRENT_TIMESTAMP()
       FROM @RAW.LANDING_STAGE/members/
     )
     FILE_FORMAT = (FORMAT_NAME = ''RAW.MEMBER_FEED_FORMAT'')
     PATTERN     = ''.*[.]dat''
     ON_ERROR    = ''CONTINUE''';
    -- ON_ERROR CONTINUE never aborts the load; file-level rejects are listed by COPY_HISTORY / VALIDATE.
    RETURN 'member feed loaded, batch ' || :BATCH_ID;
END;
$$;

CREATE OR REPLACE PROCEDURE RAW.LOAD_REDEMPTION_FEED(BATCH_ID VARCHAR)
RETURNS VARCHAR
LANGUAGE SQL
AS
$$
BEGIN
    EXECUTE IMMEDIATE
    'COPY INTO RAW.REDEMPTION_FEED_RAW
       (PAYLOAD, FILE_NAME, FILE_ROW_NUMBER, FILE_TS, BATCH_ID, LOADED_AT)
     FROM (
       SELECT $1,
              METADATA$FILENAME,
              METADATA$FILE_ROW_NUMBER,
              TRY_TO_TIMESTAMP_NTZ(REGEXP_SUBSTR(METADATA$FILENAME, ''[0-9]{8}_[0-9]{8}''), ''YYYYMMDD_HH24MISSFF2''),
              ''' || :BATCH_ID || ''',
              CURRENT_TIMESTAMP()
       FROM @RAW.LANDING_STAGE/redemptions/
     )
     FILE_FORMAT = (FORMAT_NAME = ''RAW.REDEMPTION_FEED_FORMAT'')
     PATTERN     = ''.*[.]json''
     ON_ERROR    = ''CONTINUE''';
    RETURN 'redemption feed loaded, batch ' || :BATCH_ID;
END;
$$;

-- Usage (the orchestrator creates one batch_id per run):
-- CALL RAW.LOAD_MEMBER_FEED(UUID_STRING());
-- CALL RAW.LOAD_REDEMPTION_FEED(UUID_STRING());

-- -----------------------------------------------------------------------------
-- Snowpipe (continuous load) - COMMENTED OUT: needs the cloud event
-- notification wired to the pipe (SHOW PIPES -> notification_channel).
-- A pipe has no run-level batch_id, so the FILE NAME is used as the batch id
-- (one file = one batch).
-- -----------------------------------------------------------------------------
-- CREATE PIPE IF NOT EXISTS RAW.MEMBER_FEED_PIPE AUTO_INGEST = TRUE AS
--   COPY INTO RAW.MEMBER_FEED_RAW
--     (RECORD_TYPE, MEMBER_NAME, MEMBER_ID, ENROLLMENT_DATE, LAST_FLIGHT_DATE, TIER_CODE,
--      AGENT_NAME, STATE, COUNTRY, POST_CODE, DATE_OF_BIRTH, ACTIVE_MEMBER, SOURCE_SYSTEM, MEMBER_KEY,
--      FILE_NAME, FILE_ROW_NUMBER, FILE_TS, BATCH_ID, LOADED_AT)
--   FROM (
--     SELECT $2,$3,$4,$5,$6,$7,$8,$9,$10, $11,$12,$13,
--            'SKYPOINTS', CASE WHEN $2='D' AND NULLIF(TRIM($4),'') IS NOT NULL THEN 'SKYPOINTS:'||TRIM($4) END,
--            METADATA$FILENAME, METADATA$FILE_ROW_NUMBER,
--            TRY_TO_TIMESTAMP_NTZ(REGEXP_SUBSTR(METADATA$FILENAME,'[0-9]{8}_[0-9]{8}'),'YYYYMMDD_HH24MISSFF2'),
--            METADATA$FILENAME, CURRENT_TIMESTAMP()
--     FROM @RAW.LANDING_STAGE/members/
--   )
--   FILE_FORMAT = (FORMAT_NAME = 'RAW.MEMBER_FEED_FORMAT')
--   PATTERN = '.*[.]dat';
--
-- CREATE PIPE IF NOT EXISTS RAW.REDEMPTION_FEED_PIPE AUTO_INGEST = TRUE AS
--   COPY INTO RAW.REDEMPTION_FEED_RAW (PAYLOAD, FILE_NAME, FILE_ROW_NUMBER, FILE_TS, BATCH_ID, LOADED_AT)
--   FROM (
--     SELECT $1, METADATA$FILENAME, METADATA$FILE_ROW_NUMBER,
--            TRY_TO_TIMESTAMP_NTZ(REGEXP_SUBSTR(METADATA$FILENAME,'[0-9]{8}_[0-9]{8}'),'YYYYMMDD_HH24MISSFF2'),
--            METADATA$FILENAME, CURRENT_TIMESTAMP()
--     FROM @RAW.LANDING_STAGE/redemptions/
--   )
--   FILE_FORMAT = (FORMAT_NAME = 'RAW.REDEMPTION_FEED_FORMAT')
--   PATTERN = '.*[.]json';
