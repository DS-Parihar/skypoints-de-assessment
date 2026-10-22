-- =============================================================================
-- 33_refresh_country_table.sql  (DuckDB)
-- Purpose : Step 2. Rebuild one MART.MEMBERS_<ISO3> table from CORE.MEMBER_CURRENT.
--           The table is replaced, so a member who moved is gone from the old table the moment
--           CORE says so. A table is a template for ONE country: skypoints.transform.country_split
--           fills {table} (after checking the ISO code) for every active country in
--           REF.COUNTRY_CODE_MAP; no country list is written in code or here.
-- Params  : $country_code  the ISO-3 code this table holds
-- Only members whose code is ACTIVE in REF.COUNTRY_CODE_MAP can be here; everyone else is in
-- MEMBERS_UNASSIGNED (34_refresh_unassigned.sql).
-- =============================================================================
DELETE FROM MART.{table};

INSERT INTO MART.{table}
SELECT m.*
FROM CORE.MEMBER_CURRENT m
WHERE m.COUNTRY_CODE = $country_code
  AND m.COUNTRY_CODE IN (SELECT COUNTRY_CODE FROM REF.COUNTRY_CODE_MAP WHERE IS_ACTIVE);
