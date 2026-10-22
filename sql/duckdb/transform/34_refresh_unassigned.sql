-- =============================================================================
-- 34_refresh_unassigned.sql  (DuckDB)
-- Purpose : Step 2, unassigned members: country_code is NULL, or is not an ACTIVE code in
--           REF.COUNTRY_CODE_MAP. Rebuilt from CORE.MEMBER_CURRENT, like the country tables.
-- =============================================================================
DELETE FROM MART.MEMBERS_UNASSIGNED;

INSERT INTO MART.MEMBERS_UNASSIGNED
SELECT m.*
FROM CORE.MEMBER_CURRENT m
WHERE m.COUNTRY_CODE IS NULL
   OR m.COUNTRY_CODE NOT IN (SELECT COUNTRY_CODE FROM REF.COUNTRY_CODE_MAP WHERE IS_ACTIVE);
