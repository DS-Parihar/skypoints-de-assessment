-- =============================================================================
-- 05_validations.sql  (DuckDB)
-- Purpose : The data-quality rules DQ001..DQ017 as SQL. Each rule is a view DQ.V_DQ0nn that
--           returns its findings: the failing rows (or file, or batch) with a reason.
--           Same logic and names as sql/snowflake/05_validations.sql.
-- How     : A run first loads the work tables (DQ.RULE_*) and the config tables (DQ.CFG_*),
--           then reads DQ.V_FINDINGS. The rules only DECIDE; sql/duckdb/transform/2x_*.sql
--           APPLY the decisions (quarantine, staging, findings, summary).
--           skypoints.quality loads the tables: the runner from MEMBER_PREPARED_V, the unit
--           tests from in-memory PreparedRow objects, so both run exactly this SQL.
-- Config  : thresholds live in the DQ.CFG_* tables, filled from skypoints.quality.QualityConfig;
--           no threshold is written inside a rule.
-- Severity: REJECT (quarantine, not staged), WARN and ROUTE (recorded, row continues),
--           FAIL_FILE (that file stops), FAIL_BATCH (promotion to CORE is blocked).
-- Blank and NULL are the same: every text column goes through NULLIF(TRIM(col), '').
-- Dates are parsed here with the formats in DQ.CFG_DATE_FORMAT (per source system):
--           status NULL / OK / INVALID / AMBIGUOUS / NO_FORMAT, like skypoints.transform.derive.
-- =============================================================================

CREATE SCHEMA IF NOT EXISTS DQ;

-- ---------------------------------------------------------------------------
-- Work tables (replaced on every run)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS DQ.RULE_PARAM (
    BATCH_ID                   VARCHAR,
    AS_OF_DATE                 DATE    NOT NULL,   -- explicit run date, never CURRENT_DATE
    EXPECTED_MEMBER_ID_LENGTH  INTEGER NOT NULL,
    MAX_AGE                    INTEGER NOT NULL
);

CREATE TABLE IF NOT EXISTS DQ.RULE_INPUT (        -- one row per member row, as text
    BATCH_ID         VARCHAR,
    FILE_NAME        VARCHAR NOT NULL,
    FILE_ROW_NUMBER  BIGINT  NOT NULL,
    FILE_TS          TIMESTAMP,
    SOURCE_SYSTEM    VARCHAR,
    MEMBER_KEY       VARCHAR,
    MEMBER_NAME      VARCHAR,
    MEMBER_ID        VARCHAR,
    ENROLLMENT_DATE  VARCHAR,                      -- date columns stay text; the rules parse them
    LAST_FLIGHT_DATE VARCHAR,
    DATE_OF_BIRTH    VARCHAR,
    TIER_CODE        VARCHAR,
    AGENT_NAME       VARCHAR,
    STATE            VARCHAR,
    COUNTRY          VARCHAR,
    POST_CODE        VARCHAR,
    ACTIVE_MEMBER    VARCHAR,
    RAW_PAYLOAD      JSON                          -- the original raw row, for quarantine
);

CREATE TABLE IF NOT EXISTS DQ.RULE_HEADER (        -- one row per header column of a file
    BATCH_ID    VARCHAR,
    FILE_NAME   VARCHAR NOT NULL,
    COLUMN_NAME VARCHAR NOT NULL
);

CREATE TABLE IF NOT EXISTS DQ.RULE_COUNTS (
    BATCH_ID      VARCHAR,
    RAW_ROWS      BIGINT NOT NULL,
    STAGED_ROWS   BIGINT NOT NULL,
    REJECTED_ROWS BIGINT NOT NULL
);

CREATE TABLE IF NOT EXISTS DQ.RULE_COLUMN_COUNTS (
    BATCH_ID        VARCHAR,
    COLUMN_NAME     VARCHAR NOT NULL,
    RAW_NONBLANK    BIGINT NOT NULL,
    STAGED_NONBLANK BIGINT NOT NULL
);

