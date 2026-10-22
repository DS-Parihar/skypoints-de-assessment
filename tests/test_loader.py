"""Integration test: skypoints.ingest.loader.load_member_feed inserts a parsed member feed file
into RAW.MEMBER_FEED_RAW in DuckDB.

Contract under test:
  load_member_feed(con, path, batch_id) -> LoadResult(file_name, rows_loaded, skipped, warnings)
  * one raw row per line: the header as record_type 'H' (line 1) and each detail as 'D'
  * every business value is stored as text exactly as parsed ('' stays '', not NULL)
  * source_system = 'SKYPOINTS'; member_key = 'SKYPOINTS:' || member_id, NULL for the header
    row and for a blank member_id
  * file_name, file_row_number, file_ts and batch_id are filled; loaded_at is set by the database
  * all-or-nothing, and a file name that was already loaded is skipped (append-only raw)
"""

from datetime import date, datetime
from pathlib import Path
import shutil

import duckdb
import pytest

from skypoints.db import DEFAULT_DDL_DIR, create_database
from skypoints.ingest.flat_file import FeedWarning, HeaderMismatchError, InvalidFileNameError
from skypoints.ingest.loader import LoadResult, load_member_feed

MEMBERS_DIR = Path(__file__).resolve().parent / "fixtures" / "members"
FULL_FILE = MEMBERS_DIR / "SKYPOINTS_MEMBERS_20240115_13453025.dat"  # 11 fields, 2 details
SAMPLE_FILE = MEMBERS_DIR / "SKYPOINTS_MEMBERS_20240116_08000000.dat"  # 10 fields, no Post_Code
TRANSFORM_DIR = DEFAULT_DDL_DIR / "transform"
FILE_TS = datetime(2024, 1, 15, 13, 45, 30, 250000)


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    connection = create_database()
    yield connection
    connection.close()


def _rows(con: duckdb.DuckDBPyConnection, columns: str = "*", where: str = "TRUE") -> list[tuple]:
    return con.execute(
        f"SELECT {columns} FROM raw.member_feed_raw WHERE {where} ORDER BY file_name, file_row_number"
    ).fetchall()


# ---- result ------------------------------------------------------------------------------------


def test_returns_a_load_result_with_the_row_count() -> None:
    connection = create_database()

    result = load_member_feed(connection, FULL_FILE, "b1")

    assert isinstance(result, LoadResult)
    assert result.file_name == FULL_FILE.name
    assert result.rows_loaded == 3  # header + 2 details
    assert result.skipped is False
    assert result.warnings == []
    connection.close()


def test_warnings_from_the_parser_are_returned() -> None:
    connection = create_database()

    result = load_member_feed(connection, SAMPLE_FILE, "b1")

    assert [(w.code, w.column) for w in result.warnings] == [("MISSING_OPTIONAL_COLUMN", "Post_Code")]
    assert all(isinstance(w, FeedWarning) for w in result.warnings)
    connection.close()


# ---- what lands in RAW.MEMBER_FEED_RAW ----------------------------------------------------------


def test_detail_rows_are_inserted_as_text_with_keys_and_lineage(con) -> None:
    load_member_feed(con, FULL_FILE, "b1")

    elena = _rows(
        con,
        "record_type, member_name, member_id, enrollment_date, last_flight_date, tier_code, "
        "agent_name, state, country, post_code, date_of_birth, active_member, source_system, "
        "member_key, file_name, file_row_number, file_ts, batch_id",
        "member_id = '223457'",
    )

    assert elena == [
        (
            "D", "Elena", "223457", "20101012", "20121013", "GLD", "Sam", "CA", "USA", "02134",
            "03051985", "A", "SKYPOINTS", "SKYPOINTS:223457", FULL_FILE.name, 2, FILE_TS, "b1",
        )
    ]  # fmt: skip


def test_leading_zeros_and_blanks_are_kept_as_text(con) -> None:
    load_member_feed(con, FULL_FILE, "b1")

    raj = _rows(con, "member_id, last_flight_date, agent_name, post_code, member_key", "member_id = '00789'")

    assert raj == [("00789", "", "", "", "SKYPOINTS:00789")]  # '' stays '', not NULL


def test_the_header_row_is_stored_as_record_type_h_on_line_one(con) -> None:
    load_member_feed(con, FULL_FILE, "b1")

    header = _rows(
        con,
        "record_type, member_name, member_id, post_code, date_of_birth, active_member, "
        "source_system, member_key, file_row_number",
        "record_type = 'H'",
    )

    assert header == [
        ("H", "Member_Name", "Member_Id", "Post_Code", "DOB", "Is_Active", "SKYPOINTS", None, 1)
    ]


