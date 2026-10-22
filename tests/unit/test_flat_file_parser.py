"""Unit tests for skypoints.ingest.flat_file.parse_member_feed(path) -> (header, records, warnings).

Columns are mapped by header NAME, never by position, so column order does not matter.
  * Missing OPTIONAL column (e.g. Post_Code): allowed, a warning is returned, the value is ''.
  * Missing MANDATORY column (Member_Name, Member_Id, Enrollment_Date), an unknown column, or a
    duplicate column: HeaderMismatchError, carrying .missing, .unexpected and .duplicates.
`header` is the list of column names exactly as they appear in the file (record type excluded).
`warnings` is a list of FeedWarning(code, column, message) objects, not strings.
"""

from datetime import datetime
from pathlib import Path
import shutil

import pytest

from skypoints.ingest.flat_file import (
    FeedWarning,
    HeaderMismatchError,
    InvalidFileNameError,
    parse_member_feed,
)

MEMBERS_DIR = Path(__file__).resolve().parents[1] / "fixtures" / "members"
VALID_FILE = MEMBERS_DIR / "SKYPOINTS_MEMBERS_20240115_13453025.dat"  # full 11-field layout
SAMPLE_LAYOUT_FILE = MEMBERS_DIR / "SKYPOINTS_MEMBERS_20240116_08000000.dat"  # brief's 10 fields
GOOD_NAME = "SKYPOINTS_MEMBERS_20240115_13453025.dat"

EXPECTED_HEADER = [
    "Member_Name",
    "Member_Id",
    "Enrollment_Date",
    "Last_Flight_Date",
    "Tier_Code",
    "Agent_Name",
    "State",
    "Country",
    "Post_Code",
    "DOB",
    "Is_Active",
]
SAMPLE_HEADER = [name for name in EXPECTED_HEADER if name != "Post_Code"]
BUSINESS_KEYS = [
    "member_name",
    "member_id",
    "enrollment_date",
    "last_flight_date",
    "tier_code",
    "agent_name",
    "state",
    "country",
    "post_code",
    "date_of_birth",
    "active_member",
]


def _rows(path: Path) -> list[list[str]]:
    """Fixture file as rows of tokens, leading pipe and record type removed."""
    return [line.split("|")[1:] for line in path.read_text(encoding="utf-8").splitlines()]


def _write(tmp_path: Path, rows: list[list[str]], name: str = GOOD_NAME) -> Path:
    path = tmp_path / name
    path.write_text("".join("|" + "|".join(row) + "\n" for row in rows), encoding="utf-8")
    return path


def _drop_column(rows: list[list[str]], column: str) -> list[list[str]]:
    index = rows[0].index(column) if column in rows[0] else None
    assert index is not None, column
    return [row[:index] + row[index + 1 :] for row in rows]


def _append_column(rows: list[list[str]], column: str, value: str) -> list[list[str]]:
    return [[*row, column if row[0] == "H" else value] for row in rows]


# ---- leading pipe and H/D split ------------------------------------------------------------


def test_header_is_returned_without_the_leading_pipe_or_record_type() -> None:
    header, _, _ = parse_member_feed(VALID_FILE)

    assert header == EXPECTED_HEADER


def test_only_detail_rows_are_returned_as_records() -> None:
    _, records, _ = parse_member_feed(VALID_FILE)

    assert [r["member_name"] for r in records] == ["Elena", "Raj"]


def test_detail_values_land_in_the_right_columns() -> None:
    _, records, _ = parse_member_feed(VALID_FILE)

    assert records[0] == {
        "member_name": "Elena",
        "member_id": "223457",
        "enrollment_date": "20101012",
        "last_flight_date": "20121013",
        "tier_code": "GLD",
        "agent_name": "Sam",
        "state": "CA",
        "country": "USA",
        "post_code": "02134",
        "date_of_birth": "03051985",
        "active_member": "A",
        "file_name": "SKYPOINTS_MEMBERS_20240115_13453025.dat",
        "file_row_number": 2,
        "file_ts": datetime(2024, 1, 15, 13, 45, 30, 250000),
    }


def test_records_have_the_expected_keys() -> None:
    _, records, _ = parse_member_feed(VALID_FILE)

    for record in records:
        assert set(record) == {*BUSINESS_KEYS, "file_name", "file_row_number", "file_ts"}


# ---- values stay text ------------------------------------------------------------------------


def test_every_business_value_is_a_string() -> None:
    _, records, _ = parse_member_feed(VALID_FILE)

    for record in records:
        for key in BUSINESS_KEYS:
            assert isinstance(record[key], str), key


