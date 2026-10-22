"""Export the processed data as CSV files anyone can open.

    export_csv(db_path, out_dir) -> list of the files written

One CSV per table or view, UTF-8, comma separated, with a header row; dates are ISO (2024-01-15),
booleans are true/false, NULL is an empty field. The database is only read. Files:

  members.csv                 CORE.MEMBER_CURRENT, the readable columns
  members_<iso3>.csv          one per MART country table (header only when empty), and
  members_unassigned.csv      the same columns as members.csv
  member_country_history.csv  every country move
  quarantine.csv              rejected rows with the rule, the reason and the original values
  dq_findings.csv             warnings and routing findings, one row each
  dq_summary.csv              rule_code, failed_count over all batches (DQ and RD rules)
  redemptions.csv             CORE.REDEMPTION_TXN
  redemption_summary.csv      MART.MEMBER_REDEMPTION_SUMMARY (orphans included)

The pipeline writes these (plus run_report.txt, the printed report) after every run, into
<database folder>/export unless --export-dir says otherwise, or --no-export is given.
"""

from __future__ import annotations

from pathlib import Path

import duckdb

MEMBER_COLUMNS = (
    "member_key",
    "source_system",
    "member_id",
    "member_name",
    "country_code",
    "tier_code",
    "date_of_birth",
    "age",
    "enrollment_date",
    "last_flight_date",
    "stale_member",
    "file_name",
)
FIXED_FILES = (
    "members.csv",
    "member_country_history.csv",
    "quarantine.csv",
    "dq_findings.csv",
    "dq_summary.csv",
    "redemptions.csv",
    "redemption_summary.csv",
)

# Rule codes with the number of rows they caught, over all batches: the member rules from
# DQ.RUN_SUMMARY, and the redemption rules (RD001 and RD005 are findings; the rest quarantine).
DQ_SUMMARY_SQL = """
SELECT rule_code AS rule_code, CAST(sum(n) AS BIGINT) AS failed_count
FROM (
    SELECT rule_code, sum(failed_count) AS n FROM dq.run_summary GROUP BY rule_code
    UNION ALL
    SELECT rule_code, count(*) FROM dq.row_finding WHERE rule_code LIKE 'RD%' GROUP BY rule_code
    UNION ALL
    SELECT code, count(*) FROM (
        SELECT unnest(string_split(rule_code, ',')) AS code FROM dq.quarantine
        WHERE source_table = 'RAW.REDEMPTION_FEED_RAW') GROUP BY code
) GROUP BY rule_code ORDER BY rule_code
"""


def _select(columns: tuple[str, ...], source: str, order: str) -> str:
    """SELECT with lower-case column names, so every file has the same readable header."""
    return f"SELECT {', '.join(f'{c} AS {c}' for c in columns)} FROM {source} ORDER BY {order}"


def _queries(con: duckdb.DuckDBPyConnection) -> dict[str, str]:
    members = _select(MEMBER_COLUMNS, "{table}", "member_key")
    queries = {
        "members.csv": members.format(table="core.member_current"),
        "member_country_history.csv": _select(
            ("member_key", "old_country_code", "new_country_code", "effective_file_ts", "batch_id"),
            "core.member_country_history",
            "effective_file_ts, member_key",
        ),
        "quarantine.csv": _select(
            (
                "source_table",
                "file_name",
                "file_row_number",
                "rule_code",
                "reason_code",
                "reason_detail",
                "raw_payload",
            ),
            "dq.quarantine",
            "source_table, file_name, file_row_number",
        ),
        "dq_findings.csv": _select(
            (
                "batch_id",
                "file_name",
                "file_row_number",
                "member_key",
                "rule_code",
                "severity",
                "column_name",
                "value",
                "reason",
            ),
            "dq.row_finding",
            "rule_code, file_name, file_row_number, member_key",
        ),
        "dq_summary.csv": DQ_SUMMARY_SQL,
        "redemptions.csv": _select(
            (
                "txn_id",
                "member_key",
                "feed_date",
                "txn_date",
                "partner",
                "miles_redeemed",
                "status",
                "dq_txn_after_feed",
                "file_name",
            ),
            "core.redemption_txn",
            "txn_id",
        ),
        "redemption_summary.csv": _select(
            (
                "member_key",
                "member_id",
                "member_matched",
                "member_name",
                "current_country_code",
                "total_completed_miles",
                "pending_miles",
                "txn_count",
                "last_redemption_date",
            ),
            "mart.member_redemption_summary",
            "member_matched, member_key",
        ),
    }
    country_tables = con.execute(
        "SELECT lower(table_name) FROM information_schema.tables WHERE lower(table_schema) = 'mart' "
        "AND lower(table_name) LIKE 'members\\_%' ESCAPE '\\' ORDER BY 1"
    ).fetchall()
    for (table,) in country_tables:
        queries[f"{table}.csv"] = members.format(table=f"mart.{table}")
    return queries


def export_csv(db_path: str | Path, out_dir: str | Path) -> list[Path]:
    """Write the CSV files into out_dir (created if missing, files overwritten). Raises
    FileNotFoundError, before creating anything, if the database does not exist."""
    db_path, out_dir = Path(db_path), Path(out_dir)
    if not db_path.is_file():
        raise FileNotFoundError(f"database not found: {db_path}")
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        out_dir.mkdir(parents=True, exist_ok=True)
        written = []
        for name, query in _queries(con).items():
            target = out_dir / name
            quoted = target.as_posix().replace("'", "''")
            con.execute(f"COPY ({query}) TO '{quoted}' (FORMAT CSV, HEADER, DELIMITER ',')")
            written.append(target)
        return written
    finally:
        con.close()
