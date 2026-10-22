-- =============================================================================
-- 03_targets.sql  (Snowflake)
-- Purpose : Country map (config), the conformed CORE.MEMBER_CURRENT table,
--           the move history table, and one MART.MEMBERS_<ISO3> table per
--           country plus MART.MEMBERS_UNASSIGNED.
-- Design  : Country tables are DERIVED from CORE.MEMBER_CURRENT (ADR 0004), so a
--           member can only be in one of them. ADR 0007 describes the cheaper
--           alternative (one clustered table + secure views per country).
-- =============================================================================

USE DATABASE SKYPOINTS;

-- -----------------------------------------------------------------------------
-- REF.COUNTRY_CODE_MAP : one row per RAW code spelling. Several spellings can
-- map to one ISO-3 country, and one MART table is created per DISTINCT
-- COUNTRY_CODE. Adding a country = adding a row, then CALL the procedure below.
-- The contents are an assumption to confirm with the data owner (ADR 0006).
-- -----------------------------------------------------------------------------
CREATE TABLE IF NOT EXISTS REF.COUNTRY_CODE_MAP (
    RAW_COUNTRY_CODE VARCHAR(5)   NOT NULL,   -- value as received, upper-cased and trimmed before lookup
    COUNTRY_CODE     CHAR(3)      NOT NULL,   -- ISO 3166-1 alpha-3
    COUNTRY_NAME     VARCHAR(100) NOT NULL,
    IS_ACTIVE        BOOLEAN      NOT NULL DEFAULT TRUE,
    CONSTRAINT PK_COUNTRY_CODE_MAP PRIMARY KEY (RAW_COUNTRY_CODE)
)
COMMENT = 'Maps raw country spellings to ISO-3; drives routing and country table creation';

MERGE INTO REF.COUNTRY_CODE_MAP t
USING (SELECT * FROM VALUES
         ('USA','USA','United States'), ('US','USA','United States'),
         ('IND','IND','India'),         ('IN','IND','India'),
         ('CAN','CAN','Canada'),        ('CA','CAN','Canada'),
         ('PHIL','PHL','Philippines'),  ('PHL','PHL','Philippines'), ('PH','PHL','Philippines'),
         ('AUS','AUS','Australia'),     ('AU','AUS','Australia')
       AS v(RAW_COUNTRY_CODE, COUNTRY_CODE, COUNTRY_NAME)) s
   ON t.RAW_COUNTRY_CODE = s.RAW_COUNTRY_CODE
WHEN NOT MATCHED THEN INSERT (RAW_COUNTRY_CODE, COUNTRY_CODE, COUNTRY_NAME)
     VALUES (s.RAW_COUNTRY_CODE, s.COUNTRY_CODE, s.COUNTRY_NAME);

-- -----------------------------------------------------------------------------
-- CORE.MEMBER_CURRENT : exactly one row per MEMBER_ID (latest file_ts, then
-- file_row_number wins; ADR 0003). Same business columns as staging.
-- -----------------------------------------------------------------------------
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
    COUNTRY_CODE     CHAR(3),                  -- NULL -> the row lives in MART.MEMBERS_UNASSIGNED
    AS_OF_DATE       DATE          NOT NULL,
    AGE              NUMBER(3,0),
    STALE_MEMBER     BOOLEAN       NOT NULL,
    DQ_DOB_INVALID               BOOLEAN NOT NULL DEFAULT FALSE,
    DQ_LAST_FLIGHT_DATE_INVALID  BOOLEAN NOT NULL DEFAULT FALSE,
    DQ_COUNTRY_UNKNOWN           BOOLEAN NOT NULL DEFAULT FALSE,
    DQ_POST_CODE_INVALID         BOOLEAN NOT NULL DEFAULT FALSE,
    DQ_MEMBER_ID_SHORT           BOOLEAN NOT NULL DEFAULT FALSE,
    DQ_ACTIVE_FLAG_UNKNOWN       BOOLEAN NOT NULL DEFAULT FALSE,
    FILE_NAME        VARCHAR       NOT NULL,
    FILE_ROW_NUMBER  NUMBER(38,0)  NOT NULL,
    FILE_TS          TIMESTAMP_NTZ NOT NULL,
    BATCH_ID         VARCHAR       NOT NULL,
    UPDATED_AT       TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP(),   -- audit only
    CONSTRAINT PK_MEMBER_CURRENT PRIMARY KEY (MEMBER_ID)    -- informational in Snowflake; the MERGE enforces it
)
-- CLUSTER BY MEMBER_ID: the incremental MERGE joins on MEMBER_ID, so clustering lets Snowflake
-- prune micro-partitions and touch only those holding the changed members instead of scanning
-- a table with billions of rows. The key has high cardinality, which suits equality joins.
-- Not clustered by COUNTRY_CODE: it has few values, so it would not prune MERGE lookups.
CLUSTER BY (MEMBER_ID)
COMMENT = 'One row per member, latest record wins';

