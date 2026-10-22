"""Unit tests for skypoints.ingest.redemptions.flatten_redemption_feed.

The feed is a JSON array of member objects; each transaction becomes one row. Flattening types
nothing and validates nothing: every value is text (leading zeros kept), and file metadata is
attached. Typing and the RD001..RD005 rules belong to the load (tests/integration).
"""

import json
from datetime import datetime
from pathlib import Path

import pytest

from skypoints.ingest.flat_file import InvalidFileNameError
from skypoints.ingest.redemptions import MemberEntry, flatten_redemption_feed

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "redemptions"
FILE = "SKYPOINTS_REDEMPTIONS_20240115_09000000.json"
FILE_TS = datetime(2024, 1, 15, 9, 0, 0)
ROW_COLUMNS = {
    "member_id", "feed_date", "txn_id", "txn_date", "partner", "miles_redeemed", "status",
    "file_name", "file_row_number", "txn_seq", "file_ts",
}  # fmt: skip
TEXT_COLUMNS = [
    "member_id",
    "feed_date",
    "txn_id",
    "txn_date",
    "partner",
    "miles_redeemed",
    "status",
]


def feed(scenario: str) -> Path:
    return FIXTURES / scenario / FILE


def write_feed(directory: Path, content, name: str = FILE) -> Path:
    path = directory / name
    path.write_text(json.dumps(content), encoding="utf-8")
    return path


# ---- the brief's sample ------------------------------------------------------------------------


def test_the_brief_sample_yields_exactly_two_rows() -> None:
    result = flatten_redemption_feed(feed("brief"))

    assert len(result.rows) == 2


def test_the_brief_sample_rows_carry_every_field_and_the_file_metadata() -> None:
    result = flatten_redemption_feed(feed("brief"))

    assert result.rows == [
        {
            "member_id": "223457", "feed_date": "20240115", "txn_id": "RX10091",
            "txn_date": "20240110", "partner": "AeroLink", "miles_redeemed": "12000",
            "status": "COMPLETED", "file_name": FILE, "file_row_number": 1, "txn_seq": 1,
            "file_ts": FILE_TS,
        },
        {
            "member_id": "223457", "feed_date": "20240115", "txn_id": "RX10092",
            "txn_date": "20240113", "partner": "SkyPoints", "miles_redeemed": "5000",
            "status": "PENDING", "file_name": FILE, "file_row_number": 1, "txn_seq": 2,
            "file_ts": FILE_TS,
        },
    ]  # fmt: skip


def test_every_row_has_exactly_the_expected_columns() -> None:
    result = flatten_redemption_feed(feed("brief"))

    assert all(set(row) == ROW_COLUMNS for row in result.rows)


@pytest.mark.parametrize("column", TEXT_COLUMNS)
def test_every_feed_value_is_returned_as_text(column: str) -> None:
    result = flatten_redemption_feed(feed("brief"))

    assert all(isinstance(row[column], str) for row in result.rows)


def test_dates_are_not_parsed_when_flattening() -> None:
    result = flatten_redemption_feed(feed("brief"))

    assert {row["feed_date"] for row in result.rows} == {"20240115"}
    assert [row["txn_date"] for row in result.rows] == ["20240110", "20240113"]


def test_the_result_names_the_file_and_its_timestamp() -> None:
    result = flatten_redemption_feed(feed("brief"))

    assert result.file_name == FILE
    assert result.file_ts == FILE_TS  # TT in the file name = hundredths of a second


def test_file_ts_comes_from_the_file_name(tmp_path: Path) -> None:
    path = write_feed(tmp_path, [], name="SKYPOINTS_REDEMPTIONS_20240229_13453025.json")

    assert flatten_redemption_feed(path).file_ts == datetime(2024, 2, 29, 13, 45, 30, 250000)


# ---- several members -------------------------------------------------------------------------------


def test_one_row_per_transaction_across_members_with_member_position_and_sequence() -> None:
    result = flatten_redemption_feed(feed("multi"))

    assert [
        (r["member_id"], r["txn_id"], r["file_row_number"], r["txn_seq"]) for r in result.rows
    ] == [
        ("223457", "RX10091", 1, 1),
        ("223457", "RX10092", 1, 2),
        ("223458", "RX10093", 2, 1),
    ]


def test_every_member_object_is_listed_with_its_transaction_count() -> None:
    result = flatten_redemption_feed(feed("multi"))

    assert result.members == [
        MemberEntry("223457", "20240115", 2, 1),
        MemberEntry("223458", "20240115", 1, 2),
    ]