-- Materialised once per run (by skypoints.quality.engine.materialize) so the decision views
-- and the apply scripts do not re-evaluate the rule views over and over.
CREATE TABLE IF NOT EXISTS DQ.WORK_FINDING (
    RULE_CODE VARCHAR, SEVERITY VARCHAR, REASON VARCHAR, BATCH_ID VARCHAR, FILE_NAME VARCHAR,
    FILE_ROW_NUMBER BIGINT, MEMBER_KEY VARCHAR, COLUMN_NAME VARCHAR, VALUE VARCHAR
);
CREATE TABLE IF NOT EXISTS DQ.WORK_DATE (
    BATCH_ID VARCHAR, FILE_NAME VARCHAR, FILE_ROW_NUMBER BIGINT, SOURCE_SYSTEM VARCHAR,
    MEMBER_KEY VARCHAR, COLUMN_NAME VARCHAR, TXT VARCHAR, FORMAT_NAME VARCHAR, STATUS VARCHAR,
    PARSED_DATE DATE
);

-- ---------------------------------------------------------------------------
-- Config tables (filled from QualityConfig, except the fixed header layout)
-- ---------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS DQ.CFG_TIER (TIER_CODE VARCHAR PRIMARY KEY);
CREATE TABLE IF NOT EXISTS DQ.CFG_ACTIVE (ACTIVE_VALUE VARCHAR PRIMARY KEY);
CREATE TABLE IF NOT EXISTS DQ.CFG_COUNTRY_MAP (ALIAS VARCHAR PRIMARY KEY, ISO3 VARCHAR NOT NULL);
CREATE TABLE IF NOT EXISTS DQ.CFG_MAX_LENGTH (COLUMN_NAME VARCHAR PRIMARY KEY, MAX_LENGTH INTEGER NOT NULL);
CREATE TABLE IF NOT EXISTS DQ.CFG_DATE_FORMAT (
    SOURCE_SYSTEM VARCHAR NOT NULL,
    COLUMN_NAME   VARCHAR NOT NULL,
    FORMAT_NAME   VARCHAR NOT NULL,   -- YYYYMMDD, MMDDYYYY, YYYY-MM-DD, M/D/YYYY or MDYYYY
    PRIMARY KEY (SOURCE_SYSTEM, COLUMN_NAME)
);
CREATE TABLE IF NOT EXISTS DQ.CFG_HEADER_COLUMN (  -- the member feed layout (spec, 11 columns)
    COLUMN_NAME  VARCHAR PRIMARY KEY,
    IS_MANDATORY BOOLEAN NOT NULL
);
INSERT INTO DQ.CFG_HEADER_COLUMN (COLUMN_NAME, IS_MANDATORY) VALUES
    ('Member_Name', TRUE), ('Member_Id', TRUE), ('Enrollment_Date', TRUE),
    ('Last_Flight_Date', FALSE), ('Tier_Code', FALSE), ('Agent_Name', FALSE), ('State', FALSE),
    ('Country', FALSE), ('Post_Code', FALSE), ('DOB', FALSE), ('Is_Active', FALSE)
ON CONFLICT DO NOTHING;

-- ---------------------------------------------------------------------------
-- Cleaned rows and parsed dates
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW DQ.V_RULE_ROW AS
SELECT BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, FILE_TS, RAW_PAYLOAD,
       NULLIF(TRIM(SOURCE_SYSTEM), '')    AS SOURCE_SYSTEM,
       NULLIF(TRIM(MEMBER_KEY), '')       AS MEMBER_KEY,
       NULLIF(TRIM(MEMBER_NAME), '')      AS MEMBER_NAME,
       NULLIF(TRIM(MEMBER_ID), '')        AS MEMBER_ID,
       NULLIF(TRIM(ENROLLMENT_DATE), '')  AS ENROLLMENT_DATE,
       NULLIF(TRIM(LAST_FLIGHT_DATE), '') AS LAST_FLIGHT_DATE,
       NULLIF(TRIM(DATE_OF_BIRTH), '')    AS DATE_OF_BIRTH,
       NULLIF(TRIM(TIER_CODE), '')        AS TIER_CODE,
       NULLIF(TRIM(AGENT_NAME), '')       AS AGENT_NAME,
       NULLIF(TRIM(STATE), '')            AS STATE,
       NULLIF(TRIM(COUNTRY), '')          AS COUNTRY,
       NULLIF(TRIM(POST_CODE), '')        AS POST_CODE,
       NULLIF(TRIM(ACTIVE_MEMBER), '')    AS ACTIVE_MEMBER
