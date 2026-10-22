"""Loads the SQL rule engine (sql/duckdb/05_validations.sql) and reads its findings.

The rules themselves are SQL views, so the same logic ports to Snowflake. This module only
fills the work and config tables from Python objects and turns result rows into Finding objects.
"""

from __future__ import annotations

import json
from collections.abc import Mapping, Sequence

import duckdb

from skypoints.db import DEFAULT_DDL_DIR
from skypoints.quality.model import (
    BatchCounts,
    Finding,
    HeaderInfo,
    PreparedRow,
    QualityConfig,
    RuleContext,
    Severity,
)

VALIDATIONS_SQL = DEFAULT_DDL_DIR / "05_validations.sql"
FINDING_SELECT = (
    "SELECT rule_code, severity, reason, batch_id, file_name, file_row_number, member_key, "
    "column_name, value FROM {view}"
)
_WORK_TABLES = (
    "rule_param", "rule_input", "rule_header", "rule_counts", "rule_column_counts",
    "work_finding", "work_date",
)
_scratch_connection: duckdb.DuckDBPyConnection | None = None


def scratch_connection() -> duckdb.DuckDBPyConnection:
    """An in-memory database holding only the rule engine; reused by the unit-level rule calls."""
    global _scratch_connection
    if _scratch_connection is None:
        connection = duckdb.connect()
        connection.execute(VALIDATIONS_SQL.read_text(encoding="utf-8"))
        _scratch_connection = connection
    return _scratch_connection


def _many(con: duckdb.DuckDBPyConnection, sql: str, rows: Sequence[Sequence]) -> None:
    """Run `INSERT INTO t [(cols)] VALUES (?, ...)` for every row as ONE multi-row statement.

    DuckDB's executemany runs one statement per row (about 50 ms each with a primary key), and
    rejects an empty list; a single multi-row INSERT is fast and an empty list is a no-op.
    """
    if not rows:
        return
    head, _, tuple_sql = sql.partition("VALUES")
    con.execute(
        head + "VALUES " + ", ".join(tuple_sql.strip() for _ in rows),
        [value for row in rows for value in row],
    )


def clear_work_tables(con: duckdb.DuckDBPyConnection) -> None:
    for table in _WORK_TABLES:
        con.execute(f"DELETE FROM dq.{table}")


def load_config(
    con: duckdb.DuckDBPyConnection,
    config: QualityConfig,
    as_of,
    batch_id: str | None,
) -> None:
    """Config tables from QualityConfig and the one-row run parameters (explicit as_of)."""
    for table in ("cfg_tier", "cfg_active", "cfg_country_map", "cfg_max_length", "cfg_date_format"):
        con.execute(f"DELETE FROM dq.{table}")
    _many(con, "INSERT INTO dq.cfg_tier VALUES (?)", [(t,) for t in config.tiers])
    _many(con, "INSERT INTO dq.cfg_active VALUES (?)", [(v,) for v in config.active_values])
    _many(con, 
        "INSERT INTO dq.cfg_country_map VALUES (?, ?)",
        [(alias.upper(), iso3) for alias, iso3 in config.country_map.items()],
    )
    _many(con, 
        "INSERT INTO dq.cfg_max_length VALUES (?, ?)", list(config.max_lengths.items())
    )
    _many(con, 
        "INSERT INTO dq.cfg_date_format VALUES (?, ?, ?)",
        [
            (source, column, fmt)
            for source, columns in config.date_formats.items()
            for column, fmt in columns.items()
        ],
    )
    con.execute("DELETE FROM dq.rule_param")
    con.execute(
        "INSERT INTO dq.rule_param VALUES (?, ?, ?, ?)",
        [batch_id, as_of, config.expected_member_id_length, config.max_age],
    )


def load_rows(con: duckdb.DuckDBPyConnection, rows: Sequence[PreparedRow]) -> None:
    _many(con, 
        "INSERT INTO dq.rule_input (batch_id, file_name, file_row_number, source_system, member_key, "
        "member_name, member_id, enrollment_date, last_flight_date, date_of_birth, tier_code, "
        "agent_name, state, country, post_code, active_member, raw_payload) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
        [
            (
                r.batch_id, r.file_name, r.file_row_number, r.source_system, r.member_key,
                r.member_name, r.member_id, r.enrollment_date, r.last_flight_date,
                r.date_of_birth, r.tier_code, r.agent_name, r.state, r.country, r.post_code,
                r.active_member, json.dumps(dict(r.raw)) if r.raw else None,
            )
            for r in rows
        ],
    )  # fmt: skip


def load_headers(
    con: duckdb.DuckDBPyConnection, headers: Mapping[str, Sequence[str]], batch_id: str | None
) -> None:
    _many(con, 
        "INSERT INTO dq.rule_header VALUES (?, ?, ?)",
        [(batch_id, file_name, column) for file_name, columns in headers.items() for column in columns],
    )


def load_counts(con: duckdb.DuckDBPyConnection, counts: BatchCounts, batch_id: str | None) -> None:
    con.execute(
        "INSERT INTO dq.rule_counts VALUES (?, ?, ?, ?)",
        [batch_id, counts.raw_rows, counts.staged_rows, counts.rejected_rows],
    )
    columns = set(counts.raw_nonblank) | set(counts.staged_nonblank)
    _many(con, 
        "INSERT INTO dq.rule_column_counts VALUES (?, ?, ?, ?)",
        [
            (batch_id, c, counts.raw_nonblank.get(c, 0), counts.staged_nonblank.get(c, 0))
            for c in sorted(columns)
        ],
    )


def materialize(con: duckdb.DuckDBPyConnection) -> None:
    """Evaluate the row-level rules once into DQ.WORK_FINDING / DQ.WORK_DATE for the decision views."""
    con.execute("DELETE FROM dq.work_date")
    con.execute("INSERT INTO dq.work_date SELECT * FROM dq.v_rule_date")
    con.execute("DELETE FROM dq.work_finding")
    con.execute("INSERT INTO dq.work_finding SELECT * FROM dq.v_row_findings")


def load_context(con: duckdb.DuckDBPyConnection, ctx: RuleContext) -> None:
    """Replace the work tables with the contents of a RuleContext (unit-level use)."""
    clear_work_tables(con)
    batch_id = ctx.rows[0].batch_id if ctx.rows else None
    load_config(con, ctx.config, ctx.as_of, batch_id)
    load_rows(con, ctx.rows)
    header: HeaderInfo | None = ctx.header
    if header is not None:
        load_headers(con, {header.file_name: list(header.columns)}, batch_id)
    if ctx.counts is not None:
        load_counts(con, ctx.counts, batch_id)


def fetch_findings(
    con: duckdb.DuckDBPyConnection, view: str, rule_codes: Sequence[str] | None = None
) -> list[Finding]:
    sql = FINDING_SELECT.format(view=view)
    params: list[str] = []
    if rule_codes is not None:
        sql += " WHERE rule_code IN (" + ", ".join("?" for _ in rule_codes) + ")"
        params = list(rule_codes)
    sql += " ORDER BY rule_code, file_name, file_row_number, column_name"
    return [
        Finding(
            rule_code=code, severity=Severity[severity], reason=reason, batch_id=batch_id,
            file_name=file_name, file_row_number=row_number, member_key=member_key,
            column_name=column, value=value,
        )
        for code, severity, reason, batch_id, file_name, row_number, member_key, column, value
        in con.execute(sql, params).fetchall()
    ]  # fmt: skip
