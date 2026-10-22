-- =============================================================================
-- 03_targets.sql  (DuckDB)
-- Purpose : Country map (config), CORE.MEMBER_CURRENT, the move history table,
--           and one MART.MEMBERS_<ISO3> table per country plus
--           MART.MEMBERS_UNASSIGNED. Same names and columns as Snowflake.
-- Design  : Country tables are DERIVED from CORE.MEMBER_CURRENT (ADR 0004).
-- Differences from Snowflake:
--   * No stored procedures and no dynamic DDL in DuckDB SQL. The country tables
--     are written out below for the countries in the seed map; the Python
--     loader generates a CREATE TABLE for any country added to
--     REF.COUNTRY_CODE_MAP.
--   * No CLUSTER BY (DuckDB has no clustering; zonemaps cover small data).
--   * No CREATE TABLE ... LIKE, so the MART tables use CREATE TABLE AS with
--     WHERE FALSE (same columns, no rows; primary key not copied).
--   * No MERGE in the pinned DuckDB 1.1.3; transforms rebuild with DELETE + INSERT.
-- =============================================================================

-- One row per RAW code spelling. Several spellings can map to one ISO-3 country;
-- one MART table exists per DISTINCT COUNTRY_CODE. Contents to confirm (ADR 0006).
CREATE TABLE IF NOT EXISTS REF.COUNTRY_CODE_MAP (
    RAW_COUNTRY_CODE VARCHAR(5)   NOT NULL,   -- value as received, upper-cased and trimmed before lookup
    COUNTRY_CODE     CHAR(3)      NOT NULL,   -- ISO 3166-1 alpha-3
    COUNTRY_NAME     VARCHAR(100) NOT NULL,
    IS_ACTIVE        BOOLEAN      NOT NULL DEFAULT TRUE,
    PRIMARY KEY (RAW_COUNTRY_CODE)
);

INSERT INTO REF.COUNTRY_CODE_MAP (RAW_COUNTRY_CODE, COUNTRY_CODE, COUNTRY_NAME) VALUES
    ('USA','USA','United States'), ('US','USA','United States'),
    ('IND','IND','India'),         ('IN','IND','India'),
    ('CAN','CAN','Canada'),        ('CA','CAN','Canada'),
    ('PHIL','PHL','Philippines'),  ('PHL','PHL','Philippines'), ('PH','PHL','Philippines'),
    ('AUS','AUS','Australia'),     ('AU','AUS','Australia')
ON CONFLICT DO NOTHING;

-- One row per MEMBER_KEY (latest file_ts, then file_row_number wins; ADR 0003).
-- MEMBER_ID stays a plain column.
CREATE TABLE IF NOT EXISTS CORE.MEMBER_CURRENT (
    MEMBER_NAME      VARCHAR(255)  NOT NULL,
    MEMBER_ID        VARCHAR(18)   NOT NULL,
    ENROLLMENT_DATE  DATE          NOT NULL,
    LAST_FLIGHT_DATE DATE,
    TIER_CODE        CHAR(5),
    AGENT_NAME       VARCHAR(255),
    STATE            CHAR(5),
    COUNTRY          CHAR(5),
    POST_CODE        VARCHAR(5),
    DATE_OF_BIRTH    DATE,
    ACTIVE_MEMBER    CHAR(1),
    SOURCE_SYSTEM    VARCHAR(20)   NOT NULL,
    MEMBER_KEY       VARCHAR(40)   NOT NULL,   -- source_system || ':' || member_id (ADR 0002): the merge key
    COUNTRY_CODE     CHAR(3),                  -- NULL -> the row lives in MART.MEMBERS_UNASSIGNED
    AS_OF_DATE       DATE          NOT NULL,
    AGE              DECIMAL(3,0),
    STALE_MEMBER     BOOLEAN,                  -- NULL = unknown (no usable flight date)
    DQ_DOB_INVALID               BOOLEAN NOT NULL DEFAULT FALSE,
    DQ_LAST_FLIGHT_DATE_INVALID  BOOLEAN NOT NULL DEFAULT FALSE,
    DQ_COUNTRY_UNKNOWN           BOOLEAN NOT NULL DEFAULT FALSE,
    DQ_POST_CODE_INVALID         BOOLEAN NOT NULL DEFAULT FALSE,
    DQ_MEMBER_ID_SHORT           BOOLEAN NOT NULL DEFAULT FALSE,
    DQ_ACTIVE_FLAG_UNKNOWN       BOOLEAN NOT NULL DEFAULT FALSE,
    FILE_NAME        VARCHAR       NOT NULL,
    FILE_ROW_NUMBER  BIGINT        NOT NULL,
    FILE_TS          TIMESTAMP     NOT NULL,
    BATCH_ID         VARCHAR       NOT NULL,
    UPDATED_AT       TIMESTAMP     NOT NULL DEFAULT CURRENT_TIMESTAMP,   -- audit only
    PRIMARY KEY (MEMBER_KEY)                   -- enforced in DuckDB (informational in Snowflake)
);

-- Member country changes; the old country table keeps no trace of the member.
CREATE TABLE IF NOT EXISTS CORE.MEMBER_COUNTRY_HISTORY (
    MEMBER_KEY         VARCHAR(40) NOT NULL,
    MEMBER_ID          VARCHAR(18) NOT NULL,
    OLD_COUNTRY_CODE   CHAR(3),
    NEW_COUNTRY_CODE   CHAR(3),
    EFFECTIVE_FILE_TS  TIMESTAMP   NOT NULL,   -- file_ts of the record that moved the member
    BATCH_ID           VARCHAR     NOT NULL,
    RECORDED_AT        TIMESTAMP   NOT NULL DEFAULT CURRENT_TIMESTAMP
);

-- Country tables for the seed map (one per distinct COUNTRY_CODE).
CREATE TABLE IF NOT EXISTS MART.MEMBERS_USA AS SELECT * FROM CORE.MEMBER_CURRENT WHERE FALSE;
CREATE TABLE IF NOT EXISTS MART.MEMBERS_IND AS SELECT * FROM CORE.MEMBER_CURRENT WHERE FALSE;
CREATE TABLE IF NOT EXISTS MART.MEMBERS_CAN AS SELECT * FROM CORE.MEMBER_CURRENT WHERE FALSE;
CREATE TABLE IF NOT EXISTS MART.MEMBERS_PHL AS SELECT * FROM CORE.MEMBER_CURRENT WHERE FALSE;
CREATE TABLE IF NOT EXISTS MART.MEMBERS_AUS AS SELECT * FROM CORE.MEMBER_CURRENT WHERE FALSE;

-- Members with no or an unmapped country. Never dropped (ADR 0006).
CREATE TABLE IF NOT EXISTS MART.MEMBERS_UNASSIGNED AS SELECT * FROM CORE.MEMBER_CURRENT WHERE FALSE;