FROM DQ.RULE_INPUT;

-- One row per (row, date column) with the parse status. MDYYYY (no separators, unpadded month
-- and day) has two readings for 7-digit values: D1 = 1-digit month + 2-digit day, D2 = 2-digit
-- month + 1-digit day. Two valid readings = AMBIGUOUS.
CREATE OR REPLACE VIEW DQ.V_RULE_DATE AS
WITH unpivoted AS (
    SELECT BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, SOURCE_SYSTEM, MEMBER_KEY,
           'enrollment_date' AS COLUMN_NAME, ENROLLMENT_DATE AS TXT FROM DQ.V_RULE_ROW
    UNION ALL
    SELECT BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, SOURCE_SYSTEM, MEMBER_KEY,
           'last_flight_date', LAST_FLIGHT_DATE FROM DQ.V_RULE_ROW
    UNION ALL
    SELECT BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, SOURCE_SYSTEM, MEMBER_KEY,
           'date_of_birth', DATE_OF_BIRTH FROM DQ.V_RULE_ROW
),
readings AS (
    SELECT u.*, f.FORMAT_NAME,
        CASE f.FORMAT_NAME
            WHEN 'YYYYMMDD' THEN
                CASE WHEN regexp_matches(u.TXT, '^[0-9]{8}$') THEN try_strptime(u.TXT, '%Y%m%d') END
            WHEN 'MMDDYYYY' THEN
                CASE WHEN regexp_matches(u.TXT, '^[0-9]{8}$') THEN try_strptime(u.TXT, '%m%d%Y') END
            WHEN 'YYYY-MM-DD' THEN
                CASE WHEN regexp_matches(u.TXT, '^[0-9]{4}-[0-9]{2}-[0-9]{2}$')
                     THEN try_strptime(u.TXT, '%Y-%m-%d') END
            WHEN 'M/D/YYYY' THEN
                CASE WHEN regexp_matches(u.TXT, '^[0-9]{1,2}/[0-9]{1,2}/[0-9]{4}$')
                     THEN try_strptime(lpad(split_part(u.TXT, '/', 1), 2, '0')
                                       || lpad(split_part(u.TXT, '/', 2), 2, '0')
                                       || split_part(u.TXT, '/', 3), '%m%d%Y') END
            WHEN 'MDYYYY' THEN
                CASE WHEN regexp_matches(u.TXT, '^[0-9]{7,8}$') THEN
                    CASE WHEN length(u.TXT) = 8 THEN try_strptime(u.TXT, '%m%d%Y')
                         ELSE try_strptime('0' || substr(u.TXT, 1, 1) || substr(u.TXT, 2, 2)
                                           || right(u.TXT, 4), '%m%d%Y') END
                END
        END AS D1,
        CASE WHEN f.FORMAT_NAME = 'MDYYYY' AND regexp_matches(u.TXT, '^[0-9]{7}$')
             THEN try_strptime(substr(u.TXT, 1, 2) || '0' || substr(u.TXT, 3, 1) || right(u.TXT, 4),
                               '%m%d%Y') END AS D2
    FROM unpivoted u
    LEFT JOIN DQ.CFG_DATE_FORMAT f
           ON f.SOURCE_SYSTEM = u.SOURCE_SYSTEM AND f.COLUMN_NAME = u.COLUMN_NAME
),
statused AS (
    SELECT r.*,
        CASE WHEN TXT IS NULL OR upper(TXT) = 'NULL'    THEN 'NULL'
             WHEN FORMAT_NAME IS NULL                   THEN 'NO_FORMAT'
             WHEN D1 IS NOT NULL AND D2 IS NOT NULL     THEN 'AMBIGUOUS'
             WHEN D1 IS NOT NULL OR D2 IS NOT NULL      THEN 'OK'
             ELSE 'INVALID' END AS STATUS
    FROM readings r
)
SELECT BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, SOURCE_SYSTEM, MEMBER_KEY, COLUMN_NAME, TXT,
       FORMAT_NAME, STATUS,
       CASE WHEN STATUS = 'OK' THEN CAST(COALESCE(D1, D2) AS DATE) END AS PARSED_DATE
