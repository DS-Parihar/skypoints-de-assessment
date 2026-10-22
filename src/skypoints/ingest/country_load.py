"""Load a country extract (IND.csv, USA.csv, AUS.xlsx) into RAW.MEMBER_FEED_RAW.

The adapters in country_extracts.py map each file to the canonical columns; the source manifest
(skypoints.config) gives the source system, the country, and the fixed file timestamp (the
extracts carry none). Values stay raw text, exactly as the adapter returned them. Same rules as
the member feed loader: all-or-nothing, and a file name already in raw is skipped.

Not stored: IND's "Individual or Corporate" column. It is outside the member spec and the raw
table has no column for it.
"""

from __future__ import annotations

from collections.abc import Callable
from pathlib import Path

import duckdb

from skypoints.config import SourceEntry
from skypoints.db import atomic
from skypoints.ingest.country_extracts import read_aus_xlsx, read_ind_csv, read_usa_csv
from skypoints.ingest.loader import LoadResult, insert_raw_rows

_READERS: dict[str, Callable[[str | Path], list[dict[str, str | None]]]] = {
    "IND": read_ind_csv,
    "USA": read_usa_csv,
    "AUS": read_aus_xlsx,
}


def _text(value: str | None) -> str:
    return "" if value is None else value  # a column the source does not have is blank in raw


def load_country_extract(
    con: duckdb.DuckDBPyConnection, path: str | Path, entry: SourceEntry, batch_id: str
) -> LoadResult:
    """Read the extract with its adapter and insert it into raw. Raises ValueError for a header
    that does not match, and inserts nothing."""
    path = Path(path)
    reader = _READERS.get(entry.source_system)
    if reader is None:
        raise ValueError(f"no adapter for source system {entry.source_system!r}")
    records = reader(path)  # validates the header
    already = con.execute(
        "SELECT count(*) FROM raw.member_feed_raw WHERE file_name = ?", [path.name]
    ).fetchone()[0]
    if already:
        return LoadResult(path.name, 0, skipped=True)

    rows = []
    for line, record in enumerate(records, start=2):  # line 1 is the header
        member_id = _text(record["member_id"])
        member_key = f"{entry.source_system}:{member_id.strip()}" if member_id.strip() else None
        rows.append(
            ("D", _text(record["member_name"]), member_id, _text(record["enrollment_date"]),
             _text(record["last_flight_date"]), _text(record["tier_code"]), "", "",
             entry.country_code or "", "", _text(record["date_of_birth"]), "",
             entry.source_system, member_key, path.name, line, entry.file_ts, batch_id)
        )  # fmt: skip
    if rows:
        with atomic(con):
            insert_raw_rows(con, rows)
    return LoadResult(path.name, len(rows))
