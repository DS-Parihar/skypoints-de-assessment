"""Parser for the pipe-delimited member feed (SKYPOINTS_MEMBERS_YYYYMMDD_HHMMSSTT.dat).

Lines start with a pipe: ``|H|<column names>`` for the header and ``|D|<values>`` for details.
Columns are mapped by header NAME, so order does not matter. Every value stays a string.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any

FILE_NAME_PATTERN = re.compile(r"SKYPOINTS_MEMBERS_(\d{8})_(\d{2})(\d{2})(\d{2})(\d{2})\.dat")

# header name -> canonical record key, in spec order
COLUMNS: dict[str, str] = {
    "Member_Name": "member_name",
    "Member_Id": "member_id",
    "Enrollment_Date": "enrollment_date",
    "Last_Flight_Date": "last_flight_date",
    "Tier_Code": "tier_code",
    "Agent_Name": "agent_name",
    "State": "state",
    "Country": "country",
    "Post_Code": "post_code",
    "DOB": "date_of_birth",
    "Is_Active": "active_member",
}
MANDATORY = ("Member_Name", "Member_Id", "Enrollment_Date")


@dataclass(frozen=True)
class FeedWarning:
    """A non-fatal finding about a feed file."""

    code: str  # e.g. MISSING_OPTIONAL_COLUMN
    column: str  # the header column concerned
    message: str


class HeaderMismatchError(ValueError):
    """The header does not match the expected layout."""

    def __init__(
        self,
        message: str,
        missing: list[str] | None = None,
        unexpected: list[str] | None = None,
        duplicates: list[str] | None = None,
    ) -> None:
        super().__init__(message)
        self.missing = missing or []
        self.unexpected = unexpected or []
        self.duplicates = duplicates or []


class InvalidFileNameError(ValueError):
    """The file name does not match SKYPOINTS_MEMBERS_YYYYMMDD_HHMMSSTT.dat."""


def parse_feed_file_ts(file_name: str, feed: str, extension: str) -> datetime:
    """Return the timestamp in SKYPOINTS_<feed>_YYYYMMDD_HHMMSSTT.<extension>; TT is hundredths
    of a second. Raises InvalidFileNameError for any other name."""
    pattern = re.compile(rf"SKYPOINTS_{feed}_(\d{{8}})_(\d{{2}})(\d{{2}})(\d{{2}})(\d{{2}})\.{extension}")
    match = pattern.fullmatch(file_name)
    if match is None:
        raise InvalidFileNameError(
            f"invalid {feed.lower()} feed file name: {file_name!r} "
            f"(expected SKYPOINTS_{feed}_YYYYMMDD_HHMMSSTT.{extension})"
        )
    day, hour, minute, second, hundredths = match.groups()
    try:
        moment = datetime.strptime(f"{day}{hour}{minute}{second}", "%Y%m%d%H%M%S")
    except ValueError as exc:
        raise InvalidFileNameError(f"invalid timestamp in file name {file_name!r}: {exc}") from exc
    return moment.replace(microsecond=int(hundredths) * 10_000)


def parse_file_ts(file_name: str) -> datetime:
    """Timestamp of a member feed file name (SKYPOINTS_MEMBERS_YYYYMMDD_HHMMSSTT.dat)."""
    return parse_feed_file_ts(file_name, "MEMBERS", "dat")


def _check_header(names: list[str], file_name: str) -> list[FeedWarning]:
    duplicates = list(dict.fromkeys(n for n in names if names.count(n) > 1))
    unexpected = [n for n in dict.fromkeys(names) if n not in COLUMNS]
    missing = [n for n in MANDATORY if n not in names]
    if missing or unexpected or duplicates:
        parts = [
            f"{label}: {', '.join(found)}"
            for label, found in (
                ("missing mandatory", missing),
                ("unexpected", unexpected),
                ("duplicate", duplicates),
            )
            if found
        ]
        raise HeaderMismatchError(
            f"{file_name}: header mismatch ({'; '.join(parts)})", missing, unexpected, duplicates
        )
    return [
        FeedWarning(
            "MISSING_OPTIONAL_COLUMN",
            column,
            f"{file_name}: optional column {column} is missing; its values are set to ''",
        )
        for column in COLUMNS
        if column not in names
    ]


def parse_member_feed(
    path: str | Path,
) -> tuple[list[str], list[dict[str, Any]], list[FeedWarning]]:
    """Parse a member feed file into (header, detail records, warnings)."""
    path = Path(path)
    file_ts = parse_file_ts(path.name)  # reject a bad name before opening the file
    lines = path.read_text(encoding="utf-8-sig").splitlines()

    first = next((i for i, line in enumerate(lines) if line.strip()), None)
    if first is None or not lines[first].startswith("|H|"):
        raise HeaderMismatchError(
            f"{path.name}: no header row (|H|) found", missing=list(MANDATORY)
        )
    header = lines[first].split("|")[2:]
    warnings = _check_header(header, path.name)

    records: list[dict[str, Any]] = []
    for number, line in enumerate(lines[first + 1 :], start=first + 2):
        if not line.strip():
            continue
        tokens = line.split("|")
        if len(tokens) < 2 or tokens[0] != "" or tokens[1] != "D":
            raise ValueError(f"{path.name} line {number}: expected a |D| detail row")
        values = tokens[2:]
        if len(values) > len(header):
            raise ValueError(
                f"{path.name} line {number}: {len(values)} values for {len(header)} header columns"
            )
        by_name = dict(zip(header, values))
        record: dict[str, Any] = {key: by_name.get(name, "") for name, key in COLUMNS.items()}
        record.update(file_name=path.name, file_row_number=number, file_ts=file_ts)
        records.append(record)
    return header, records, warnings