-- History of country moves: written when a member's country differs from the previous run.
CREATE TABLE IF NOT EXISTS CORE.MEMBER_COUNTRY_HISTORY (
    MEMBER_ID          VARCHAR(18)   NOT NULL,
    OLD_COUNTRY_CODE   CHAR(3),
    NEW_COUNTRY_CODE   CHAR(3),
    EFFECTIVE_FILE_TS  TIMESTAMP_NTZ NOT NULL,   -- file_ts of the record that moved the member
    BATCH_ID           VARCHAR       NOT NULL,
    RECORDED_AT        TIMESTAMP_NTZ NOT NULL DEFAULT CURRENT_TIMESTAMP()
)
-- Append-only and read by MEMBER_ID: clustering keeps "history of one member" cheap.
CLUSTER BY (MEMBER_ID)
COMMENT = 'Member country changes; the old country table keeps no trace';

-- -----------------------------------------------------------------------------
-- MART.CREATE_COUNTRY_TABLES : creates MART.MEMBERS_<ISO3> for every distinct
-- active COUNTRY_CODE in the map. Safe to re-run; run again after adding a country.
-- Each table copies CORE.MEMBER_CURRENT's columns (CREATE TABLE ... LIKE).
-- -----------------------------------------------------------------------------
CREATE OR REPLACE PROCEDURE MART.CREATE_COUNTRY_TABLES()
RETURNS VARCHAR
LANGUAGE SQL
AS
$$
DECLARE
    c CURSOR FOR SELECT DISTINCT COUNTRY_CODE FROM REF.COUNTRY_CODE_MAP WHERE IS_ACTIVE ORDER BY 1;
    n NUMBER DEFAULT 0;
BEGIN
    FOR rec IN c DO
        LET tbl VARCHAR := 'MART.MEMBERS_' || rec.COUNTRY_CODE;
        EXECUTE IMMEDIATE 'CREATE TABLE IF NOT EXISTS ' || :tbl || ' LIKE CORE.MEMBER_CURRENT';
        -- Country tables stay MEMBER_ID-clustered for the same reason as MEMBER_CURRENT: the
        -- per-country MERGE and the delete-on-move join on MEMBER_ID. The country is constant
        -- inside one table, so clustering by it would add nothing.
        EXECUTE IMMEDIATE 'ALTER TABLE ' || :tbl || ' CLUSTER BY (MEMBER_ID)';
        n := n + 1;
    END FOR;
    RETURN n || ' country table(s) ensured';
END;
$$;

CALL MART.CREATE_COUNTRY_TABLES();

-- Members with no or an unmapped country. Never dropped (ADR 0006).
CREATE TABLE IF NOT EXISTS MART.MEMBERS_UNASSIGNED LIKE CORE.MEMBER_CURRENT;
ALTER TABLE MART.MEMBERS_UNASSIGNED CLUSTER BY (MEMBER_ID);
