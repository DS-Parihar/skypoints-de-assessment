-- =============================================================================
-- 06_redemption_summary.sql  (DuckDB)
-- Purpose : MART.MEMBER_REDEMPTION_SUMMARY, one row per member_key that has redemptions.
--           Same logic and names as the view in sql/snowflake/06_redemptions.sql.
-- Join    : CORE.REDEMPTION_TXN LEFT JOIN CORE.MEMBER_CURRENT on member_key (= 'SKYPOINTS:' ||
--           member_id, ADR 0002). The LEFT JOIN starts from the transactions, so a redemption
--           whose member is not in the member feed is visible: MEMBER_MATCHED = FALSE and no
--           name or country. A member without redemptions is not listed here.
-- Columns : TOTAL_COMPLETED_MILES, PENDING_MILES (0 when none), TXN_COUNT, and
--           LAST_REDEMPTION_DATE = the latest txn_date over all statuses.
--           CURRENT_COUNTRY_CODE is the member's country now (NULL when unassigned or an orphan),
--           so a member who moved shows the new country; it uses the same "active in
--           REF.COUNTRY_CODE_MAP" rule as the country tables.
-- =============================================================================
CREATE OR REPLACE VIEW MART.MEMBER_REDEMPTION_SUMMARY AS
SELECT t.MEMBER_KEY,
       t.MEMBER_ID,
       (m.MEMBER_KEY IS NOT NULL)                                         AS MEMBER_MATCHED,
       m.MEMBER_NAME,
       CASE WHEN m.COUNTRY_CODE IN (SELECT COUNTRY_CODE FROM REF.COUNTRY_CODE_MAP WHERE IS_ACTIVE)
            THEN m.COUNTRY_CODE END                                       AS CURRENT_COUNTRY_CODE,
       COALESCE(SUM(CASE WHEN t.STATUS = 'COMPLETED' THEN t.MILES_REDEEMED END), 0) AS TOTAL_COMPLETED_MILES,
       COALESCE(SUM(CASE WHEN t.STATUS = 'PENDING' THEN t.MILES_REDEEMED END), 0)   AS PENDING_MILES,
       COUNT(*)                                                           AS TXN_COUNT,
       MAX(t.TXN_DATE)                                                    AS LAST_REDEMPTION_DATE
FROM CORE.REDEMPTION_TXN t
LEFT JOIN CORE.MEMBER_CURRENT m ON m.MEMBER_KEY = t.MEMBER_KEY
GROUP BY t.MEMBER_KEY, t.MEMBER_ID, m.MEMBER_KEY, m.MEMBER_NAME, m.COUNTRY_CODE;
