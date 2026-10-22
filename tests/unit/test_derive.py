"""Unit tests for the pure functions in skypoints.transform.derive.

No test calls date.today(); every date comes from the test itself and as_of is always explicit.
"""

from dataclasses import FrozenInstanceError
from datetime import date

import pytest

from skypoints.transform.derive import (
    ParseResult,
    ParseStatus,
    compute_age,
    is_stale,
    parse_date,
)

ALL_FORMATS = ["YYYYMMDD", "MMDDYYYY", "YYYY-MM-DD", "M/D/YYYY", "MDYYYY"]

# ---- parse_date ----------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("text", "fmt", "expected"),
    [
        pytest.param("03051985", "MMDDYYYY", date(1985, 3, 5), id="MMDDYYYY-5-March"),
        pytest.param("20101012", "YYYYMMDD", date(2010, 10, 12), id="YYYYMMDD"),
        pytest.param("2022-05-11", "YYYY-MM-DD", date(2022, 5, 11), id="iso-dashes"),
        pytest.param("8/13/1982", "M/D/YYYY", date(1982, 8, 13), id="slashes-unpadded-month-first"),
        pytest.param("12/1/1998", "M/D/YYYY", date(1998, 12, 1), id="slashes-12/1-is-december"),
        pytest.param("12282021", "MDYYYY", date(2021, 12, 28), id="MDYYYY-8-digits"),
        pytest.param("6152022", "MDYYYY", date(2022, 6, 15), id="MDYYYY-only-one-split-valid-a"),
        pytest.param("8202022", "MDYYYY", date(2022, 8, 20), id="MDYYYY-only-one-split-valid-b"),
        pytest.param("02292000", "MMDDYYYY", date(2000, 2, 29), id="leap-day-2000-is-valid"),
    ],
)
def test_parse_date_ok(text: str, fmt: str, expected: date) -> None:
    result = parse_date(text, fmt)

    assert result.status is ParseStatus.OK
    assert result.value == expected


@pytest.mark.parametrize(
    ("text", "fmt", "candidates"),
    [
        pytest.param("1052022", "MDYYYY", (date(2022, 1, 5), date(2022, 10, 5)), id="1052022"),
        pytest.param("1152022", "MDYYYY", (date(2022, 1, 15), date(2022, 11, 5)), id="1152022"),
    ],
)
def test_parse_date_ambiguous_has_no_value_but_lists_both_readings(
    text: str, fmt: str, candidates: tuple[date, ...]
) -> None:
    result = parse_date(text, fmt)

    assert result.status is ParseStatus.AMBIGUOUS
    assert result.value is None
    assert result.candidates == candidates  # sorted, earliest first


@pytest.mark.parametrize(
    ("text", "fmt"),
    [
        pytest.param("2021-13-13", "YYYY-MM-DD", id="month-13"),
        pytest.param("2022-02-30", "YYYY-MM-DD", id="30-february"),
        pytest.param("02291999", "MMDDYYYY", id="29-february-1999-not-a-leap-year"),
        pytest.param("3051985", "MMDDYYYY", id="7-digits-leading-zero-lost"),
        pytest.param("abc", "YYYYMMDD", id="letters"),
    ],
)
def test_parse_date_invalid(text: str, fmt: str) -> None:
    result = parse_date(text, fmt)

    assert result.status is ParseStatus.INVALID
    assert result.value is None


@pytest.mark.parametrize("fmt", ALL_FORMATS)
@pytest.mark.parametrize(
    "text",
    [
        pytest.param(None, id="None"),
        pytest.param("", id="empty"),
        pytest.param("   ", id="spaces"),
        pytest.param("NULL", id="NULL"),
        pytest.param("null", id="null"),
    ],
)
def test_parse_date_null_for_missing_or_null_text_in_every_format(
    text: str | None, fmt: str
) -> None:
    result = parse_date(text, fmt)

    assert result.status is ParseStatus.NULL
    assert result.value is None


def test_parse_date_unknown_format_raises_value_error() -> None:
    with pytest.raises(ValueError):
        parse_date("20101012", "DD/MM/YYYY")


@pytest.mark.parametrize("text", ["  20101012", "20101012  ", "\t20101012\n"])
def test_parse_date_strips_surrounding_whitespace(text: str) -> None:
    result = parse_date(text, "YYYYMMDD")

    assert result.status is ParseStatus.OK
    assert result.value == date(2010, 10, 12)


def test_parse_date_null_marker_is_recognised_after_stripping() -> None:
    assert parse_date("  NULL  ", "YYYYMMDD").status is ParseStatus.NULL


def test_parse_result_is_frozen() -> None:
    result = parse_date("20101012", "YYYYMMDD")

    assert isinstance(result, ParseResult)
    with pytest.raises(FrozenInstanceError):
        result.value = date(2000, 1, 1)  # type: ignore[misc]