def test_leading_zeros_are_preserved() -> None:
    _, records, _ = parse_member_feed(VALID_FILE)

    assert records[0]["date_of_birth"] == "03051985"
    assert records[0]["post_code"] == "02134"
    assert records[1]["member_id"] == "00789"


def test_blank_fields_stay_empty_strings_not_none() -> None:
    _, records, _ = parse_member_feed(VALID_FILE)

    assert records[1]["last_flight_date"] == ""
    assert records[1]["agent_name"] == ""
    assert records[1]["post_code"] == ""


# ---- load metadata ---------------------------------------------------------------------------


def test_file_name_is_the_base_name_not_the_full_path() -> None:
    _, records, _ = parse_member_feed(VALID_FILE)

    assert {r["file_name"] for r in records} == {VALID_FILE.name}


def test_file_row_number_is_the_one_based_line_number_with_the_header_as_line_one() -> None:
    _, records, _ = parse_member_feed(VALID_FILE)

    assert [r["file_row_number"] for r in records] == [2, 3]


def test_file_ts_is_parsed_from_the_file_name_with_tt_as_hundredths_of_a_second() -> None:
    _, records, _ = parse_member_feed(VALID_FILE)

    assert {r["file_ts"] for r in records} == {datetime(2024, 1, 15, 13, 45, 30, 250000)}


def test_file_ts_does_not_depend_on_the_clock_or_file_modified_time(tmp_path: Path) -> None:
    copy = tmp_path / "SKYPOINTS_MEMBERS_20200229_00000000.dat"
    shutil.copy(VALID_FILE, copy)

    _, records, _ = parse_member_feed(copy)

    assert records[0]["file_ts"] == datetime(2020, 2, 29, 0, 0, 0)


def test_accepts_a_str_path() -> None:
    header, records, _ = parse_member_feed(str(VALID_FILE))

    assert header == EXPECTED_HEADER
    assert len(records) == 2


# ---- header handling: map by name ----------------------------------------------------------


def test_full_header_gives_no_warnings() -> None:
    _, _, warnings = parse_member_feed(VALID_FILE)

    assert warnings == []


def test_the_briefs_10_field_sample_header_parses_with_a_post_code_warning() -> None:
    header, records, warnings = parse_member_feed(SAMPLE_LAYOUT_FILE)

    assert header == SAMPLE_HEADER
    assert len(records) == 1
    assert len(warnings) == 1
    warning = warnings[0]
    assert isinstance(warning, FeedWarning)
    assert warning.code == "MISSING_OPTIONAL_COLUMN"
    assert warning.column == "Post_Code"
    assert "Post_Code" in warning.message
    assert SAMPLE_LAYOUT_FILE.name in warning.message


def test_a_warning_is_a_small_immutable_record_of_code_column_and_message() -> None:
    warning = FeedWarning(code="C", column="Col", message="m")

    assert (warning.code, warning.column, warning.message) == ("C", "Col", "m")
    assert warning == FeedWarning("C", "Col", "m")
    with pytest.raises(AttributeError):
        warning.code = "other"  # type: ignore[misc]


def test_sample_header_values_are_mapped_by_name_and_post_code_is_blank() -> None:
    _, records, _ = parse_member_feed(SAMPLE_LAYOUT_FILE)

    assert records[0] == {
        "member_name": "Elena",
        "member_id": "223457",
        "enrollment_date": "20101012",
        "last_flight_date": "20121013",
        "tier_code": "GLD",
        "agent_name": "Sam",
        "state": "CA",
        "country": "USA",
        "post_code": "",  # absent column -> ''
        "date_of_birth": "03051985",  # still read from the DOB column, not from position 10
        "active_member": "A",
        "file_name": "SKYPOINTS_MEMBERS_20240116_08000000.dat",
        "file_row_number": 2,
        "file_ts": datetime(2024, 1, 16, 8, 0, 0),
    }


def test_each_missing_optional_column_gets_its_own_warning(tmp_path: Path) -> None:
    rows = _drop_column(_drop_column(_rows(VALID_FILE), "Agent_Name"), "Is_Active")
    path = _write(tmp_path, rows)

    _, records, warnings = parse_member_feed(path)

    assert len(warnings) == 2
    assert {w.column for w in warnings} == {"Agent_Name", "Is_Active"}
    assert {w.code for w in warnings} == {"MISSING_OPTIONAL_COLUMN"}
    assert records[0]["agent_name"] == ""
    assert records[0]["active_member"] == ""


