-- =============================================================================
-- 00_setup.sql  (Snowflake)
-- Purpose : Create the database, schemas, file formats and the external stage
--           that every later script depends on. Safe to re-run.
-- Layers  : RAW -> STAGING -> CORE -> MART, plus DQ (quarantine/checks) and REF
--           (reference/config tables). See docs/architecture.md.
-- Run as  : a role with CREATE DATABASE (e.g. SYSADMIN). The storage
--           integration step needs ACCOUNTADMIN and is commented out.
-- =============================================================================

CREATE DATABASE IF NOT EXISTS SKYPOINTS;
USE DATABASE SKYPOINTS;

CREATE SCHEMA IF NOT EXISTS RAW     COMMENT = 'Landing: every column text, append-only, plus load metadata';
CREATE SCHEMA IF NOT EXISTS STAGING COMMENT = 'Typed, derived Age/Stale_Member, DQ flags';
CREATE SCHEMA IF NOT EXISTS CORE    COMMENT = 'Conformed: one row per member (member_current), history';
CREATE SCHEMA IF NOT EXISTS MART    COMMENT = 'Per-country member tables + members_unassigned';
CREATE SCHEMA IF NOT EXISTS DQ      COMMENT = 'Quarantine table and data-quality checks';
-- Addition to the five schemas in the brief: REF holds the country map
-- (REF.COUNTRY_CODE_MAP). Config does not belong in RAW or CORE.
CREATE SCHEMA IF NOT EXISTS REF     COMMENT = 'Reference / configuration tables';

-- -----------------------------------------------------------------------------
-- File format: pipe-delimited member feed.
-- Records look like  |H|Member_Name|...   and   |D|Elena|223457|...
-- The line STARTS with a pipe, so field $1 is always empty, $2 is the record
-- type (H or D) and the data starts at $3. The COPY in 01_raw.sql handles that.
-- -----------------------------------------------------------------------------
CREATE FILE FORMAT IF NOT EXISTS RAW.MEMBER_FEED_FORMAT
    TYPE                           = CSV
    FIELD_DELIMITER                = '|'
    SKIP_HEADER                    = 0            -- the |H| header is a data row; kept in raw as record_type 'H'
    FIELD_OPTIONALLY_ENCLOSED_BY   = NONE
    TRIM_SPACE                     = FALSE        -- raw keeps the text exactly as received
    NULL_IF                        = ()           -- do NOT turn the text 'NULL' (seen in AUS DOB) into SQL NULL; staging decides
    EMPTY_FIELD_AS_NULL            = FALSE        -- blank stays '' in raw so blanks stay distinguishable from missing columns
    ERROR_ON_COLUMN_COUNT_MISMATCH = FALSE        -- sample has 10 fields, spec has 11: never reject at load, check the header in DQ
    ENCODING                       = 'UTF8';

-- File format: redemption JSON. The file is one array of member objects; each
-- member object becomes one row (payload VARIANT).
CREATE FILE FORMAT IF NOT EXISTS RAW.REDEMPTION_FEED_FORMAT
    TYPE               = JSON
    STRIP_OUTER_ARRAY  = TRUE
    ALLOW_DUPLICATE    = FALSE;

-- -----------------------------------------------------------------------------
-- External stage. Replace the URL and integration with your cloud bucket.
-- Expected layout:  members/members_<YYYYMMDDHH24MISS>.dat
--                   redemptions/redemptions_<YYYYMMDDHH24MISS>.json
-- The 14-digit timestamp in the file name becomes file_ts (ADR 0003).
-- -----------------------------------------------------------------------------
-- CREATE STORAGE INTEGRATION SKYPOINTS_S3_INT          -- needs ACCOUNTADMIN
--     TYPE = EXTERNAL_STAGE
--     STORAGE_PROVIDER = 'S3'
--     ENABLED = TRUE
--     STORAGE_AWS_ROLE_ARN = 'arn:aws:iam::<account-id>:role/<role>'
--     STORAGE_ALLOWED_LOCATIONS = ('s3://skypoints-landing/');

CREATE STAGE IF NOT EXISTS RAW.LANDING_STAGE
    URL       = 's3://skypoints-landing/'      -- TODO: real bucket
    -- STORAGE_INTEGRATION = SKYPOINTS_S3_INT  -- uncomment once the integration exists
    DIRECTORY = (ENABLE = TRUE)
    COMMENT   = 'External landing stage for member and redemption files';
