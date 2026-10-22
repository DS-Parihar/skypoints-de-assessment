"""Pure derivation functions: date parsing, age and staleness.

No function reads the clock; `as_of` is always an explicit argument with no default.
"""

from __future__ import annotations

import re
from dataclasses import dataclass
from datetime import date
from enum import Enum


class ParseStatus(Enum):
    OK = "OK"
    NULL = "NULL"
    INVALID = "INVALID"
    AMBIGUOUS = "AMBIGUOUS"


@dataclass(frozen=True)
class ParseResult:
    value: date | None
    status: ParseStatus
    candidates: tuple[date, ...] = ()


_NULL_MARKER = "NULL"
_EIGHT_DIGITS = re.compile(r"\d{8}")
_DASHED = re.compile(r"(\d{4})-(\d{2})-(\d{2})")
_SLASHED = re.compile(r"(\d{1,2})/(\d{1,2})/(\d{4})")
_NO_SEPARATORS = re.compile(r"\d{7,8}")
SUPPORTED_FORMATS = ("YYYYMMDD", "MMDDYYYY", "YYYY-MM-DD", "M/D/YYYY", "MDYYYY")


def _make_date(year: int, month: int, day: int) -> date | None:
    try:
        return date(year, month, day)
    except ValueError:
        return None


def _result(candidates: list[date | None]) -> ParseResult:
    valid = tuple(sorted({d for d in candidates if d is not None}))
    if not valid:
        return ParseResult(None, ParseStatus.INVALID)
    if len(valid) > 1:
        return ParseResult(None, ParseStatus.AMBIGUOUS, valid)
    return ParseResult(valid[0], ParseStatus.OK, valid)


def _readings(text: str, fmt: str) -> list[date | None] | None:
    """Every date the text can mean under `fmt`; None when the text has the wrong shape."""
    if fmt == "YYYYMMDD" and _EIGHT_DIGITS.fullmatch(text):
        return [_make_date(int(text[:4]), int(text[4:6]), int(text[6:]))]
    if fmt == "MMDDYYYY" and _EIGHT_DIGITS.fullmatch(text):
        return [_make_date(int(text[4:]), int(text[:2]), int(text[2:4]))]
    if fmt == "YYYY-MM-DD" and (m := _DASHED.fullmatch(text)):
        return [_make_date(int(m[1]), int(m[2]), int(m[3]))]
    if fmt == "M/D/YYYY" and (m := _SLASHED.fullmatch(text)):
        return [_make_date(int(m[3]), int(m[1]), int(m[2]))]
    if fmt == "MDYYYY" and _NO_SEPARATORS.fullmatch(text):
        year, month_day = int(text[-4:]), text[:-4]
        if len(month_day) == 4:
            splits = [(month_day[:2], month_day[2:])]
        else:  # 3 digits: 1-digit month + 2-digit day, or 2-digit month + 1-digit day
            splits = [(month_day[:1], month_day[1:]), (month_day[:2], month_day[2:])]
        return [_make_date(year, int(m), int(d)) for m, d in splits]
    return None


def parse_date(value: str | None, fmt: str) -> ParseResult:
    """Parse `value` as `fmt`. The status says why there is no date: NULL, INVALID or AMBIGUOUS."""
    if fmt not in SUPPORTED_FORMATS:
        raise ValueError(f"unsupported date format {fmt!r}; use one of {SUPPORTED_FORMATS}")
    text = (value or "").strip()
    if not text or text.upper() == _NULL_MARKER:
        return ParseResult(None, ParseStatus.NULL)
    readings = _readings(text, fmt)
    return ParseResult(None, ParseStatus.INVALID) if readings is None else _result(readings)


def compute_age(dob: date | None, as_of: date) -> int | None:
    """Whole years completed at `as_of`. A 29 February birthday counts as 1 March in other years."""
    if dob is None or dob > as_of:
        return None
    before_birthday = (as_of.month, as_of.day) < (dob.month, dob.day)
    return as_of.year - dob.year - before_birthday


def is_stale(last_flight_date: date | None, as_of: date, threshold_days: int = 90) -> bool | None:
    """True if the last flight is MORE than `threshold_days` before `as_of`.

    None (unknown) when there is no flight date or it is after `as_of` (bad data).
    """
    if last_flight_date is None or last_flight_date > as_of:
        return None
    return (as_of - last_flight_date).days > threshold_days