FROM statused;

-- ---------------------------------------------------------------------------
-- The rules. Every view returns the same nine columns.
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW DQ.V_DQ001
    (RULE_CODE, SEVERITY, REASON, BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, COLUMN_NAME, VALUE) AS
SELECT 'DQ001' AS RULE_CODE, 'REJECT' AS SEVERITY, 'member_name is blank or NULL' AS REASON,
       BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY,
       'member_name' AS COLUMN_NAME, CAST(NULL AS VARCHAR) AS VALUE
FROM DQ.V_RULE_ROW WHERE MEMBER_NAME IS NULL;

CREATE OR REPLACE VIEW DQ.V_DQ002
    (RULE_CODE, SEVERITY, REASON, BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, COLUMN_NAME, VALUE) AS
SELECT 'DQ002', 'REJECT', 'member_id is blank or NULL',
       BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, 'member_id', CAST(NULL AS VARCHAR)
FROM DQ.V_RULE_ROW WHERE MEMBER_ID IS NULL;

-- Anything but a valid date is unusable for a mandatory column, so AMBIGUOUS and NO_FORMAT count too.
CREATE OR REPLACE VIEW DQ.V_DQ003
    (RULE_CODE, SEVERITY, REASON, BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, COLUMN_NAME, VALUE) AS
SELECT 'DQ003', 'REJECT',
       'enrollment_date is ' || CASE STATUS WHEN 'NULL' THEN 'blank or NULL'
                                            WHEN 'AMBIGUOUS' THEN 'ambiguous'
                                            WHEN 'NO_FORMAT' THEN 'in an unknown date format'
                                            ELSE 'not a valid date' END,
       BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, 'enrollment_date', TXT
FROM DQ.V_RULE_DATE WHERE COLUMN_NAME = 'enrollment_date' AND STATUS <> 'OK';

-- Duplicate member_key within one file: every row except the last one (highest row number).
CREATE OR REPLACE VIEW DQ.V_DQ004
    (RULE_CODE, SEVERITY, REASON, BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, COLUMN_NAME, VALUE) AS
SELECT 'DQ004', 'REJECT',
       'member_key ' || MEMBER_KEY || ' appears again later in the same file (row '
           || CAST(LAST_ROW AS VARCHAR) || ' is kept)',
       BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, 'member_key', MEMBER_KEY
FROM (SELECT r.*, max(FILE_ROW_NUMBER) OVER (PARTITION BY BATCH_ID, FILE_NAME, MEMBER_KEY) AS LAST_ROW
      FROM DQ.V_RULE_ROW r WHERE MEMBER_KEY IS NOT NULL) x
WHERE FILE_ROW_NUMBER < LAST_ROW;

CREATE OR REPLACE VIEW DQ.V_DQ005
    (RULE_CODE, SEVERITY, REASON, BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, COLUMN_NAME, VALUE) AS
