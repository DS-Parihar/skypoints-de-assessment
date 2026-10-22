"""Flatten the redemption JSON feed into one row per transaction, load it, and promote it.

Feed: SKYPOINTS_REDEMPTIONS_YYYYMMDD_HHMMSSTT.json, a JSON array of member objects:
    {"member_id": "223457", "feed_date": "20240115",
     "redemptions": [{"txn_id": ..., "txn_date": ..., "partner": ..., "miles_redeemed": ..., "status": ...}]}

Layers (same as the member feed): the member object is stored whole in RAW.REDEMPTION_FEED_RAW,
the flatten gives text rows, and the load types and validates them into STAGING.REDEMPTION_STG;
promote_redemptions copies the staged rows to CORE.REDEMPTION_TXN. The logic is SQL in
sql/duckdb/transform/40..47_*.sql (read_json_objects + UNNEST); this module runs it in order.

Rules recorded by the load:
  RD001 WARN    a member with an empty redemptions array (DQ.ROW_FINDING; the member is in raw)
  RD002 REJECT  a duplicate txn_id: only the newest (feed_date, then file_ts, then position) is
                kept; the older one goes to DQ.QUARANTINE with reason_code DUPLICATE_TXN_OLDER
  RD003 REJECT  miles_redeemed is negative            (DQ.QUARANTINE)
  RD004 REJECT  status is not one of the allowed ones (DQ.QUARANTINE)
  RD005 WARN    txn_date is after feed_date           (DQ.ROW_FINDING; the row is still staged)
  RD006 REJECT  a required value is missing or unreadable (DQ.QUARANTINE)
A transaction that breaks several REJECT rules is quarantined once with all codes.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from datetime import datetime
from pathlib import Path
from typing import Any

import duckdb

from skypoints.db import DEFAULT_DDL_DIR, atomic
from skypoints.ingest.flat_file import parse_feed_file_ts

TRANSFORM_DIR = DEFAULT_DDL_DIR / "transform"
_ROW_COLUMNS = (
    "member_id", "feed_date", "txn_id", "txn_date", "partner", "miles_redeemed", "status",
    "file_name", "file_row_number", "txn_seq", "file_ts",
)  # fmt: skip


@dataclass(frozen=True)
class RedemptionConfig:
    statuses: frozenset[str] = frozenset({"COMPLETED", "PENDING"})


@dataclass(frozen=True)
class MemberEntry:
    """One member object of the feed, whether or not it has transactions."""

    member_id: str
    feed_date: str
    redemption_count: int
    file_row_number: int  # 1-based position of the member object in the array


@dataclass(frozen=True)
class FlatFeed:
    file_name: str
    file_ts: datetime
    rows: list[dict[str, Any]] = field(default_factory=list)  # one per transaction, values as text
    members: list[MemberEntry] = field(default_factory=list)


@dataclass(frozen=True)
class RedemptionLoadResult:
    file_name: str
    members_read: int
    rows_flattened: int
    rows_staged: int
    rows_quarantined: int
    empty_members: tuple[str, ...] = ()
    skipped: bool = False  # the file was already loaded


@dataclass(frozen=True)
class PromoteResult:
    inserted: int
    updated: int


def _sql(name: str) -> str:
    return (TRANSFORM_DIR / name).read_text(encoding="utf-8")


def _checked(path: str | Path, file_ts: datetime | None = None) -> tuple[Path, datetime]:
    """Validate the file name first (no file is opened for a bad name), then that the file exists.
    A file_ts given by the caller (from the source manifest) replaces the one in the name."""
    path = Path(path)
    file_ts = file_ts or parse_feed_file_ts(path.name, "REDEMPTIONS", "json")
    if not path.is_file():
        raise FileNotFoundError(path)
    return path, file_ts


def _members(con: duckdb.DuckDBPyConnection) -> list[MemberEntry]:
    return [
        MemberEntry(str(member_id), feed_date, count, row_number)
        for member_id, feed_date, count, row_number in con.execute(
            _sql("43_redemption_members.sql")
        ).fetchall()
    ]


def flatten_redemption_feed(
    path: str | Path, file_ts: datetime | None = None, default_feed_date: str | None = None
) -> FlatFeed:
    """Parse the feed. No typing, no validation: every value is text, file metadata is attached.
    file_ts and default_feed_date come from the source manifest for a file not named like a feed."""
    path, file_ts = _checked(path, file_ts)
    con = duckdb.connect()  # parsing needs no database tables
    try:
        con.execute(
            _sql("40_redemption_member_from_file.sql"),
            {
                "file": str(path),
                "file_name": path.name,
                "file_ts": file_ts,
                "batch_id": None,
                "default_feed_date": default_feed_date,
            },
        )
        con.execute(_sql("42_redemption_flatten.sql"))
        columns = ", ".join(_ROW_COLUMNS)
        rows = [
            dict(zip(_ROW_COLUMNS, row))
            for row in con.execute(
                f"SELECT {columns} FROM _redemption_flat ORDER BY file_row_number, txn_seq"
            ).fetchall()
        ]
        return FlatFeed(file_name=path.name, file_ts=file_ts, rows=rows, members=_members(con))
    finally:
        con.close()


def load_redemption_feed(
    con: duckdb.DuckDBPyConnection,
    path: str | Path,
    batch_id: str,
    config: RedemptionConfig | None = None,
    file_ts: datetime | None = None,
    default_feed_date: str | None = None,
) -> RedemptionLoadResult:
    """Raw -> flatten -> validate -> STAGING.REDEMPTION_STG, applying RD001..RD006. A file name
    that is already in raw is skipped (raw is append-only); the load is all-or-nothing."""
    path, file_ts = _checked(path, file_ts)
    config = config or RedemptionConfig()
    already = con.execute(
        "SELECT count(*) FROM raw.redemption_feed_raw WHERE file_name = ?", [path.name]
    ).fetchone()[0]
    if already:
        return RedemptionLoadResult(path.name, 0, 0, 0, 0, skipped=True)

    with atomic(con):
        con.execute(
            _sql("44_load_redemption_raw.sql"),
            {"file": str(path), "file_name": path.name, "file_ts": file_ts, "batch_id": batch_id},
        )
        con.execute(
            _sql("41_redemption_member_from_raw.sql"),
            {"file_name": path.name, "default_feed_date": default_feed_date},
        )
        con.execute(_sql("42_redemption_flatten.sql"))
        members = _members(con)
        con.execute("CREATE OR REPLACE TEMP TABLE _redemption_status (STATUS VARCHAR)")
        for status in sorted(config.statuses):
            con.execute("INSERT INTO _redemption_status VALUES (?)", [status])
        con.execute(_sql("45_stage_redemptions.sql"))
        flattened = con.execute("SELECT count(*) FROM _redemption_flat").fetchone()[0]
        staged = con.execute(
            "SELECT count(*) FROM _rd_winner WHERE action IN ('INSERT', 'REPLACE')"
        ).fetchone()[0]
        quarantined = con.execute("SELECT count(*) FROM _rd_quarantine").fetchone()[0]
    return RedemptionLoadResult(
        file_name=path.name,
        members_read=len(members),
        rows_flattened=flattened,
        rows_staged=staged,
        rows_quarantined=quarantined,
        empty_members=tuple(m.member_id for m in members if m.redemption_count == 0),
    )


def promote_redemptions(
    con: duckdb.DuckDBPyConnection, batch_id: str | None = None
) -> PromoteResult:
    """STAGING.REDEMPTION_STG -> CORE.REDEMPTION_TXN (batch_id=None: all staged rows). A row
    replaces the CORE row only if strictly newer; re-running changes nothing."""
    with atomic(con):
        con.execute(_sql("46_promote_candidates.sql"), {"batch_id": batch_id})
        con.execute(_sql("47_promote_redemptions.sql"))
        counts = dict(
            con.execute("SELECT action, count(*) FROM _promote_decision GROUP BY action").fetchall()
        )
    return PromoteResult(inserted=counts.get("INSERT", 0), updated=counts.get("UPDATE", 0))