@pytest.mark.parametrize(
    "mandatory", ["Member_Name", "Member_Id", "Enrollment_Date"]
)
def test_missing_mandatory_column_raises_and_names_it(tmp_path: Path, mandatory: str) -> None:
    path = _write(tmp_path, _drop_column(_rows(VALID_FILE), mandatory))

    with pytest.raises(HeaderMismatchError) as excinfo:
        parse_member_feed(path)

    assert excinfo.value.missing == [mandatory]
    assert excinfo.value.unexpected == []
    assert mandatory in str(excinfo.value)
    assert GOOD_NAME in str(excinfo.value)


def test_unknown_column_raises_and_names_it(tmp_path: Path) -> None:
    path = _write(tmp_path, _append_column(_rows(VALID_FILE), "Region", "X"))

    with pytest.raises(HeaderMismatchError) as excinfo:
        parse_member_feed(path)

    assert excinfo.value.unexpected == ["Region"]
    assert excinfo.value.missing == []
    assert "Region" in str(excinfo.value)


def test_duplicate_column_raises_and_names_it(tmp_path: Path) -> None:
    path = _write(tmp_path, _append_column(_rows(VALID_FILE), "State", "NY"))

    with pytest.raises(HeaderMismatchError) as excinfo:
        parse_member_feed(path)

    assert excinfo.value.duplicates == ["State"]
    assert "State" in str(excinfo.value)


def test_missing_and_unknown_columns_are_both_reported(tmp_path: Path) -> None:
    rows = _append_column(_drop_column(_rows(VALID_FILE), "Member_Id"), "Region", "X")
    path = _write(tmp_path, rows)

    with pytest.raises(HeaderMismatchError) as excinfo:
        parse_member_feed(path)

    assert excinfo.value.missing == ["Member_Id"]
    assert excinfo.value.unexpected == ["Region"]


def test_header_mismatch_error_is_a_value_error() -> None:
    assert issubclass(HeaderMismatchError, ValueError)


def test_column_order_does_not_matter(tmp_path: Path) -> None:
    rows = _rows(VALID_FILE)
    order = [8, 1, 0, 3, 2, 10, 9, 5, 4, 7, 6]  # a shuffle of the 11 columns, applied to every row
    shuffled = [[row[0], *(row[1:][i] for i in order)] for row in rows]
    path = _write(tmp_path, shuffled)

    header, records, warnings = parse_member_feed(path)

    assert header == [EXPECTED_HEADER[i] for i in order]
    assert warnings == []
    assert records == parse_member_feed(VALID_FILE)[1]


def test_file_without_a_header_row_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / GOOD_NAME
    path.write_text("|D|Elena|223457|20101012|||||||\n", encoding="utf-8")

    with pytest.raises(HeaderMismatchError):
        parse_member_feed(path)


def test_empty_file_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / GOOD_NAME
    path.write_text("", encoding="utf-8")

    with pytest.raises(HeaderMismatchError):
        parse_member_feed(path)


def test_header_only_file_returns_no_records(tmp_path: Path) -> None:
    path = tmp_path / GOOD_NAME
    path.write_text(VALID_FILE.read_text().splitlines()[0] + "\n", encoding="utf-8")

    header, records, warnings = parse_member_feed(path)

    assert header == EXPECTED_HEADER
    assert records == []
    assert warnings == []


# ---- file name validation ----------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "members.dat",
        "SKYPOINTS_MEMBERS_20240115_13453025.csv",  # wrong extension
        "SKYPOINTS_MEMBERS_2024011_13453025.dat",  # date too short
        "SKYPOINTS_MEMBERS_20240115_134530.dat",  # time lacks the TT part
        "SKYPOINTS_MEMBERS_20241345_13453025.dat",  # month 13 / day 45
        "SKYPOINTS_MEMBERS_20240115_25453025.dat",  # hour 25
        "skypoints_members_20240115_13453025.dat",  # wrong case
        "SKYPOINTS_REDEMPTIONS_20240115_13453025.dat",  # wrong feed
    ],
)
def test_invalid_file_name_is_rejected(tmp_path: Path, bad_name: str) -> None:
    path = tmp_path / bad_name
    shutil.copy(VALID_FILE, path)

    with pytest.raises(InvalidFileNameError) as excinfo:
        parse_member_feed(path)

    assert bad_name in str(excinfo.value)


def test_invalid_file_name_is_rejected_before_the_file_is_opened(tmp_path: Path) -> None:
    with pytest.raises(InvalidFileNameError):
        parse_member_feed(tmp_path / "not_a_feed_name.dat")  # does not exist


def test_invalid_file_name_error_is_a_value_error() -> None:
    assert issubclass(InvalidFileNameError, ValueError)


def test_missing_file_with_a_valid_name_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        parse_member_feed(tmp_path / GOOD_NAME)
