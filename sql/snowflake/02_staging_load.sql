-- =============================================================================
-- 02_staging_load.sql  (Snowflake)
-- Purpose : Populate STAGING.MEMBER_STG from RAW.MEMBER_FEED_RAW (through the typed view
--           STAGING.MEMBER_PREPARED_V, which does the TRY_TO_DATE date typing and the
--           NULLIF(TRIM(col), '') cleaning) and derive AGE and STALE_MEMBER.
-- Run     : once per batch, after setting the two session variables:
--               SET batch_id    = '<batch id>';
--               SET as_of_date  = '2024-01-15';
--           The as-of date is bound, never CURRENT_DATE() inside the logic.
-- Rules (same as sql/duckdb/load_staging.sql and skypoints.transform.derive):
--   AGE          whole years completed at as_of. The month-day comparison is the birthday
--                adjustment (DATEDIFF('year') would only count calendar-year boundaries and
--                is wrong for 31 Dec -> 1 Jan). A 29 February birthday counts from 1 March in
--                non-leap years. NULL when there is no (valid) DOB or the DOB is after as_of.
--   STALE_MEMBER TRUE when the last flight is MORE than 90 days before as_of, FALSE otherwise.
--                NULL (unknown) when there is no flight date or it is after as_of.
-- Re-runnable: a (file_name, file_row_number) already staged is not added twice.
-- Rejected rows are handled by transform/10_quarantine_members.sql.
-- =============================================================================
INSERT INTO STAGING.MEMBER_STG
    (MEMBER_NAME, MEMBER_ID, ENROLLMENT_DATE, LAST_FLIGHT_DATE, TIER_CODE, AGENT_NAME, STATE,
     COUNTRY, POST_CODE, DATE_OF_BIRTH, ACTIVE_MEMBER, SOURCE_SYSTEM, MEMBER_KEY, COUNTRY_CODE,
     AS_OF_DATE, AGE, STALE_MEMBER,
     DQ_DOB_INVALID, DQ_LAST_FLIGHT_DATE_INVALID, DQ_COUNTRY_UNKNOWN, DQ_POST_CODE_INVALID,
     DQ_MEMBER_ID_SHORT, DQ_ACTIVE_FLAG_UNKNOWN,
     FILE_NAME, FILE_ROW_NUMBER, FILE_TS, BATCH_ID)
WITH params AS (
    SELECT TO_DATE($as_of_date) AS AS_OF
)
SELECT p.MEMBER_NAME, p.MEMBER_ID, p.ENROLLMENT_DATE, p.LAST_FLIGHT_DATE, p.TIER_CODE, p.AGENT_NAME,
       p.STATE, p.COUNTRY, p.POST_CODE, p.DATE_OF_BIRTH, p.ACTIVE_MEMBER, p.SOURCE_SYSTEM,
       p.MEMBER_KEY, p.COUNTRY_CODE,
       a.AS_OF,
       CASE WHEN p.DATE_OF_BIRTH IS NULL OR p.DATE_OF_BIRTH > a.AS_OF THEN NULL
            ELSE YEAR(a.AS_OF) - YEAR(p.DATE_OF_BIRTH)
                 - IFF(MONTH(a.AS_OF) * 100 + DAY(a.AS_OF)
                       < MONTH(p.DATE_OF_BIRTH) * 100 + DAY(p.DATE_OF_BIRTH), 1, 0)
       END,
       CASE WHEN p.LAST_FLIGHT_DATE IS NULL OR p.LAST_FLIGHT_DATE > a.AS_OF THEN NULL
            ELSE DATEDIFF(day, p.LAST_FLIGHT_DATE, a.AS_OF) > 90 END,
       p.DQ_DOB_INVALID, p.DQ_LAST_FLIGHT_DATE_INVALID, p.DQ_COUNTRY_UNKNOWN, p.DQ_POST_CODE_INVALID,
       p.DQ_MEMBER_ID_SHORT, p.DQ_ACTIVE_FLAG_UNKNOWN,
       p.FILE_NAME, p.FILE_ROW_NUMBER, p.FILE_TS, p.BATCH_ID
FROM STAGING.MEMBER_PREPARED_V p
CROSS JOIN params a
WHERE p.BATCH_ID = $batch_id
  AND p.REJECT_REASON IS NULL
  AND NOT EXISTS (
      SELECT 1 FROM STAGING.MEMBER_STG s
      WHERE s.FILE_NAME = p.FILE_NAME AND s.FILE_ROW_NUMBER = p.FILE_ROW_NUMBER);
