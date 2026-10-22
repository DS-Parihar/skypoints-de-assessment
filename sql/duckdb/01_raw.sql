-- =============================================================================
-- 01_raw.sql  (DuckDB)
-- Purpose : Landing tables, same names and columns as sql/snowflake/01_raw.sql.
-- Rules   : Every business column is unlimited VARCHAR so nothing is truncated
--           or rejected at load; typing happens only in STAGING. Append-only.
-- Metadata: file_name, file_row_number, file_ts, batch_id, loaded_at.
--           file_ts comes from the 14-digit timestamp in the FILE NAME, never
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
    POST_CODE        VARCHAR,          -- NULL for the 10-field sample layout
    DATE_OF_BIRTH    VARCHAR,          -- feed header calls it DOB
    ACTIVE_MEMBER    VARCHAR,          -- feed header calls it Is_Active
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
--   SAMPLE layout (10 fields): c11 DOB       c12 Is_Active
--   SPEC layout   (11 fields): c11 Post_Code c12 Date of Birth  c13 Active Member
-- DEVIATION from the spec: the sample has no Post_Code, so POST_CODE is NULL and
-- DOB is read from c11. For the SPEC layout use "c11, c12, c13" in place of
-- "NULL, c11, c12". null_padding fills short lines; quote='' keeps raw text; auto_detect = false skips the sniffer, which fails on ragged lines.
-- file_row_number is the line position in the file (the preserved read order).
--
-- INSERT INTO RAW.MEMBER_FEED_RAW
-- SELECT c02, c03, c04, c05, c06, c07, c08, c09, c10,
--        NULL, c11, c12,
--        parse_filename(filename),
--        row_number() OVER (),
--        try_strptime(regexp_extract(filename, '[0-9]{14}'), '%Y%m%d%H%M%S'),
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
--        try_strptime(regexp_extract(filename, '[0-9]{14}'), '%Y%m%d%H%M%S'),
--        $batch_id, current_timestamp
-- FROM read_json_objects($file, format = 'array', filename = true);
-- -----------------------------------------------------------------------------