def test_a_blank_member_id_gets_no_member_key(con, tmp_path: Path) -> None:
    path = tmp_path / "SKYPOINTS_MEMBERS_20240115_13453025.dat"
    path.write_text(
        "|H|Member_Name|Member_Id|Enrollment_Date\n|D|Elena||20101012\n", encoding="utf-8"
    )

    load_member_feed(con, path, "b1")

    assert _rows(con, "member_id, member_key", "record_type = 'D'") == [("", None)]


def test_the_sample_header_is_loaded_by_name_with_post_code_blank(con) -> None:
    load_member_feed(con, SAMPLE_FILE, "b1")

    row = _rows(con, "date_of_birth, active_member, post_code", "record_type = 'D'")

    assert row == [("03051985", "A", "")]  # DOB read by name, not from position 10


def test_loaded_at_is_set_by_the_database(con) -> None:
    load_member_feed(con, FULL_FILE, "b1")

    assert con.execute(
        "SELECT count(*) FROM raw.member_feed_raw WHERE loaded_at IS NOT NULL"
    ).fetchone() == (3,)


# ---- re-runs and failures ------------------------------------------------------------------------


def test_loading_the_same_file_name_twice_is_skipped(con) -> None:
    load_member_feed(con, FULL_FILE, "b1")

    second = load_member_feed(con, FULL_FILE, "b2")

    assert second.skipped is True
    assert second.rows_loaded == 0
    assert con.execute("SELECT count(*) FROM raw.member_feed_raw").fetchone() == (3,)
    assert con.execute("SELECT DISTINCT batch_id FROM raw.member_feed_raw").fetchall() == [("b1",)]


def test_two_different_files_load_side_by_side(con) -> None:
    load_member_feed(con, FULL_FILE, "b1")
    load_member_feed(con, SAMPLE_FILE, "b2")

    assert con.execute("SELECT count(*) FROM raw.member_feed_raw").fetchone() == (5,)


def test_a_header_mismatch_inserts_nothing(con, tmp_path: Path) -> None:
    path = tmp_path / "SKYPOINTS_MEMBERS_20240115_13453025.dat"
    path.write_text("|H|Member_Name|Region\n|D|Elena|X\n", encoding="utf-8")

    with pytest.raises(HeaderMismatchError):
        load_member_feed(con, path, "b1")

    assert con.execute("SELECT count(*) FROM raw.member_feed_raw").fetchone() == (0,)


def test_an_invalid_file_name_inserts_nothing(con, tmp_path: Path) -> None:
    path = tmp_path / "members.dat"
    shutil.copy(FULL_FILE, path)

    with pytest.raises(InvalidFileNameError):
        load_member_feed(con, path, "b1")

    assert con.execute("SELECT count(*) FROM raw.member_feed_raw").fetchone() == (0,)


def test_a_failure_part_way_through_rolls_everything_back(con, monkeypatch) -> None:
    from skypoints.ingest import loader

    real = loader.parse_member_feed

    def broken_records(path):
        header, records, warnings = real(path)
        records[-1]["file_ts"] = object()  # cannot be bound as a timestamp -> insert fails
        return header, records, warnings

    monkeypatch.setattr(loader, "parse_member_feed", broken_records)

    with pytest.raises(Exception):
        load_member_feed(con, FULL_FILE, "b1")

    assert con.execute("SELECT count(*) FROM raw.member_feed_raw").fetchone() == (0,)


# ---- end to end with the staging load ------------------------------------------------------------


def test_a_loaded_file_flows_through_the_staging_sql(con) -> None:
    load_member_feed(con, FULL_FILE, "b1")

    con.execute((TRANSFORM_DIR / "10_quarantine_members.sql").read_text(), {"batch_id": "b1"})
    con.execute(
        (TRANSFORM_DIR / "11_stage_members.sql").read_text(),
        {"batch_id": "b1", "as_of": date(2024, 1, 15)},
    )

    staged = con.execute(
        "SELECT member_key, date_of_birth, post_code, age, country_code "
        "FROM staging.member_stg ORDER BY member_key"
    ).fetchall()
    assert staged == [
        ("SKYPOINTS:00789", date(1990, 12, 7), None, 33, "IND"),
        ("SKYPOINTS:223457", date(1985, 3, 5), "02134", 38, "USA"),
    ]
    assert con.execute("SELECT count(*) FROM dq.quarantine").fetchone() == (0,)