WITH t AS (
    SELECT BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, 'member_name' AS COLUMN_NAME, MEMBER_NAME AS TXT FROM DQ.V_RULE_ROW
    UNION ALL SELECT BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, 'member_id', MEMBER_ID FROM DQ.V_RULE_ROW
    UNION ALL SELECT BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, 'tier_code', TIER_CODE FROM DQ.V_RULE_ROW
    UNION ALL SELECT BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, 'agent_name', AGENT_NAME FROM DQ.V_RULE_ROW
    UNION ALL SELECT BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, 'state', STATE FROM DQ.V_RULE_ROW
    UNION ALL SELECT BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, 'country', COUNTRY FROM DQ.V_RULE_ROW
    UNION ALL SELECT BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, 'post_code', POST_CODE FROM DQ.V_RULE_ROW
    UNION ALL SELECT BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, 'active_member', ACTIVE_MEMBER FROM DQ.V_RULE_ROW
)
SELECT 'DQ005', 'REJECT',
       t.COLUMN_NAME || ' has ' || CAST(length(t.TXT) AS VARCHAR)
           || ' characters, over the spec limit of ' || CAST(c.MAX_LENGTH AS VARCHAR),
       t.BATCH_ID, t.FILE_NAME, t.FILE_ROW_NUMBER, t.MEMBER_KEY, t.COLUMN_NAME, t.TXT
FROM t JOIN DQ.CFG_MAX_LENGTH c ON c.COLUMN_NAME = t.COLUMN_NAME
WHERE length(t.TXT) > c.MAX_LENGTH;

CREATE OR REPLACE VIEW DQ.V_DQ006
    (RULE_CODE, SEVERITY, REASON, BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, COLUMN_NAME, VALUE) AS
SELECT 'DQ006', 'WARN', COLUMN_NAME || ' is not a valid date; the value becomes NULL',
       BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, COLUMN_NAME, TXT
FROM DQ.V_RULE_DATE
WHERE COLUMN_NAME IN ('last_flight_date', 'date_of_birth') AND STATUS = 'INVALID';

CREATE OR REPLACE VIEW DQ.V_DQ007
    (RULE_CODE, SEVERITY, REASON, BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, COLUMN_NAME, VALUE) AS
SELECT 'DQ007', 'WARN', COLUMN_NAME || ' is ambiguous (two valid readings); the value becomes NULL',
       BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, COLUMN_NAME, TXT
FROM DQ.V_RULE_DATE WHERE STATUS = 'AMBIGUOUS';

-- Flag only: the value is not changed.
CREATE OR REPLACE VIEW DQ.V_DQ008
    (RULE_CODE, SEVERITY, REASON, BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, COLUMN_NAME, VALUE) AS
WITH dob AS (
    SELECT d.*, p.AS_OF_DATE, p.MAX_AGE,
           year(p.AS_OF_DATE) - year(d.PARSED_DATE)
           - CASE WHEN month(p.AS_OF_DATE) * 100 + day(p.AS_OF_DATE)
                       < month(d.PARSED_DATE) * 100 + day(d.PARSED_DATE) THEN 1 ELSE 0 END AS AGE
    FROM DQ.V_RULE_DATE d CROSS JOIN DQ.RULE_PARAM p
    WHERE d.COLUMN_NAME = 'date_of_birth' AND d.STATUS = 'OK'
)
SELECT 'DQ008', 'WARN',
       CASE WHEN PARSED_DATE > AS_OF_DATE THEN 'date_of_birth is in the future'
            ELSE 'age ' || CAST(AGE AS VARCHAR) || ' is above the maximum of ' || CAST(MAX_AGE AS VARCHAR) END,
       BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, 'date_of_birth', TXT
FROM dob WHERE PARSED_DATE > AS_OF_DATE OR AGE > MAX_AGE;

CREATE OR REPLACE VIEW DQ.V_DQ009
    (RULE_CODE, SEVERITY, REASON, BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, COLUMN_NAME, VALUE) AS
SELECT 'DQ009', 'WARN',
       CASE WHEN f.PARSED_DATE > p.AS_OF_DATE THEN 'last_flight_date is after the as-of date'
            ELSE 'last_flight_date is before enrollment_date' END,
       f.BATCH_ID, f.FILE_NAME, f.FILE_ROW_NUMBER, f.MEMBER_KEY, 'last_flight_date', f.TXT
