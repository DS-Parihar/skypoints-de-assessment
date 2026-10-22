-- =============================================================================
-- 02_staging.sql  (Snowflake)
-- Purpose : Typed staging table for members, the quarantine table, and the
--           header check. This script only creates structures; the SQL that
--           fills them comes later.
-- Typing  : Spec types and lengths, with every deviation marked DEVIATION.
-- Rules   : Mandatory columns are NOT NULL here and nowhere else. A raw row that
--           breaks one goes to DQ.QUARANTINE with a reason code instead of
--           failing the load. Other faults (bad DOB, unknown country...) set a
--           DQ_ flag and the row stays in staging. Age and Stale_Member come
--           from AS_OF_DATE, an explicit run parameter, never from today's date.
-- Note    : Snowflake CHAR(n) behaves as VARCHAR(n) and does not space-pad, but
--           the spec types are kept so the DDL ports to engines that do pad.
-- =============================================================================

USE DATABASE SKYPOINTS;

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

    -- ---- derived ----
    COUNTRY_CODE     CHAR(3),                  -- ISO-3 via REF.COUNTRY_CODE_MAP; NULL if unmapped -> members_unassigned
    AS_OF_DATE       DATE          NOT NULL,   -- explicit run date for Age / Stale_Member
    AGE              NUMBER(3,0),              -- whole years at AS_OF_DATE; NULL when DATE_OF_BIRTH is NULL
    STALE_MEMBER     BOOLEAN       NOT NULL,   -- no flight in the 90 days before AS_OF_DATE; no flight date counts as stale (to confirm)

    -- ---- data-quality flags (TRUE = problem) ----
    DQ_DOB_INVALID               BOOLEAN NOT NULL DEFAULT FALSE,  -- present but unreadable or ambiguous
    DQ_LAST_FLIGHT_DATE_INVALID  BOOLEAN NOT NULL DEFAULT FALSE,
    DQ_COUNTRY_UNKNOWN           BOOLEAN NOT NULL DEFAULT FALSE,  -- blank or not in the country map
    DQ_POST_CODE_INVALID         BOOLEAN NOT NULL DEFAULT FALSE,  -- not 1-5 digits
    DQ_MEMBER_ID_SHORT           BOOLEAN NOT NULL DEFAULT FALSE,  -- warning only (spec allows variable length)
    DQ_ACTIVE_FLAG_UNKNOWN       BOOLEAN NOT NULL DEFAULT FALSE,

    -- ---- lineage (copied from RAW) ----
    FILE_NAME        VARCHAR       NOT NULL,
    FILE_ROW_NUMBER  NUMBER(38,0)  NOT NULL,
    FILE_TS          TIMESTAMP_NTZ NOT NULL,   -- rows with a NULL file_ts are quarantined (NO_FILE_TS)
    BATCH_ID         VARCHAR       NOT NULL,
    STAGED_AT        TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),   -- audit only

    -- Informational in Snowflake (not enforced); documents the grain.
    CONSTRAINT PK_MEMBER_STG PRIMARY KEY (FILE_NAME, FILE_ROW_NUMBER)
)
-- No CLUSTER BY: rows arrive in load order, so micro-partitions are already ordered by
-- FILE_TS / BATCH_ID, which is how each incremental run filters. Add a key only if
-- SYSTEM$CLUSTERING_INFORMATION shows poor pruning.
COMMENT = 'Typed member rows with derived Age/Stale_Member and DQ flags';

-- -----------------------------------------------------------------------------
-- DQ.QUARANTINE : rows that cannot be a member. Nothing is silently dropped.
-- Member reason codes: MISSING_MEMBER_ID, MISSING_MEMBER_NAME,
-- MISSING_ENROLLMENT_DATE, INVALID_ENROLLMENT_DATE, NO_FILE_TS, HEADER_MISMATCH.
-- Redemptions add INVALID_JSON, UNMATCHED_MEMBER, DUPLICATE_TXN_OLDER.
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS DQ.QUARANTINE (
    QUARANTINE_ID    NUMBER(38,0)  NOT NULL AUTOINCREMENT,
    SOURCE_TABLE     VARCHAR       NOT NULL,   -- e.g. RAW.MEMBER_FEED_RAW
    FILE_NAME        VARCHAR,
    FILE_ROW_NUMBER  NUMBER(38,0),
    BATCH_ID         VARCHAR,
    REASON_CODE      VARCHAR       NOT NULL,
    REASON_DETAIL    VARCHAR,
    RAW_PAYLOAD      VARIANT,                  -- the row exactly as received
    QUARANTINED_AT   TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),   -- audit only
    CONSTRAINT PK_QUARANTINE PRIMARY KEY (QUARANTINE_ID)
)
COMMENT = 'Rejected rows with a reason code';

-- -----------------------------------------------------------------------------
-- DQ.MEMBER_HEADER_CHECK : compares each |H| row with the header of the sample
-- file. The spec lists 11 fields; the sample header has 10 (no Post_Code).
-- Loading never fails on this, so a mismatch is reported here and the batch is
-- stopped (HEADER_MISMATCH) before staging.
-- -----------------------------------------------------------------------------
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
        AND DATE_OF_BIRTH    = 'DOB'
        AND ACTIVE_MEMBER    = 'Is_Active') AS HEADER_OK
FROM RAW.MEMBER_FEED_RAW
WHERE RECORD_TYPE = 'H';
