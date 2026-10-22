"""Applies the quality rules to a batch: the rules (SQL views) decide, this module applies.

Flow of run_quality:
  1. load config, run parameters (explicit as_of), the batch's rows from STAGING.MEMBER_PREPARED_V
     and the file headers into the DQ work tables;
  2. 20_apply_validations.sql   quarantine REJECT rows, stage the accepted rows
  3. 21_reconcile_counts.sql    raw vs staging counts for DQ015 / DQ016
  4. 22_record_findings.sql     DQ.ROW_FINDING and DQ.RUN_SUMMARY
Raw is never touched.
"""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from datetime import date

import duckdb

from skypoints.db import DEFAULT_DDL_DIR
from skypoints.quality import engine
from skypoints.quality.model import Finding, QualityConfig, RunSummary

TRANSFORM_DIR = DEFAULT_DDL_DIR / "transform"
_LOAD_INPUT = (
    "INSERT INTO dq.rule_input (batch_id, file_name, file_row_number, file_ts, source_system, "
    "member_key, member_name, member_id, enrollment_date, last_flight_date, date_of_birth, "
    "tier_code, agent_name, state, country, post_code, active_member, raw_payload) "
    "SELECT batch_id, file_name, file_row_number, file_ts, source_system, member_key, member_name, "
    "member_id, enrollment_date_txt, last_flight_date_txt, date_of_birth_txt, tier_code, "
    "agent_name, state, country, post_code_txt, active_member, raw_payload "
    "FROM staging.member_prepared_v WHERE batch_id = ?"
)


def _script(name: str) -> str:
    return (TRANSFORM_DIR / name).read_text(encoding="utf-8")


def run_quality(
    con: duckdb.DuckDBPyConnection,
    batch_id: str,
    as_of: date,
    headers: Mapping[str, Sequence[str]] | None = None,
    config: QualityConfig | None = None,
) -> RunSummary:
    """Validate one raw batch. `headers` maps file_name -> header column names (for DQ014).
    Re-running a batch replaces its findings and summary and adds no duplicate quarantine
    or staging rows."""
    engine.clear_work_tables(con)
    engine.load_config(con, config or QualityConfig(), as_of, batch_id)
    con.execute(_LOAD_INPUT, [batch_id])
    if headers:
        engine.load_headers(con, headers, batch_id)

    engine.materialize(con)
    con.execute(_script("20_apply_validations.sql"))
    con.execute(_script("21_reconcile_counts.sql"))
    con.execute(_script("22_record_findings.sql"))
    return _summary(con, batch_id)


def reconcile(con: duckdb.DuckDBPyConnection, batch_id: str) -> list[Finding]:
    """Compare raw with staging for a batch that run_quality has processed (DQ015, DQ016)."""
    loaded = con.execute("SELECT batch_id FROM dq.rule_param").fetchall()
    if loaded != [(batch_id,)]:
        raise ValueError(f"run_quality has not processed batch {batch_id!r} in this session")
    con.execute(_script("21_reconcile_counts.sql"))
    return engine.fetch_findings(con, "dq.v_run_findings", rule_codes=["DQ015", "DQ016"])


def _summary(con: duckdb.DuckDBPyConnection, batch_id: str) -> RunSummary:
    def scalar(sql: str, params: list | None = None) -> int:
        return con.execute(sql, params or []).fetchone()[0]

    by_rule = dict(
        con.execute(
            "SELECT rule_code, failed_count FROM dq.run_summary WHERE batch_id = ?", [batch_id]
        ).fetchall()
    )
    failed_files = tuple(
        row[0] for row in con.execute("SELECT file_name FROM dq.v_failed_file ORDER BY 1").fetchall()
    )
    return RunSummary(
        batch_id=batch_id,
        rows_read=scalar("SELECT count(*) FROM dq.rule_input"),
        rows_staged=scalar("SELECT count(*) FROM staging.member_stg WHERE batch_id = ?", [batch_id]),
        rows_rejected=scalar("SELECT count(*) FROM dq.v_row_reject"),
        findings_by_rule=by_rule,
        failed_files=failed_files,
        promotion_blocked=scalar(
            "SELECT count(*) FROM dq.row_finding WHERE batch_id = ? AND severity = 'FAIL_BATCH'",
            [batch_id],
        )
        > 0,
    )
