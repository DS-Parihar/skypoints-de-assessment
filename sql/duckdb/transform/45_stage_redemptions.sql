-- =============================================================================
-- 45_stage_redemptions.sql  (DuckDB)
-- Purpose : Type, validate and stage the flattened redemptions (_redemption_flat), applying
--           RD001..RD006 and recording the results. Never drops a record silently.
-- Needs   : temp tables _redemption_flat (42), _redemption_member (40/41),
--           _redemption_status(STATUS) = the allowed statuses (config),
-- Rules   : RD001 WARN    member with an empty redemptions array      -> DQ.ROW_FINDING
--           RD002 REJECT  duplicate txn_id, older version             -> DQ.QUARANTINE
--           RD003 REJECT  miles_redeemed < 0                          -> DQ.QUARANTINE
--           RD004 REJECT  status not an allowed status (or blank)     -> DQ.QUARANTINE
--           RD005 WARN    txn_date after feed_date (row still staged) -> DQ.ROW_FINDING
--           RD006 REJECT  a required value missing or unreadable      -> DQ.QUARANTINE
-- Duplicates: per txn_id the newest version wins by (feed_date, file_ts, file_row_number,
--           txn_seq), against the other rows of this load AND against STAGING.REDEMPTION_STG.
--           A newer version replaces the staged one (the old one goes to quarantine); an older
--           one is quarantined itself. Rejected rows never displace a staged row.
-- Leaves  : _rd_winner (rows staged by this load) and _rd_quarantine (rows quarantined by it).
-- Same logic as sql/snowflake/06_redemptions.sql.
-- =============================================================================

CREATE OR REPLACE TEMP TABLE _rd_check AS
WITH typed AS (
    SELECT f.*,
           CASE WHEN regexp_matches(f.FEED_DATE, '^[0-9]{8}$')
                THEN CAST(try_strptime(f.FEED_DATE, '%Y%m%d') AS DATE) END  AS FEED_DATE_D,
           CASE WHEN regexp_matches(f.TXN_DATE, '^[0-9]{8}$')
                THEN CAST(try_strptime(f.TXN_DATE, '%Y%m%d') AS DATE) END   AS TXN_DATE_D,
           TRY_CAST(f.MILES_REDEEMED AS BIGINT)                             AS MILES_I
    FROM _redemption_flat f
)
SELECT t.*,
       COALESCE(t.MILES_I < 0, FALSE) AS R3,
       (NULLIF(trim(t.STATUS), '') IS NULL
        OR NOT EXISTS (SELECT 1 FROM _redemption_status s WHERE s.STATUS = trim(t.STATUS))) AS R4,
       (NULLIF(trim(t.MEMBER_ID), '') IS NULL OR NULLIF(trim(t.TXN_ID), '') IS NULL
        OR t.FEED_DATE_D IS NULL OR t.TXN_DATE_D IS NULL OR t.MILES_I IS NULL)              AS R6
FROM typed t;

-- Valid rows ranked per txn_id: rank 1 is the newest version inside this load.
CREATE OR REPLACE TEMP TABLE _rd_ranked AS
SELECT c.*,
       row_number() OVER (PARTITION BY c.TXN_ID
                          ORDER BY c.FEED_DATE_D DESC, c.FILE_TS DESC, c.FILE_ROW_NUMBER DESC,
                                   c.TXN_SEQ DESC) AS RN
FROM _rd_check c
WHERE NOT (c.R3 OR c.R4 OR c.R6);

-- The load's winner per txn_id against what is already staged.
CREATE OR REPLACE TEMP TABLE _rd_winner AS
SELECT r.*,
       CASE WHEN s.TXN_ID IS NULL THEN 'INSERT'
            WHEN (r.FEED_DATE_D, r.FILE_TS, r.FILE_ROW_NUMBER, r.TXN_SEQ)
               > (s.FEED_DATE, s.FILE_TS, s.FILE_ROW_NUMBER, s.TXN_SEQ) THEN 'REPLACE'
            ELSE 'DROP' END AS ACTION
FROM _rd_ranked r
LEFT JOIN STAGING.REDEMPTION_STG s ON s.TXN_ID = r.TXN_ID
WHERE r.RN = 1;

-- Everything this load quarantines, with the original values as JSON.
CREATE OR REPLACE TEMP TABLE _rd_quarantine AS
SELECT FILE_NAME, FILE_ROW_NUMBER, BATCH_ID,
       concat_ws(',', CASE WHEN R3 THEN 'RD003' END, CASE WHEN R4 THEN 'RD004' END,
                      CASE WHEN R6 THEN 'RD006' END)                        AS RULE_CODE,
       CASE WHEN R3 THEN 'NEGATIVE_MILES' WHEN R4 THEN 'UNKNOWN_STATUS'
            ELSE 'INVALID_VALUE' END                                        AS REASON_CODE,
       concat_ws('; ', CASE WHEN R3 THEN 'miles_redeemed is negative' END,
                       CASE WHEN R4 THEN 'status is not one of the allowed statuses' END,
                       CASE WHEN R6 THEN 'a required value is missing or unreadable' END) AS REASON_DETAIL,
       json_object('member_id', MEMBER_ID, 'feed_date', FEED_DATE, 'txn_id', TXN_ID,
                   'txn_date', TXN_DATE, 'partner', PARTNER, 'miles_redeemed', MILES_REDEEMED,
                   'status', STATUS)                                        AS RAW_PAYLOAD
FROM _rd_check
WHERE R3 OR R4 OR R6
UNION ALL   -- older duplicates inside this load
SELECT FILE_NAME, FILE_ROW_NUMBER, BATCH_ID, 'RD002', 'DUPLICATE_TXN_OLDER',
       'txn_id ' || TXN_ID || ' has a newer version; this older one is dropped',
       json_object('member_id', MEMBER_ID, 'feed_date', FEED_DATE, 'txn_id', TXN_ID,
                   'txn_date', TXN_DATE, 'partner', PARTNER, 'miles_redeemed', MILES_REDEEMED,
                   'status', STATUS)
