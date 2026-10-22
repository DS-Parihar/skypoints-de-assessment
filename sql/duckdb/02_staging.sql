-- =============================================================================
-- 02_staging.sql  (DuckDB)
-- Purpose : Typed staging table for members, the quarantine table, and the
--           header check. Same names and columns as sql/snowflake/02_staging.sql.
-- member_key = source_system || ':' || member_id (ADR 0002). The .dat member feed is source
-- system 'SKYPOINTS'; the IND/USA/AUS extracts use their own code.
-- Rules   : Mandatory columns are NOT NULL here and nowhere else. A raw row that
--           breaks one goes to DQ.QUARANTINE with a reason code instead of
--           failing the load. Other faults set a DQ_ flag and the row stays.
--           Age and Stale_Member come from AS_OF_DATE, an explicit parameter.
-- Differences from Snowflake:
--   * DuckDB does not enforce VARCHAR(n) / CHAR(n) lengths; they are kept so the
--     tables document the spec and match Snowflake. Length checks belong in the
--     staging transform.
--   * NUMBER(3,0) -> DECIMAL(3,0), TIMESTAMP_NTZ -> TIMESTAMP, VARIANT -> JSON.
--   * No AUTOINCREMENT: a sequence supplies QUARANTINE_ID. No COMMENT clause, no CLUSTER BY.
-- =============================================================================

CREATE TABLE IF NOT EXISTS STAGING.MEMBER_STG (
    -- ---- spec columns (only record_type 'D' rows get here) ----
    MEMBER_NAME      VARCHAR(255)  NOT NULL,   -- spec: CHAR(255) key. DEVIATION: VARCHAR, CHAR pads with spaces. Not a unique key (names repeat), see ADR 0002.
    MEMBER_ID        VARCHAR(18)   NOT NULL,   -- business key (ADR 0002); text so leading zeros survive
    ENROLLMENT_DATE  DATE          NOT NULL,   -- mandatory; missing or unreadable -> row quarantined
    LAST_FLIGHT_DATE DATE,                     -- optional; unreadable -> NULL + DQ_LAST_FLIGHT_DATE_INVALID
    TIER_CODE        CHAR(5),
    AGENT_NAME       VARCHAR(255),             -- DEVIATION: spec says CHAR(255); VARCHAR avoids padding
    STATE            CHAR(5),
    COUNTRY          CHAR(5),                  -- as received, trimmed; named COUNTRY, not the 'County' typo of the old staging
    POST_CODE        VARCHAR(5),               -- DEVIATION: spec says INT; INT drops leading zeros (as happened to DOB)
    DATE_OF_BIRTH    DATE,                     -- optional; assumed MMDDYYYY (ADR 0005); unreadable -> NULL + flag
    ACTIVE_MEMBER    CHAR(1),                  -- spec 'Active Member'; the feed header calls it Is_Active

    -- ---- key (ADR 0002) ----
    SOURCE_SYSTEM    VARCHAR(20)   NOT NULL,   -- 'SKYPOINTS' for the .dat feed; 'IND' / 'USA' / 'AUS' for the extracts
    MEMBER_KEY       VARCHAR(40)   NOT NULL,   -- source_system || ':' || member_id; member_id alone is NOT unique

    -- ---- derived ----
    COUNTRY_CODE     CHAR(3),                  -- ISO-3 via REF.COUNTRY_CODE_MAP; NULL if unmapped -> members_unassigned
    AS_OF_DATE       DATE          NOT NULL,   -- explicit run date for Age / Stale_Member
    AGE              DECIMAL(3,0),             -- whole years at AS_OF_DATE; NULL when DATE_OF_BIRTH is NULL
    STALE_MEMBER     BOOLEAN,                  -- TRUE: last flight more than 90 days before AS_OF_DATE; NULL: unknown (no flight date, or one after AS_OF_DATE)

    -- ---- data-quality flags (TRUE = problem) ----
    DQ_DOB_INVALID               BOOLEAN NOT NULL DEFAULT FALSE,
    DQ_LAST_FLIGHT_DATE_INVALID  BOOLEAN NOT NULL DEFAULT FALSE,
    DQ_COUNTRY_UNKNOWN           BOOLEAN NOT NULL DEFAULT FALSE,
    DQ_POST_CODE_INVALID         BOOLEAN NOT NULL DEFAULT FALSE,
    DQ_MEMBER_ID_SHORT           BOOLEAN NOT NULL DEFAULT FALSE,
    DQ_ACTIVE_FLAG_UNKNOWN       BOOLEAN NOT NULL DEFAULT FALSE,

    -- ---- lineage (copied from RAW) ----
    FILE_NAME        VARCHAR       NOT NULL,
    FILE_ROW_NUMBER  BIGINT        NOT NULL,
    FILE_TS          TIMESTAMP     NOT NULL,   -- rows with a NULL file_ts are quarantined (NO_FILE_TS)
    BATCH_ID         VARCHAR       NOT NULL,
    STAGED_AT        TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,   -- audit only

    PRIMARY KEY (FILE_NAME, FILE_ROW_NUMBER)   -- enforced in DuckDB (informational in Snowflake)
);

