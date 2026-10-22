-- =============================================================================
-- 01_raw.sql  (DuckDB)
-- Purpose : Landing tables, same names and columns as sql/snowflake/01_raw.sql.
-- member_key = source_system || ':' || member_id (ADR 0002). The .dat member feed is source
-- system 'SKYPOINTS'; the IND/USA/AUS extracts use their own code.
-- Rules   : Every business column is unlimited VARCHAR so nothing is truncated
--           or rejected at load; typing happens only in STAGING. Append-only.
-- Metadata: file_name, file_row_number, file_ts, batch_id, loaded_at.
--           file_ts comes from the YYYYMMDD_HHMMSSTT timestamp in the FILE NAME, never
--           from now(); loaded_at is audit-only and never used in logic.
-- Differences from Snowflake: no COPY INTO / METADATA$ columns / Snowpipe. The
-- Python loader (src/skypoints) runs the INSERT below with parameters.
-- =============================================================================

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
    SOURCE_SYSTEM    VARCHAR   NOT NULL,   -- 'SKYPOINTS' for the .dat feed; 'IND' / 'USA' / 'AUS' for the extracts
    MEMBER_KEY       VARCHAR,          -- source_system || ':' || member_id; NULL for header rows and blank IDs
    FILE_NAME        VARCHAR   NOT NULL,
    FILE_ROW_NUMBER  BIGINT    NOT NULL,
    FILE_TS          TIMESTAMP,        -- NULL if the name has no timestamp; such rows are quarantined in staging
    BATCH_ID         VARCHAR   NOT NULL,
    LOADED_AT        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

CREATE TABLE IF NOT EXISTS RAW.REDEMPTION_FEED_RAW (
    PAYLOAD          JSON      NOT NULL,   -- one member object; Snowflake uses VARIANT
    FILE_NAME        VARCHAR   NOT NULL,
    FILE_ROW_NUMBER  BIGINT    NOT NULL,   -- position of the member object in the array
    FILE_TS          TIMESTAMP,
    BATCH_ID         VARCHAR   NOT NULL,
    LOADED_AT        TIMESTAMP NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- -----------------------------------------------------------------------------
-- Loading a member file (run by the Python loader; $file and $batch_id are
-- parameters). The line starts with a pipe, so token c01 is always empty:
--   c01 ''  c02 record_type  c03 Member_Name  c04 Member_Id  c05 Enrollment_Date
--   c06 Last_Flight_Date  c07 Tier_Code  c08 Agent_Name  c09 State  c10 Country
--   c11 Post_Code  c12 Date of Birth (DOB)  c13 Active Member (Is_Active)
-- NOTE: the brief's 10-field SAMPLE header (no Post_Code) is handled by the Python parser
-- (skypoints.ingest.flat_file), which maps columns by header NAME, fills the missing optional
-- column with '' and returns a warning. The SQL below assumes the 11-field SPEC layout.
-- null_padding fills short lines; quote='' keeps raw text; auto_detect = false skips the sniffer, which fails on ragged lines.
-- file_row_number is the line position in the file (the preserved read order).
--
-- INSERT INTO RAW.MEMBER_FEED_RAW
-- SELECT c02, c03, c04, c05, c06, c07, c08, c09, c10,
--        c11, c12, c13,
--        'SKYPOINTS',
--        CASE WHEN c02 = 'D' AND NULLIF(trim(c04), '') IS NOT NULL THEN 'SKYPOINTS:' || trim(c04) END,
--        parse_filename(filename),
--        row_number() OVER (),
--        try_strptime(left(regexp_extract(filename, '[0-9]{8}_[0-9]{8}'), 15), '%Y%m%d_%H%M%S')
--          + to_microseconds(try_cast(right(regexp_extract(filename, '[0-9]{8}_[0-9]{8}'), 2) AS INTEGER) * 10000),
--        $batch_id,
--        current_timestamp
-- FROM read_csv($file, delim = '|', header = false, quote = '', all_varchar = true,
--               null_padding = true, filename = true, auto_detect = false,
--               columns = {'c01':'VARCHAR','c02':'VARCHAR','c03':'VARCHAR','c04':'VARCHAR',
--                          'c05':'VARCHAR','c06':'VARCHAR','c07':'VARCHAR','c08':'VARCHAR',
--                          'c09':'VARCHAR','c10':'VARCHAR','c11':'VARCHAR','c12':'VARCHAR',
--                          'c13':'VARCHAR'});
--
-- Loading a redemption file: each member object of the array becomes one row.
--
-- INSERT INTO RAW.REDEMPTION_FEED_RAW
-- SELECT json, parse_filename(filename), row_number() OVER (),
--        try_strptime(left(regexp_extract(filename, '[0-9]{8}_[0-9]{8}'), 15), '%Y%m%d_%H%M%S')
--          + to_microseconds(try_cast(right(regexp_extract(filename, '[0-9]{8}_[0-9]{8}'), 2) AS INTEGER) * 10000),
--        $batch_id, current_timestamp
-- FROM read_json_objects($file, format = 'array', filename = true);
-- -----------------------------------------------------------------------------
