"""Merge staged members into CORE.MEMBER_CURRENT (latest record wins) and split them into the
per-country MART tables. The logic is SQL (sql/duckdb/transform/30..34_*.sql); this module runs
it in order, applies the guards and turns the results into Python objects.

Rules:
  * a staged record replaces the CORE row only if strictly newer by (file_ts, file_row_number);
  * unassigned = country_code NULL or not active in REF.COUNTRY_CODE_MAP (None in a Move);
  * CORE.MEMBER_COUNTRY_HISTORY is append-only: one row per country change against the previous
    CORE state; a first appearance is not a move; moves inside one batch collapse to one;
  * a batch with an open FAIL_BATCH finding (DQ.ROW_FINDING) is refused;
  * identical duplicates collapse; conflicting ones raise and nothing is written.
"""

from __future__ import annotations

import re
from contextlib import contextmanager
from dataclasses import dataclass, field
from datetime import datetime

import duckdb

from skypoints.db import DEFAULT_DDL_DIR

TRANSFORM_DIR = DEFAULT_DDL_DIR / "transform"
UNASSIGNED_TABLE = "MEMBERS_UNASSIGNED"
_ISO3 = re.compile(r"[A-Z]{3}")


class AmbiguousLatestRecordError(Exception):
    """Two different records share the same (file_ts, file_row_number, member_key)."""


class BatchBlockedError(Exception):
    """The batch has an open FAIL_BATCH finding, so it cannot be promoted to CORE."""


@dataclass(frozen=True)
class Move:
    member_key: str
    from_country: str | None  # ISO-3, or None for unassigned
    to_country: str | None
    moved_at: datetime  # file_ts of the winning record
    batch_id: str  # batch of the winning record


@dataclass(frozen=True)
class MergeResult:
    inserted: int
    updated: int
    stale_ignored: int
    moves: list[Move] = field(default_factory=list)


@dataclass(frozen=True)
class SplitResult:
    rows_per_table: dict[str, int]  # 'MEMBERS_IND' -> row count, for every country table
    moves: list[Move]
    inserted: int
    updated: int
    stale_ignored: int


def _sql(name: str) -> str:
    return (TRANSFORM_DIR / name).read_text(encoding="utf-8")


@contextmanager
def _atomic(con: duckdb.DuckDBPyConnection):
    """One transaction; joins the caller's transaction if one is already open."""
    try:
        con.execute("BEGIN TRANSACTION")
    except duckdb.TransactionException:
        yield
        return
    try:
        yield
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def _check_not_blocked(con: duckdb.DuckDBPyConnection, batch_id: str | None) -> None:
    """Refuse a batch with a FAIL_BATCH finding. Nothing resolves one, so any recorded finding is
    open. With no batch id, every batch present in staging is checked."""
    if batch_id is not None:
        blocked = con.execute(
            "SELECT DISTINCT batch_id FROM dq.row_finding WHERE severity = 'FAIL_BATCH' AND batch_id = ?",
            [batch_id],
        ).fetchall()
    else:
        blocked = con.execute(
            "SELECT DISTINCT batch_id FROM dq.row_finding WHERE severity = 'FAIL_BATCH' "
            "AND batch_id IN (SELECT batch_id FROM staging.member_stg)"
        ).fetchall()
    if blocked:
        raise BatchBlockedError(
            "FAIL_BATCH finding(s) open for batch(es): " + ", ".join(sorted(b for (b,) in blocked))
        )


def _merge(con: duckdb.DuckDBPyConnection, batch_id: str | None) -> MergeResult:
    con.execute(_sql("30_split_candidates.sql"), {"batch_id": batch_id})
    conflicts = con.execute(_sql("31_split_ambiguous.sql")).fetchall()
    if conflicts:
        raise AmbiguousLatestRecordError(
            "different records share the same (file_ts, file_row_number) for: "
            + ", ".join(f"{key} ({n} records)" for key, _, _, n in conflicts)
        )
    con.execute(_sql("32_merge_member_current.sql"))
    counts = dict(con.execute("SELECT action, count(*) FROM _split_decision GROUP BY action").fetchall())
    moves = [
        Move(key, old, new, moved_at, batch)
        for key, old, new, moved_at, batch in con.execute(
            "SELECT member_key, old_country, new_country, file_ts, batch_id FROM _split_decision "
            "WHERE action = 'UPDATE' AND old_country IS DISTINCT FROM new_country "
            "ORDER BY file_ts, member_key"
        ).fetchall()
    ]
    return MergeResult(
        inserted=counts.get("INSERT", 0),
        updated=counts.get("UPDATE", 0),
        stale_ignored=counts.get("STALE", 0),
        moves=moves,
    )


def _country_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    rows = con.execute(
        "SELECT upper(table_name) FROM information_schema.tables "
        "WHERE lower(table_schema) = 'mart' AND upper(table_name) LIKE 'MEMBERS\\_%' ESCAPE '\\' "
        "ORDER BY 1"
    ).fetchall()
    return [name for (name,) in rows]


def _refresh(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    active = [
        code
        for (code,) in con.execute(
            "SELECT DISTINCT country_code FROM ref.country_code_map WHERE is_active ORDER BY 1"
        ).fetchall()
    ]
    for code in active:  # a country added to the map gets its table; no code change
        if not _ISO3.fullmatch(code):
            raise ValueError(f"country_code {code!r} in REF.COUNTRY_CODE_MAP is not an ISO-3 code")
        con.execute(
            f"CREATE TABLE IF NOT EXISTS mart.members_{code.lower()} AS "
            "SELECT * FROM core.member_current WHERE FALSE"
        )
    template = _sql("33_refresh_country_table.sql")
    for table in _country_tables(con):
        if table == UNASSIGNED_TABLE:
            continue
        code = table.removeprefix("MEMBERS_")
        if _ISO3.fullmatch(code):  # an existing table whose country was removed empties out
            con.execute(template.format(table=table), {"country_code": code})
    con.execute(_sql("34_refresh_unassigned.sql"))
    return {
        table: con.execute(f"SELECT count(*) FROM mart.{table}").fetchone()[0]
        for table in _country_tables(con)
    }


def merge_member_current(con: duckdb.DuckDBPyConnection, batch_id: str | None = None) -> MergeResult:
    """STAGING.MEMBER_STG -> CORE.MEMBER_CURRENT. batch_id=None means all staging rows."""
    _check_not_blocked(con, batch_id)
    with _atomic(con):
        return _merge(con, batch_id)


def refresh_country_tables(con: duckdb.DuckDBPyConnection) -> dict[str, int]:
    """CORE.MEMBER_CURRENT -> MART.MEMBERS_<ISO3> (created on demand) and MEMBERS_UNASSIGNED."""
    with _atomic(con):
        return _refresh(con)


def split_by_country(con: duckdb.DuckDBPyConnection, batch_id: str | None = None) -> SplitResult:
    """merge_member_current, then refresh_country_tables, in one transaction."""
    _check_not_blocked(con, batch_id)
    with _atomic(con):
        merged = _merge(con, batch_id)
        rows_per_table = _refresh(con)
    return SplitResult(
        rows_per_table=rows_per_table,
        moves=merged.moves,
        inserted=merged.inserted,
        updated=merged.updated,
        stale_ignored=merged.stale_ignored,
    )
