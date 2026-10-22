-- =============================================================================
-- 31_split_ambiguous.sql  (DuckDB)
-- Purpose : Step 1b. Members whose NEWEST record is not unique: two or more records share the
--           same (file_ts, file_row_number) for one member_key AND their content differs.
--           Identical content is not a conflict; it collapses to one record in step 1c.
--           A row in the result means the merge must stop and write nothing.
-- Content = every business, derived and flag column; file_name and batch_id are not content.
-- =============================================================================
WITH ranked AS (
    SELECT c.*,
           rank() OVER (PARTITION BY MEMBER_KEY ORDER BY FILE_TS DESC, FILE_ROW_NUMBER DESC) AS RNK
    FROM _split_candidate c
)
SELECT MEMBER_KEY, FILE_TS, FILE_ROW_NUMBER, count(*) AS RECORDS
FROM ranked
WHERE RNK = 1
GROUP BY MEMBER_KEY, FILE_TS, FILE_ROW_NUMBER
HAVING count(DISTINCT md5(concat_ws('|',
           coalesce(CAST(MEMBER_NAME AS VARCHAR), '<null>'), coalesce(CAST(MEMBER_ID AS VARCHAR), '<null>'),
           coalesce(CAST(ENROLLMENT_DATE AS VARCHAR), '<null>'), coalesce(CAST(LAST_FLIGHT_DATE AS VARCHAR), '<null>'),
           coalesce(CAST(TIER_CODE AS VARCHAR), '<null>'), coalesce(CAST(AGENT_NAME AS VARCHAR), '<null>'),
           coalesce(CAST(STATE AS VARCHAR), '<null>'), coalesce(CAST(COUNTRY AS VARCHAR), '<null>'),
           coalesce(CAST(POST_CODE AS VARCHAR), '<null>'), coalesce(CAST(DATE_OF_BIRTH AS VARCHAR), '<null>'),
           coalesce(CAST(ACTIVE_MEMBER AS VARCHAR), '<null>'), coalesce(CAST(SOURCE_SYSTEM AS VARCHAR), '<null>'),
           coalesce(CAST(COUNTRY_CODE AS VARCHAR), '<null>'), coalesce(CAST(AS_OF_DATE AS VARCHAR), '<null>'),
           coalesce(CAST(AGE AS VARCHAR), '<null>'), coalesce(CAST(STALE_MEMBER AS VARCHAR), '<null>'),
           coalesce(CAST(DQ_DOB_INVALID AS VARCHAR), '<null>'),
           coalesce(CAST(DQ_LAST_FLIGHT_DATE_INVALID AS VARCHAR), '<null>'),
           coalesce(CAST(DQ_COUNTRY_UNKNOWN AS VARCHAR), '<null>'),
           coalesce(CAST(DQ_POST_CODE_INVALID AS VARCHAR), '<null>'),
           coalesce(CAST(DQ_MEMBER_ID_SHORT AS VARCHAR), '<null>'),
           coalesce(CAST(DQ_ACTIVE_FLAG_UNKNOWN AS VARCHAR), '<null>')))) > 1;
