"""Load a parsed member feed file into RAW.MEMBER_FEED_RAW (DuckDB).

One raw row per line: the header as record_type 'H' (on its own line number) and every detail as
'D', all values as text exactly as parsed ('' stays '', not NULL). source_system is 'SKYPOINTS'
and member_key is 'SKYPOINTS:' || member_id (NULL for the header row and for a blank id). The
load is all-or-nothing, and a file name already in raw is skipped, so raw stays append-only.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from pathlib import Path

import duckdb

from skypoints.db import atomic
from skypoints.ingest.flat_file import COLUMNS, FeedWarning, parse_file_ts, parse_member_feed

SOURCE_SYSTEM = "SKYPOINTS"
_INSERT = (
    "INSERT INTO raw.member_feed_raw (record_type, member_name, member_id, enrollment_date, "
    "last_flight_date, tier_code, agent_name, state, country, post_code, date_of_birth, "
    "active_member, source_system, member_key, file_name, file_row_number, file_ts, batch_id) VALUES "
)
_ROW = "(" + ", ".join("?" for _ in range(18)) + ")"
_BUSINESS_KEYS = tuple(COLUMNS.values())  # canonical keys in spec order


@dataclass(frozen=True)
class LoadResult:
    file_name: str
    rows_loaded: int
    skipped: bool = False  # the file name was already in raw
    warnings: list[FeedWarning] = field(default_factory=list)


def insert_raw_rows(con: duckdb.DuckDBPyConnection, rows: list[tuple]) -> None:
    """One multi-row INSERT into RAW.MEMBER_FEED_RAW; each row is the 18 columns in the order
    (record_type, 11 business columns, source_system, member_key, file_name, file_row_number,
    file_ts, batch_id)."""
    con.execute(_INSERT + ", ".join(_ROW for _ in rows), [value for row in rows for value in row])


def _header_line_number(path: Path) -> int:
    for number, line in enumerate(path.read_text(encoding="utf-8-sig").splitlines(), start=1):
        if line.startswith("|H|"):
            return number
    return 1


def load_member_feed(con: duckdb.DuckDBPyConnection, path: str | Path, batch_id: str) -> LoadResult:
    """Parse `path` and insert it into RAW.MEMBER_FEED_RAW. Raises (and inserts nothing) for an
    invalid file name or a header that does not match the layout."""
    path = Path(path)
    header, records, warnings = parse_member_feed(path)  # validates the name and the header first
    already = con.execute(
        "SELECT count(*) FROM raw.member_feed_raw WHERE file_name = ?", [path.name]
    ).fetchone()[0]
    if already:
        return LoadResult(path.name, 0, skipped=True)

    file_ts = parse_file_ts(path.name)
    header_values = {key: name for name, key in COLUMNS.items() if name in header}
    rows: list[tuple] = [
        ("H", *(header_values.get(key, "") for key in _BUSINESS_KEYS), SOURCE_SYSTEM, None,
         path.name, _header_line_number(path), file_ts, batch_id)
    ]  # fmt: skip
    for record in records:
        member_id = record["member_id"]
        member_key = f"{SOURCE_SYSTEM}:{member_id.strip()}" if member_id.strip() else None
        rows.append(
            ("D", *(record[key] for key in _BUSINESS_KEYS), SOURCE_SYSTEM, member_key,
             record["file_name"], record["file_row_number"], record["file_ts"], batch_id)
        )  # fmt: skip

    with atomic(con):  # all or nothing: one multi-row INSERT
        insert_raw_rows(con, rows)
    return LoadResult(path.name, len(rows), warnings=warnings)