FROM _rd_ranked WHERE RN > 1
UNION ALL   -- this load's newest version is older than the one already staged
SELECT FILE_NAME, FILE_ROW_NUMBER, BATCH_ID, 'RD002', 'DUPLICATE_TXN_OLDER',
       'txn_id ' || TXN_ID || ' is already staged with a newer version; this older one is dropped',
       json_object('member_id', MEMBER_ID, 'feed_date', FEED_DATE, 'txn_id', TXN_ID,
                   'txn_date', TXN_DATE, 'partner', PARTNER, 'miles_redeemed', MILES_REDEEMED,
                   'status', STATUS)
FROM _rd_winner WHERE ACTION = 'DROP'
UNION ALL   -- the staged version is replaced by a newer one: keep the old one visible
SELECT s.FILE_NAME, s.FILE_ROW_NUMBER, w.BATCH_ID, 'RD002', 'DUPLICATE_TXN_OLDER',
       'txn_id ' || s.TXN_ID || ' was replaced by a newer version',
       json_object('member_id', s.MEMBER_ID, 'feed_date', CAST(s.FEED_DATE AS VARCHAR),
                   'txn_id', s.TXN_ID, 'txn_date', CAST(s.TXN_DATE AS VARCHAR),
                   'partner', s.PARTNER, 'miles_redeemed', CAST(s.MILES_REDEEMED AS VARCHAR),
                   'status', s.STATUS)
FROM _rd_winner w JOIN STAGING.REDEMPTION_STG s ON s.TXN_ID = w.TXN_ID
WHERE w.ACTION = 'REPLACE';

INSERT INTO DQ.QUARANTINE
    (SOURCE_TABLE, FILE_NAME, FILE_ROW_NUMBER, BATCH_ID, REASON_CODE, REASON_DETAIL, RULE_CODE,
     SEVERITY, RAW_PAYLOAD)
SELECT 'RAW.REDEMPTION_FEED_RAW', FILE_NAME, FILE_ROW_NUMBER, BATCH_ID, REASON_CODE, REASON_DETAIL,
       RULE_CODE, 'REJECT', RAW_PAYLOAD
FROM _rd_quarantine;

-- A newer version replaces the staged row in place. UPDATE, not DELETE + INSERT: DuckDB rejects
-- re-inserting a primary key it deleted in the same transaction.
UPDATE STAGING.REDEMPTION_STG s
SET MEMBER_ID = trim(w.MEMBER_ID), MEMBER_KEY = 'SKYPOINTS:' || trim(w.MEMBER_ID),
    FEED_DATE = w.FEED_DATE_D, TXN_DATE = w.TXN_DATE_D, PARTNER = NULLIF(trim(w.PARTNER), ''),
    MILES_REDEEMED = w.MILES_I, STATUS = trim(w.STATUS),
    DQ_TXN_AFTER_FEED = w.TXN_DATE_D > w.FEED_DATE_D, FILE_NAME = w.FILE_NAME,
    FILE_ROW_NUMBER = w.FILE_ROW_NUMBER, TXN_SEQ = w.TXN_SEQ, FILE_TS = w.FILE_TS,
    BATCH_ID = w.BATCH_ID
FROM _rd_winner w
WHERE s.TXN_ID = w.TXN_ID AND w.ACTION = 'REPLACE';

INSERT INTO STAGING.REDEMPTION_STG
    (MEMBER_ID, MEMBER_KEY, FEED_DATE, TXN_ID, TXN_DATE, PARTNER, MILES_REDEEMED, STATUS,
     DQ_TXN_AFTER_FEED, FILE_NAME, FILE_ROW_NUMBER, TXN_SEQ, FILE_TS, BATCH_ID)
SELECT trim(MEMBER_ID), 'SKYPOINTS:' || trim(MEMBER_ID), FEED_DATE_D, trim(TXN_ID), TXN_DATE_D,
       NULLIF(trim(PARTNER), ''), MILES_I, trim(STATUS), TXN_DATE_D > FEED_DATE_D,
       FILE_NAME, FILE_ROW_NUMBER, TXN_SEQ, FILE_TS, BATCH_ID
FROM _rd_winner
WHERE ACTION = 'INSERT';

-- RD001: a member object with no transactions is recorded, not rejected.
INSERT INTO DQ.ROW_FINDING
    (BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, RULE_CODE, SEVERITY, COLUMN_NAME, VALUE, REASON)
SELECT BATCH_ID, FILE_NAME, FILE_ROW_NUMBER,
       'SKYPOINTS:' || json_extract_string(PAYLOAD, '$.member_id'), 'RD001', 'WARN', NULL, NULL,
       'the member has an empty redemptions array'
FROM _redemption_member
WHERE COALESCE(json_array_length(json_extract(PAYLOAD, '$.redemptions')), 0) = 0;

-- RD005: txn_date after feed_date; the row is staged, with the flag set.
INSERT INTO DQ.ROW_FINDING
    (BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, MEMBER_KEY, RULE_CODE, SEVERITY, COLUMN_NAME, VALUE, REASON)
SELECT BATCH_ID, FILE_NAME, FILE_ROW_NUMBER, 'SKYPOINTS:' || trim(MEMBER_ID), 'RD005', 'WARN',
       'txn_date', TXN_DATE, 'txn_date is after feed_date'
FROM _rd_winner
WHERE ACTION IN ('INSERT', 'REPLACE') AND TXN_DATE_D > FEED_DATE_D;
