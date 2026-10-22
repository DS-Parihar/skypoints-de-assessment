"""Data containers for the quality framework (no behaviour)."""

from __future__ import annotations

from collections.abc import Mapping, Sequence
from dataclasses import dataclass, field
from datetime import date
from enum import Enum


class Severity(Enum):
    REJECT = "REJECT"  # row goes to DQ.QUARANTINE and stays out of staging
    WARN = "WARN"  # row continues; recorded in DQ.ROW_FINDING
    ROUTE = "ROUTE"  # row continues to the unassigned table; recorded in DQ.ROW_FINDING
    FAIL_FILE = "FAIL_FILE"  # stops only that file
    FAIL_BATCH = "FAIL_BATCH"  # blocks promotion from staging to CORE; raw is untouched


SEED_COUNTRY_MAP = {
    "USA": "USA", "US": "USA", "IND": "IND", "IN": "IND", "CAN": "CAN", "CA": "CAN",
    "PHIL": "PHL", "PHL": "PHL", "PH": "PHL", "AUS": "AUS", "AU": "AUS",
}  # fmt: skip
SKYPOINTS_DATE_FORMATS = {
    "enrollment_date": "YYYYMMDD",
    "last_flight_date": "YYYYMMDD",
    "date_of_birth": "MMDDYYYY",
}


@dataclass(frozen=True)
class QualityConfig:
    """Thresholds and lookups. Config lives here, not inside the rules."""

    expected_member_id_length: int = 6
    tiers: frozenset[str] = frozenset({"GLD", "SLV", "PLT"})
    active_values: frozenset[str] = frozenset({"A", "I"})
    max_age: int = 120
    country_map: Mapping[str, str] = field(default_factory=lambda: dict(SEED_COUNTRY_MAP))
    max_lengths: Mapping[str, int] = field(
        default_factory=lambda: {
            "member_name": 255,
            "member_id": 18,
            "tier_code": 5,
            "agent_name": 255,
            "state": 5,
            "country": 5,
            "post_code": 5,
            "active_member": 1,
        }
    )
    # source_system -> {date column -> format name understood by derive.parse_date}
    date_formats: Mapping[str, Mapping[str, str]] = field(
        default_factory=lambda: {
            "SKYPOINTS": dict(SKYPOINTS_DATE_FORMATS),
            "IND": dict.fromkeys(SKYPOINTS_DATE_FORMATS, "M/D/YYYY"),
            "USA": dict.fromkeys(SKYPOINTS_DATE_FORMATS, "MDYYYY"),
            "AUS": dict.fromkeys(SKYPOINTS_DATE_FORMATS, "YYYY-MM-DD"),
        }
    )


@dataclass(frozen=True)
class PreparedRow:
    """One row as read from STAGING.MEMBER_PREPARED_V: text cleaned (blank -> None)."""

    batch_id: str
    file_name: str
    file_row_number: int
    source_system: str | None = "SKYPOINTS"
    member_key: str | None = None
    member_name: str | None = None
    member_id: str | None = None
    enrollment_date: str | None = None  # text; the rules parse it
    last_flight_date: str | None = None
    date_of_birth: str | None = None
    tier_code: str | None = None
    agent_name: str | None = None
    state: str | None = None
    country: str | None = None
    post_code: str | None = None
    active_member: str | None = None
    raw: Mapping[str, str | None] = field(default_factory=dict)  # original raw values


@dataclass(frozen=True)
class HeaderInfo:
    file_name: str
    columns: Sequence[str]  # header names exactly as in the file


@dataclass(frozen=True)
class BatchCounts:
    raw_rows: int  # detail rows in raw for the batch
    staged_rows: int
    rejected_rows: int
    # non-blank values per text column, over the raw rows that were not rejected / over staging
    raw_nonblank: Mapping[str, int] = field(default_factory=dict)
    staged_nonblank: Mapping[str, int] = field(default_factory=dict)


@dataclass(frozen=True)
class RuleContext:
    rows: Sequence[PreparedRow]
    as_of: date  # explicit, no default
    config: QualityConfig = field(default_factory=QualityConfig)
    header: HeaderInfo | None = None
    counts: BatchCounts | None = None


@dataclass(frozen=True)
class Finding:
    """One failing row (or file, or batch) with the reason."""

    rule_code: str
    severity: Severity
    reason: str
    batch_id: str | None = None
    file_name: str | None = None
    file_row_number: int | None = None  # None for file- and batch-level findings
    member_key: str | None = None
    column_name: str | None = None
    value: str | None = None


@dataclass(frozen=True)
class RunSummary:
    batch_id: str
    rows_read: int
    rows_staged: int
    rows_rejected: int
    findings_by_rule: Mapping[str, int]  # rule_code -> failed_count (only rules that fired)
    failed_files: tuple[str, ...]
    promotion_blocked: bool  # True when a FAIL_BATCH rule fired
