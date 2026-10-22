-- =============================================================================
-- 47_promote_redemptions.sql  (DuckDB)
-- Purpose : Step 2: STAGING.REDEMPTION_STG -> CORE.REDEMPTION_TXN, one row per txn_id.
--           INSERT when new; UPDATE only when STRICTLY newer by (feed_date, file_ts,
--           file_row_number, txn_seq); otherwise leave it. Re-running changes nothing.
-- No MERGE in DuckDB 1.1.3: UPDATE + INSERT inside the caller's transaction.
-- Leaves _promote_decision for the caller to count.
-- =============================================================================
CREATE OR REPLACE TEMP TABLE _promote_decision AS
SELECT p.*,
       CASE WHEN c.TXN_ID IS NULL THEN 'INSERT'
            WHEN (p.FEED_DATE, p.FILE_TS, p.FILE_ROW_NUMBER, p.TXN_SEQ)
               > (c.FEED_DATE, c.FILE_TS, c.FILE_ROW_NUMBER, c.TXN_SEQ) THEN 'UPDATE'
            ELSE 'KEEP' END AS ACTION
FROM _promote_candidate p
LEFT JOIN CORE.REDEMPTION_TXN c ON c.TXN_ID = p.TXN_ID;

UPDATE CORE.REDEMPTION_TXN c
SET MEMBER_ID = d.MEMBER_ID, MEMBER_KEY = d.MEMBER_KEY, FEED_DATE = d.FEED_DATE,
    TXN_DATE = d.TXN_DATE, PARTNER = d.PARTNER, MILES_REDEEMED = d.MILES_REDEEMED,
    STATUS = d.STATUS, DQ_TXN_AFTER_FEED = d.DQ_TXN_AFTER_FEED, FILE_NAME = d.FILE_NAME,
    FILE_ROW_NUMBER = d.FILE_ROW_NUMBER, TXN_SEQ = d.TXN_SEQ, FILE_TS = d.FILE_TS,
    BATCH_ID = d.BATCH_ID, UPDATED_AT = CURRENT_TIMESTAMP
FROM _promote_decision d
WHERE c.TXN_ID = d.TXN_ID AND d.ACTION = 'UPDATE';

INSERT INTO CORE.REDEMPTION_TXN
    (TXN_ID, MEMBER_ID, MEMBER_KEY, FEED_DATE, TXN_DATE, PARTNER, MILES_REDEEMED, STATUS,
     DQ_TXN_AFTER_FEED, FILE_NAME, FILE_ROW_NUMBER, TXN_SEQ, FILE_TS, BATCH_ID)
SELECT TXN_ID, MEMBER_ID, MEMBER_KEY, FEED_DATE, TXN_DATE, PARTNER, MILES_REDEEMED, STATUS,
       DQ_TXN_AFTER_FEED, FILE_NAME, FILE_ROW_NUMBER, TXN_SEQ, FILE_TS, BATCH_ID
FROM _promote_decision
WHERE ACTION = 'INSERT';