def test_parse_status_has_exactly_the_four_members() -> None:
    assert {s.name for s in ParseStatus} == {"OK", "NULL", "INVALID", "AMBIGUOUS"}
    assert parse_date("20101012", "YYYYMMDD").status in ParseStatus


def test_candidates_for_ok_null_and_invalid_results() -> None:
    """Assumption to confirm: OK lists its single reading; NULL and INVALID list none."""
    assert parse_date("20101012", "YYYYMMDD").candidates == (date(2010, 10, 12),)
    assert parse_date("6152022", "MDYYYY").candidates == (date(2022, 6, 15),)
    assert parse_date(None, "YYYYMMDD").candidates == ()
    assert parse_date("2021-13-13", "YYYY-MM-DD").candidates == ()


# ---- compute_age ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("dob", "as_of", "expected"),
    [
        pytest.param(date(1985, 3, 5), date(2024, 3, 4), 38, id="birthday-tomorrow-not-reached"),
        pytest.param(date(1985, 3, 5), date(2024, 3, 5), 39, id="birthday-today-counts"),
        pytest.param(date(1985, 3, 5), date(2024, 12, 31), 39, id="after-birthday-same-year"),
        pytest.param(date(1985, 12, 31), date(2024, 1, 1), 38, id="not-the-datediff-year-boundary"),
        pytest.param(
            date(2000, 2, 29), date(2023, 2, 28), 22, id="leapling-non-leap-before-1-march"
        ),
        pytest.param(date(2000, 2, 29), date(2023, 3, 1), 23, id="leapling-non-leap-on-1-march"),
        pytest.param(date(2000, 2, 29), date(2024, 2, 29), 24, id="leapling-on-leap-day"),
        pytest.param(date(2024, 1, 15), date(2024, 1, 15), 0, id="born-on-as-of"),
        pytest.param(date(1850, 1, 1), date(2024, 1, 15), 174, id="no-upper-limit-on-age"),
    ],
)
def test_compute_age(dob: date, as_of: date, expected: int) -> None:
    assert compute_age(dob, as_of) == expected


def test_compute_age_is_none_without_a_date_of_birth() -> None:
    assert compute_age(None, date(2024, 1, 15)) is None


def test_compute_age_is_none_for_a_date_of_birth_in_the_future() -> None:
    assert compute_age(date(2025, 1, 1), date(2024, 1, 15)) is None


def test_compute_age_requires_as_of() -> None:
    with pytest.raises(TypeError):
        compute_age(date(1985, 3, 5))  # type: ignore[call-arg]


# ---- is_stale ------------------------------------------------------------------------------

AS_OF = date(2024, 1, 15)


@pytest.mark.parametrize(
    ("last_flight", "expected"),
    [
        pytest.param(date(2023, 10, 17), False, id="exactly-90-days-ago-rule-is-more-than-90"),
        pytest.param(date(2023, 10, 16), True, id="91-days-ago"),
        pytest.param(date(2023, 10, 18), False, id="89-days-ago"),
        pytest.param(date(2024, 1, 15), False, id="flew-on-as-of"),
        pytest.param(date(2022, 6, 15), True, id="long-ago-IND-example"),
        pytest.param(None, None, id="no-flight-date-is-unknown-not-stale"),
        pytest.param(date(2024, 1, 16), None, id="future-flight-date-is-bad-data"),
    ],
)
def test_is_stale_with_the_default_threshold(
    last_flight: date | None, expected: bool | None
) -> None:
    assert is_stale(last_flight, AS_OF) is expected


@pytest.mark.parametrize(
    ("last_flight", "expected"),
    [
        pytest.param(date(2023, 12, 16), False, id="30-days-ago"),
        pytest.param(date(2023, 12, 15), True, id="31-days-ago"),
    ],
)
def test_is_stale_threshold_days_moves_the_boundary(last_flight: date, expected: bool) -> None:
    assert is_stale(last_flight, AS_OF, threshold_days=30) is expected


@pytest.mark.parametrize(
    ("last_flight", "expected"),
    [
        pytest.param(date(2024, 1, 14), True, id="yesterday-is-stale"),
        pytest.param(date(2023, 1, 15), True, id="a-year-ago-is-stale"),
        pytest.param(date(2024, 1, 15), False, id="today-is-not-stale"),
    ],
)
def test_is_stale_with_a_zero_threshold_any_past_day_is_stale(
    last_flight: date, expected: bool
) -> None:
    assert is_stale(last_flight, AS_OF, threshold_days=0) is expected


def test_is_stale_requires_as_of() -> None:
    with pytest.raises(TypeError):
        is_stale(date(2023, 1, 1))  # type: ignore[call-arg]


def test_is_stale_requires_as_of_even_when_threshold_is_given() -> None:
    with pytest.raises(TypeError):
        is_stale(date(2023, 1, 1), threshold_days=30)  # type: ignore[call-arg]
