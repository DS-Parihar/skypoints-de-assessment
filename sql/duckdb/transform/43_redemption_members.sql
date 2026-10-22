-- =============================================================================
-- 43_redemption_members.sql  (DuckDB)
-- Purpose : Every member object of the feed with its transaction count, including members with
--           an empty (or missing) redemptions array: they produce no flattened row but are
--           recorded. Reads _redemption_member.
-- =============================================================================
SELECT json_extract_string(PAYLOAD, '$.member_id')  AS MEMBER_ID,
       json_extract_string(PAYLOAD, '$.feed_date')  AS FEED_DATE,
       COALESCE(json_array_length(json_extract(PAYLOAD, '$.redemptions')), 0) AS REDEMPTION_COUNT,
       FILE_ROW_NUMBER
FROM _redemption_member
ORDER BY FILE_ROW_NUMBER;
