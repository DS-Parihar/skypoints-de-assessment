"""Adapters mapping the IND/USA/AUS extracts to one canonical layout.

Values stay raw strings. A column the source does not have is None (absent), as opposed to ''
(present but blank). member_key = source_system + ':' + member_id (ADR 0002).
"""

from __future__ import annotations

import csv
from collections.abc import Iterable
from pathlib import Path
from typing import Any

import openpyxl

CANONICAL_FIELDS = (
    "member_id",
    "member_name",
    "tier_code",
    "date_of_birth",
    "enrollment_date",
    "last_flight_date",
    "individual_or_corporate",
)

# source header -> canonical field
IND_COLUMNS = {
    "ID": "member_id",
    "Name": "member_name",
    "DOB": "date_of_birth",
    "TierCode": "tier_code",
    "EnrollmentDate": "enrollment_date",
    "Individual or Corporate": "individual_or_corporate",
    "Flight Date": "last_flight_date",
}
USA_COLUMNS = {
    "ID": "member_id",
    "Name": "member_name",
    "TierCode": "tier_code",
    "EnrollmentDate": "enrollment_date",
    "FlightDate": "last_flight_date",
}
AUS_COLUMNS = {
    "Unique ID": "member_id",
    "Member Name": "member_name",
    "Tier Type": "tier_code",
    "Date of Birth": "date_of_birth",
    "Date of Enrollment": "enrollment_date",
    "Date of Flight": "last_flight_date",
}


def _text(value: Any) -> str:
    if value is None:
        return ""
    if isinstance(value, float) and value.is_integer():
        return str(int(value))  # Excel numbers: 1.0 -> '1'
    return str(value)


def _canonical(
    rows: Iterable[list[str]], columns: dict[str, str], source_system: str, file_name: str
) -> list[dict[str, str | None]]:
    iterator = iter(rows)
    header = next(iterator, [])
    missing = [name for name in columns if name not in header]
    if missing:
        raise ValueError(f"{file_name}: header is missing column(s): {', '.join(missing)}")
    index = {columns[name]: header.index(name) for name in columns}
    records: list[dict[str, str | None]] = []
    for row in iterator:
        if not any(cell.strip() for cell in row):
            continue
        record: dict[str, str | None] = {"source_system": source_system, "member_key": None}
        for field in CANONICAL_FIELDS:
            if field not in index:
                record[field] = None  # the source has no such column
            else:
                record[field] = row[index[field]] if index[field] < len(row) else ""
        record["member_key"] = f"{source_system}:{record['member_id']}"
        records.append(record)
    return records


def _read_csv(path: str | Path, columns: dict[str, str], source_system: str):
    path = Path(path)
    with path.open(newline="", encoding="utf-8-sig") as handle:
        return _canonical(list(csv.reader(handle)), columns, source_system, path.name)


def read_ind_csv(path: str | Path) -> list[dict[str, str | None]]:
    return _read_csv(path, IND_COLUMNS, "IND")


def read_usa_csv(path: str | Path) -> list[dict[str, str | None]]:
    return _read_csv(path, USA_COLUMNS, "USA")


def read_aus_xlsx(path: str | Path) -> list[dict[str, str | None]]:
    path = Path(path)
    workbook = openpyxl.load_workbook(path, read_only=True, data_only=True)
    try:
        sheet = workbook.worksheets[0]
        rows = [[_text(cell) for cell in row] for row in sheet.iter_rows(values_only=True)]
    finally:
        workbook.close()
    return _canonical(rows, AUS_COLUMNS, "AUS", path.name)