-- Member reason codes: MISSING_MEMBER_ID, MISSING_MEMBER_NAME, MISSING_ENROLLMENT_DATE,
-- INVALID_ENROLLMENT_DATE, NO_FILE_TS, HEADER_MISMATCH.
-- Redemptions add INVALID_JSON, UNMATCHED_MEMBER, DUPLICATE_TXN_OLDER.
CREATE SEQUENCE IF NOT EXISTS DQ.QUARANTINE_SEQ;

CREATE TABLE IF NOT EXISTS DQ.QUARANTINE (
    QUARANTINE_ID    BIGINT        NOT NULL DEFAULT nextval('DQ.QUARANTINE_SEQ'),
    SOURCE_TABLE     VARCHAR       NOT NULL,   -- e.g. RAW.MEMBER_FEED_RAW
    FILE_NAME        VARCHAR,
    FILE_ROW_NUMBER  BIGINT,
    BATCH_ID         VARCHAR,
    REASON_CODE      VARCHAR       NOT NULL,
    REASON_DETAIL    VARCHAR,
    RULE_CODE        VARCHAR,                  -- every rule code that rejected the row, comma-separated, e.g. 'DQ001,DQ002'
    SEVERITY         VARCHAR,                  -- 'REJECT' for quarantined rows
    RAW_PAYLOAD      JSON,                     -- the row exactly as received
    QUARANTINED_AT   TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,   -- audit only
    PRIMARY KEY (QUARANTINE_ID)
);

-- Rows that continue (WARN, ROUTE) and file/batch level findings. member_key is NULL for
-- file- and batch-level findings.
CREATE SEQUENCE IF NOT EXISTS DQ.ROW_FINDING_SEQ;

CREATE TABLE IF NOT EXISTS DQ.ROW_FINDING (
    FINDING_ID       BIGINT        NOT NULL DEFAULT nextval('DQ.ROW_FINDING_SEQ'),
    BATCH_ID         VARCHAR       NOT NULL,
    FILE_NAME        VARCHAR,
    FILE_ROW_NUMBER  BIGINT,
    MEMBER_KEY       VARCHAR,
    RULE_CODE        VARCHAR       NOT NULL,
    SEVERITY         VARCHAR       NOT NULL,
    COLUMN_NAME      VARCHAR,
    VALUE            VARCHAR,                  -- the offending value as received
    REASON           VARCHAR       NOT NULL,
    CREATED_AT       TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,   -- audit only
    PRIMARY KEY (FINDING_ID)
);

-- One row per rule that fired in a batch.
CREATE TABLE IF NOT EXISTS DQ.RUN_SUMMARY (
    BATCH_ID         VARCHAR       NOT NULL,
    RULE_CODE        VARCHAR       NOT NULL,
    FAILED_COUNT     BIGINT        NOT NULL,
    PRIMARY KEY (BATCH_ID, RULE_CODE)
);

-- Compares each |H| row loaded by 01_raw.sql with the 11-field spec header, by position.
-- The brief's 10-field sample header (no Post_Code) is handled by the Python parser,
-- which maps columns by name, so it never reaches this check. Loading never fails on this, so
-- a mismatch is reported here and the batch is stopped (HEADER_MISMATCH).
CREATE OR REPLACE VIEW DQ.MEMBER_HEADER_CHECK AS
SELECT FILE_NAME,
       BATCH_ID,
       (    MEMBER_NAME      = 'Member_Name'
        AND MEMBER_ID        = 'Member_Id'
        AND ENROLLMENT_DATE  = 'Enrollment_Date'
        AND LAST_FLIGHT_DATE = 'Last_Flight_Date'
        AND TIER_CODE        = 'Tier_Code'
        AND AGENT_NAME       = 'Agent_Name'
        AND STATE            = 'State'
        AND COUNTRY          = 'Country'
        AND POST_CODE        = 'Post_Code'
        AND DATE_OF_BIRTH    = 'DOB'
        AND ACTIVE_MEMBER    = 'Is_Active') AS HEADER_OK
FROM RAW.MEMBER_FEED_RAW
WHERE RECORD_TYPE = 'H';