# ---- empty redemptions array ---------------------------------------------------------------------


def test_an_empty_redemptions_array_yields_no_rows() -> None:
    result = flatten_redemption_feed(feed("empty_array"))

    assert [r["member_id"] for r in result.rows] == ["223458"]


def test_an_empty_redemptions_array_is_still_recorded_as_a_member() -> None:
    result = flatten_redemption_feed(feed("empty_array"))

    assert result.members == [
        MemberEntry("223457", "20240115", 0, 1),
        MemberEntry("223458", "20240115", 1, 2),
    ]


def test_a_feed_of_only_empty_arrays_yields_no_rows_but_lists_the_members(tmp_path: Path) -> None:
    path = write_feed(
        tmp_path, [{"member_id": "223457", "feed_date": "20240115", "redemptions": []}]
    )

    result = flatten_redemption_feed(path)

    assert result.rows == []
    assert result.members == [MemberEntry("223457", "20240115", 0, 1)]


def test_an_empty_feed_yields_nothing(tmp_path: Path) -> None:
    result = flatten_redemption_feed(write_feed(tmp_path, []))

    assert (result.rows, result.members) == ([], [])


# ---- member_id stays text ----------------------------------------------------------------------


def test_member_id_keeps_its_leading_zeros() -> None:
    result = flatten_redemption_feed(feed("leading_zeros"))

    assert [r["member_id"] for r in result.rows] == ["007890"]
    assert result.members[0].member_id == "007890"


def test_a_numeric_member_id_in_the_json_becomes_plain_text(tmp_path: Path) -> None:
    content = [
        {
            "member_id": 7890,
            "feed_date": "20240115",
            "redemptions": [
                {
                    "txn_id": "RX1",
                    "txn_date": "20240110",
                    "partner": "P",
                    "miles_redeemed": 1,
                    "status": "COMPLETED",
                }
            ],
        }
    ]

    result = flatten_redemption_feed(write_feed(tmp_path, content))

    assert result.rows[0]["member_id"] == "7890"
    assert result.members[0].member_id == "7890"


# ---- numbers and odd values are not interpreted --------------------------------------------------


@pytest.mark.parametrize(
    ("miles", "text"),
    [(12000, "12000"), (-500, "-500"), (0, "0"), ("007", "007"), ("12k", "12k")],
)
def test_miles_redeemed_is_carried_as_text_without_judgement(
    tmp_path: Path, miles, text: str
) -> None:
    content = [
        {
            "member_id": "223457",
            "feed_date": "20240115",
            "redemptions": [
                {
                    "txn_id": "RX1",
                    "txn_date": "20240110",
                    "partner": "P",
                    "miles_redeemed": miles,
                    "status": "COMPLETED",
                }
            ],
        }
    ]

    result = flatten_redemption_feed(write_feed(tmp_path, content))

    assert result.rows[0]["miles_redeemed"] == text


def test_an_unknown_status_and_a_late_txn_date_are_not_judged_when_flattening() -> None:
    bad_status = flatten_redemption_feed(feed("unknown_status")).rows
    late = flatten_redemption_feed(feed("txn_after_feed")).rows

    assert [r["status"] for r in bad_status] == ["REFUNDED", "COMPLETED"]
    assert late[0]["txn_date"] == "20240116" and late[0]["feed_date"] == "20240115"


def test_duplicate_transactions_are_not_removed_when_flattening() -> None:
    result = flatten_redemption_feed(feed("duplicate_same_file"))

    assert [r["txn_id"] for r in result.rows] == ["RX10091", "RX10092", "RX10091"]


# ---- bad input ----------------------------------------------------------------------------------


@pytest.mark.parametrize(
    "bad_name",
    [
        "redemptions.json",
        "SKYPOINTS_REDEMPTIONS_20240115_09000000.dat",  # wrong extension
        "SKYPOINTS_MEMBERS_20240115_09000000.json",  # wrong feed
        "SKYPOINTS_REDEMPTIONS_20240115_090000.json",  # no TT part
        "SKYPOINTS_REDEMPTIONS_20241345_09000000.json",  # month 13
    ],
)
def test_an_invalid_file_name_is_rejected(tmp_path: Path, bad_name: str) -> None:
    path = write_feed(tmp_path, [], name=bad_name)

    with pytest.raises(InvalidFileNameError) as excinfo:
        flatten_redemption_feed(path)

    assert bad_name in str(excinfo.value)


def test_a_missing_file_with_a_valid_name_raises_file_not_found(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        flatten_redemption_feed(tmp_path / FILE)