FROM DQ.V_RULE_DATE f
CROSS JOIN DQ.RULE_PARAM p
LEFT JOIN DQ.V_RULE_DATE e
       ON e.FILE_NAME = f.FILE_NAME AND e.FILE_ROW_NUMBER = f.FILE_ROW_NUMBER
      AND e.COLUMN_NAME = 'enrollment_date' AND e.STATUS = 'OK'
      AND e.BATCH_ID IS NOT DISTINCT FROM f.BATCH_ID
WHERE f.COLUMN_NAME = 'last_flight_date' AND f.STATUS = 'OK'
  AND (f.PARSED_DATE > p.AS_OF_DATE OR f.PARSED_DATE < e.PARSED_DATE);

CREATE OR REPLACE VIEW DQ.V_DQ010
    (RULE_CODE, SEVERITY, REASON, BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, COLUMN_NAME, VALUE) AS
SELECT 'DQ010', 'ROUTE', 'country is blank or not in the country map',
       BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, 'country', COUNTRY
FROM DQ.V_RULE_ROW r
WHERE COUNTRY IS NULL
   OR NOT EXISTS (SELECT 1 FROM DQ.CFG_COUNTRY_MAP m WHERE m.ALIAS = upper(r.COUNTRY));

CREATE OR REPLACE VIEW DQ.V_DQ011
    (RULE_CODE, SEVERITY, REASON, BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, COLUMN_NAME, VALUE) AS
SELECT 'DQ011', 'WARN', 'tier_code is not one of the allowed tiers',
       BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, 'tier_code', TIER_CODE
FROM DQ.V_RULE_ROW r
WHERE TIER_CODE IS NOT NULL AND NOT EXISTS (SELECT 1 FROM DQ.CFG_TIER t WHERE t.TIER_CODE = r.TIER_CODE);

CREATE OR REPLACE VIEW DQ.V_DQ012
    (RULE_CODE, SEVERITY, REASON, BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, COLUMN_NAME, VALUE) AS
SELECT 'DQ012', 'WARN', 'active_member is not one of the allowed values',
       BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, 'active_member', ACTIVE_MEMBER
FROM DQ.V_RULE_ROW r
WHERE ACTIVE_MEMBER IS NOT NULL
  AND NOT EXISTS (SELECT 1 FROM DQ.CFG_ACTIVE a WHERE a.ACTIVE_VALUE = r.ACTIVE_MEMBER);

CREATE OR REPLACE VIEW DQ.V_DQ013
    (RULE_CODE, SEVERITY, REASON, BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, COLUMN_NAME, VALUE) AS
SELECT 'DQ013', 'WARN',
       'member_id has ' || CAST(length(r.MEMBER_ID) AS VARCHAR) || ' characters, fewer than the expected '
           || CAST(p.EXPECTED_MEMBER_ID_LENGTH AS VARCHAR),
       r.BATCH_ID, r.FILE_NAME, r.FILE_ROW_NUMBER, r.MEMBER_KEY, 'member_id', r.MEMBER_ID
FROM DQ.V_RULE_ROW r CROSS JOIN DQ.RULE_PARAM p
WHERE r.MEMBER_ID IS NOT NULL AND length(r.MEMBER_ID) < p.EXPECTED_MEMBER_ID_LENGTH;

-- Header: missing mandatory, unknown or duplicate column fails the file; a missing optional
-- column (for example Post_Code) is only a warning. File level: no row number, no member_key.
CREATE OR REPLACE VIEW DQ.V_DQ014
    (RULE_CODE, SEVERITY, REASON, BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, COLUMN_NAME, VALUE) AS
WITH files AS (SELECT DISTINCT BATCH_ID, FILE_NAME FROM DQ.RULE_HEADER),
     cols  AS (SELECT BATCH_ID, FILE_NAME, COLUMN_NAME, count(*) AS N
               FROM DQ.RULE_HEADER GROUP BY BATCH_ID, FILE_NAME, COLUMN_NAME)
