-- =============================================================================
-- 42_redemption_flatten.sql  (DuckDB)
-- Purpose : Flatten _redemption_member into _redemption_flat: one row per transaction, every
--           value as text (json_extract_string gives 7890 for the number 7890 and '007890' for
--           the string "007890"). UNNEST explodes the redemptions array; generate_subscripts
--           numbers the transactions inside each member (txn_seq). A member with an empty array
--           produces no row. Typing and rules come later (45_stage_redemptions.sql).
-- Same logic as the LATERAL FLATTEN in sql/snowflake/06_redemptions.sql.
-- =============================================================================
CREATE OR REPLACE TEMP TABLE _redemption_flat AS
WITH members AS (
    SELECT FILE_NAME, FILE_ROW_NUMBER, FILE_TS, BATCH_ID, PAYLOAD,
           CAST(json_extract(PAYLOAD, '$.redemptions') AS JSON[]) AS ARR
    FROM _redemption_member
),
exploded AS (
    SELECT m.*, unnest(m.ARR) AS TXN, generate_subscripts(m.ARR, 1) AS TXN_SEQ FROM members m
)
SELECT json_extract_string(PAYLOAD, '$.member_id')   AS MEMBER_ID,
       json_extract_string(PAYLOAD, '$.feed_date')   AS FEED_DATE,
       json_extract_string(TXN, '$.txn_id')          AS TXN_ID,
       json_extract_string(TXN, '$.txn_date')        AS TXN_DATE,
       json_extract_string(TXN, '$.partner')         AS PARTNER,
       json_extract_string(TXN, '$.miles_redeemed')  AS MILES_REDEEMED,
       json_extract_string(TXN, '$.status')          AS STATUS,
       FILE_NAME, FILE_ROW_NUMBER, TXN_SEQ, FILE_TS, BATCH_ID
FROM exploded;
