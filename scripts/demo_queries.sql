-- =============================================================================
-- demo_queries.sql : the queries to show live, in the order of the story.
-- Run against the pipeline database (DuckDB), for example:
--     python -m skypoints.pipeline --as-of 2024-01-15 --db data/processed/skypoints.duckdb
--     duckdb data/processed/skypoints.duckdb < scripts/demo_queries.sql
-- Every statement ends with a semicolon and contains no semicolon inside a string.
-- =============================================================================

-- Q1. Rows per layer: nothing is lost between raw and the tables people use.
SELECT 'RAW member rows' AS layer, count(*) AS n FROM raw.member_feed_raw WHERE record_type = 'D'
UNION ALL SELECT 'STAGING members', count(*) FROM staging.member_stg
UNION ALL SELECT 'Quarantined member rows', count(*) FROM dq.quarantine WHERE source_table = 'RAW.MEMBER_FEED_RAW'
UNION ALL SELECT 'CORE members (one per member_key)', count(*) FROM core.member_current
UNION ALL SELECT 'RAW redemption members', count(*) FROM raw.redemption_feed_raw
UNION ALL SELECT 'STAGING redemptions', count(*) FROM staging.redemption_stg
UNION ALL SELECT 'CORE redemption transactions', count(*) FROM core.redemption_txn;

-- Q2. Members per country table (a member is in exactly one). Add a UNION for any new country.
SELECT 'MEMBERS_AUS' AS country_table, count(*) AS n FROM mart.members_aus
UNION ALL SELECT 'MEMBERS_CAN', count(*) FROM mart.members_can
UNION ALL SELECT 'MEMBERS_IND', count(*) FROM mart.members_ind
UNION ALL SELECT 'MEMBERS_PHL', count(*) FROM mart.members_phl
UNION ALL SELECT 'MEMBERS_USA', count(*) FROM mart.members_usa
UNION ALL SELECT 'MEMBERS_UNASSIGNED', count(*) FROM mart.members_unassigned;

-- Q3. Raw equals staged plus quarantined: the difference must be 0.
SELECT (SELECT count(*) FROM raw.member_feed_raw WHERE record_type = 'D')
     - (SELECT count(*) FROM staging.member_stg)
     - (SELECT count(*) FROM dq.quarantine WHERE source_table = 'RAW.MEMBER_FEED_RAW') AS unexplained_member_rows;

-- Q4. Latest record wins: every version of Ravi in staging, and the one that is current.
SELECT 'staging' AS layer, file_name, file_ts, country_code FROM staging.member_stg WHERE member_key = 'SKYPOINTS:223458'
UNION ALL
SELECT 'CORE (current)', file_name, file_ts, country_code FROM core.member_current WHERE member_key = 'SKYPOINTS:223458'
ORDER BY file_ts, layer;

-- Q5. A member who moved country: the history, and where he is now.
SELECT member_key, old_country_code, new_country_code, effective_file_ts FROM core.member_country_history ORDER BY effective_file_ts;

-- Q6. ...and he is only in the new country table.
SELECT 'MEMBERS_IND' AS country_table, count(*) AS rows_for_ravi FROM mart.members_ind WHERE member_key = 'SKYPOINTS:223458'
UNION ALL SELECT 'MEMBERS_USA', count(*) FROM mart.members_usa WHERE member_key = 'SKYPOINTS:223458';

-- Q7. Age and stale flag, computed from the explicit as-of date (never today's date).
SELECT member_name, date_of_birth, as_of_date, age, last_flight_date, stale_member, country_code
FROM core.member_current ORDER BY member_name;

-- Q8. Data quality: how many rows each rule caught, by severity.
SELECT rule_code, severity, count(*) AS findings FROM dq.row_finding GROUP BY rule_code, severity ORDER BY rule_code;

-- Q9. Quarantined rows with the reason code and the original values.
SELECT source_table, file_name, file_row_number, rule_code, reason_code, reason_detail, raw_payload
FROM dq.quarantine ORDER BY source_table, file_name, file_row_number;

-- Q10. Raw keeps text: the leading zero of a date of birth survives; staging has the typed date.
SELECT r.member_name, r.date_of_birth AS raw_dob_text, s.date_of_birth AS staged_dob, r.post_code AS raw_post_code
FROM raw.member_feed_raw r JOIN staging.member_stg s USING (file_name, file_row_number)
WHERE r.record_type = 'D' ORDER BY r.member_name LIMIT 5;

-- Q11. Redemptions per member: completed and pending miles, last date, current country.
SELECT member_key, member_matched, member_name, current_country_code, total_completed_miles,
       pending_miles, txn_count, last_redemption_date
FROM mart.member_redemption_summary ORDER BY member_matched, total_completed_miles DESC;

-- Q12. Orphans: redemptions whose member is not in the member feed.
SELECT member_key, total_completed_miles, pending_miles, txn_count
FROM mart.member_redemption_summary WHERE NOT member_matched;