SELECT 'DQ014' AS RULE_CODE,
       CASE WHEN c.IS_MANDATORY THEN 'FAIL_FILE' ELSE 'WARN' END AS SEVERITY,
       CASE WHEN c.IS_MANDATORY THEN 'mandatory column ' || c.COLUMN_NAME || ' is missing from the header'
            ELSE 'optional column ' || c.COLUMN_NAME || ' is missing from the header' END AS REASON,
       f.BATCH_ID, f.FILE_NAME, CAST(NULL AS BIGINT) AS FILE_ROW_NUMBER, CAST(NULL AS VARCHAR) AS MEMBER_KEY,
       c.COLUMN_NAME, CAST(NULL AS VARCHAR) AS VALUE
FROM files f CROSS JOIN DQ.CFG_HEADER_COLUMN c
WHERE NOT EXISTS (SELECT 1 FROM DQ.RULE_HEADER h WHERE h.FILE_NAME = f.FILE_NAME AND h.COLUMN_NAME = c.COLUMN_NAME)
UNION ALL
SELECT 'DQ014', 'FAIL_FILE', 'column ' || COLUMN_NAME || ' is not part of the member feed layout',
       BATCH_ID, FILE_NAME, NULL, NULL, COLUMN_NAME, NULL
FROM cols WHERE NOT EXISTS (SELECT 1 FROM DQ.CFG_HEADER_COLUMN k WHERE k.COLUMN_NAME = cols.COLUMN_NAME)
UNION ALL
SELECT 'DQ014', 'FAIL_FILE', 'column ' || COLUMN_NAME || ' appears ' || CAST(N AS VARCHAR) || ' times in the header',
       BATCH_ID, FILE_NAME, NULL, NULL, COLUMN_NAME, NULL
FROM cols WHERE N > 1;

CREATE OR REPLACE VIEW DQ.V_DQ015
    (RULE_CODE, SEVERITY, REASON, BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, COLUMN_NAME, VALUE) AS
SELECT 'DQ015', 'FAIL_BATCH',
       'raw rows ' || CAST(RAW_ROWS AS VARCHAR) || ' differ from staged ' || CAST(STAGED_ROWS AS VARCHAR)
           || ' plus rejected ' || CAST(REJECTED_ROWS AS VARCHAR),
       BATCH_ID, CAST(NULL AS VARCHAR), CAST(NULL AS BIGINT), CAST(NULL AS VARCHAR),
       CAST(NULL AS VARCHAR), CAST(NULL AS VARCHAR)
FROM DQ.RULE_COUNTS WHERE RAW_ROWS <> STAGED_ROWS + REJECTED_ROWS;

-- Text columns only, zero tolerance.
CREATE OR REPLACE VIEW DQ.V_DQ016
    (RULE_CODE, SEVERITY, REASON, BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, COLUMN_NAME, VALUE) AS
SELECT 'DQ016', 'FAIL_BATCH',
       COLUMN_NAME || ': ' || CAST(RAW_NONBLANK AS VARCHAR) || ' non-blank values in raw, '
           || CAST(STAGED_NONBLANK AS VARCHAR) || ' in staging',
       BATCH_ID, CAST(NULL AS VARCHAR), CAST(NULL AS BIGINT), CAST(NULL AS VARCHAR),
       COLUMN_NAME, CAST(NULL AS VARCHAR)
FROM DQ.RULE_COLUMN_COUNTS
WHERE COLUMN_NAME IN ('member_name', 'member_id', 'tier_code', 'agent_name', 'state', 'country',
                      'active_member')
  AND RAW_NONBLANK > STAGED_NONBLANK;

-- Exclusive with DQ010: only a country that RESOLVES through an alias (PHIL -> PHL) is flagged.
CREATE OR REPLACE VIEW DQ.V_DQ017
    (RULE_CODE, SEVERITY, REASON, BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, COLUMN_NAME, VALUE) AS
SELECT 'DQ017', 'WARN', 'country ' || r.COUNTRY || ' was resolved through an alias to ' || m.ISO3,
       r.BATCH_ID, r.FILE_NAME, r.FILE_ROW_NUMBER, r.MEMBER_KEY, 'country', r.COUNTRY
FROM DQ.V_RULE_ROW r JOIN DQ.CFG_COUNTRY_MAP m ON m.ALIAS = upper(r.COUNTRY)
WHERE upper(r.COUNTRY) <> m.ISO3;

-- ---------------------------------------------------------------------------
-- All findings, and the decisions the apply scripts take from them
-- ---------------------------------------------------------------------------
CREATE OR REPLACE VIEW DQ.V_ROW_FINDINGS AS     -- DQ001..DQ014 and DQ017: no counts needed
SELECT * FROM DQ.V_DQ001 UNION ALL SELECT * FROM DQ.V_DQ002 UNION ALL SELECT * FROM DQ.V_DQ003
UNION ALL SELECT * FROM DQ.V_DQ004 UNION ALL SELECT * FROM DQ.V_DQ005 UNION ALL SELECT * FROM DQ.V_DQ006
UNION ALL SELECT * FROM DQ.V_DQ007 UNION ALL SELECT * FROM DQ.V_DQ008 UNION ALL SELECT * FROM DQ.V_DQ009
UNION ALL SELECT * FROM DQ.V_DQ010 UNION ALL SELECT * FROM DQ.V_DQ011 UNION ALL SELECT * FROM DQ.V_DQ012
UNION ALL SELECT * FROM DQ.V_DQ013 UNION ALL SELECT * FROM DQ.V_DQ014 UNION ALL SELECT * FROM DQ.V_DQ017;

CREATE OR REPLACE VIEW DQ.V_FINDINGS AS
SELECT * FROM DQ.V_ROW_FINDINGS
UNION ALL SELECT * FROM DQ.V_DQ015
UNION ALL SELECT * FROM DQ.V_DQ016;

-- All findings of a run: the materialised row-level ones plus the two reconciliation rules.
CREATE OR REPLACE VIEW DQ.V_RUN_FINDINGS AS
SELECT * FROM DQ.WORK_FINDING UNION ALL SELECT * FROM DQ.V_DQ015 UNION ALL SELECT * FROM DQ.V_DQ016;

-- Files with a FAIL_FILE finding: none of their rows are staged or quarantined.
CREATE OR REPLACE VIEW DQ.V_FAILED_FILE AS
SELECT DISTINCT FILE_NAME FROM DQ.WORK_FINDING WHERE SEVERITY = 'FAIL_FILE';

-- Rows with at least one REJECT finding (not in a failed file): one row each, all codes.
CREATE OR REPLACE VIEW DQ.V_ROW_REJECT AS
SELECT BATCH_ID, FILE_NAME, FILE_ROW_NUMBER,
       string_agg(DISTINCT RULE_CODE, ',' ORDER BY RULE_CODE) AS RULE_CODES,
       string_agg(REASON, '; ' ORDER BY RULE_CODE, COLUMN_NAME) AS REASONS
FROM DQ.WORK_FINDING f
WHERE SEVERITY = 'REJECT' AND NOT EXISTS (SELECT 1 FROM DQ.V_FAILED_FILE x WHERE x.FILE_NAME = f.FILE_NAME)
GROUP BY BATCH_ID, FILE_NAME, FILE_ROW_NUMBER;

CREATE OR REPLACE VIEW DQ.V_ACCEPTED_ROW AS
SELECT r.* FROM DQ.V_RULE_ROW r
WHERE NOT EXISTS (SELECT 1 FROM DQ.V_ROW_REJECT x
                  WHERE x.FILE_NAME = r.FILE_NAME AND x.FILE_ROW_NUMBER = r.FILE_ROW_NUMBER
                    AND x.BATCH_ID IS NOT DISTINCT FROM r.BATCH_ID)
  AND NOT EXISTS (SELECT 1 FROM DQ.V_FAILED_FILE x WHERE x.FILE_NAME = r.FILE_NAME);
