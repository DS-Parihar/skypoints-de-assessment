"""Integration test: AGE and STALE_MEMBER computed by sql/duckdb/load_staging.sql must equal what
skypoints.transform.derive returns for the same raw values and the same as_of date.

Flow: member feed file -> parse_member_feed -> RAW.MEMBER_FEED_RAW -> load_staging.sql ->
STAGING.MEMBER_STG, then compare each staged row with compute_age / is_stale.
"""

from datetime import date
from pathlib import Path

import duckdb
import pytest

from skypoints.db import DEFAULT_DDL_DIR, create_database
from skypoints.ingest.flat_file import parse_member_feed
from skypoints.transform.derive import compute_age, is_stale, parse_date

FIXTURE_FEED = (
    Path(__file__).resolve().parent
    / "fixtures"
    / "members"
    / "SKYPOINTS_MEMBERS_20240115_13453025.dat"
)
HEADER = (
    "|H|Member_Name|Member_Id|Enrollment_Date|Last_Flight_Date|Tier_Code|Agent_Name|State|"
    "Country|Post_Code|DOB|Is_Active\n"
)

# (date of birth MMDDYYYY, last flight YYYYMMDD): edge cases for the birthday and 90-day rules.
EDGE_CASES = [
    ("03051985", "20231017"),  # birthday 5 March; flight exactly 90 days before 2024-01-15
    ("03051985", "20231016"),  # 91 days before 2024-01-15
    ("12311985", "20240115"),  # born 31 Dec (the DATEDIFF year-boundary bug); flew on as_of
    ("02292000", "20220615"),  # 29 February birthday; long ago
    ("01152024", ""),  # born 15 Jan 2024; no flight date
    ("01012025", "20240116"),  # DOB in the future; flight after 2024-01-15
    ("", "NULL"),  # no DOB; flight date written as the text NULL
    ("13051985", "20230101"),  # DOB with month 13: unreadable
    ("03051985", "20240301"),  # flight in the future for most as_of dates
]
AS_OF_DATES = [
    date(2023, 2, 28),
    date(2023, 3, 1),
    date(2024, 1, 15),
    date(2024, 3, 4),
    date(2024, 3, 5),
    date(2024, 12, 31),
]


def _edge_feed(directory: Path) -> Path:
    path = directory / "SKYPOINTS_MEMBERS_20240116_08000000.dat"
    lines = [
        f"|D|Member {i}|{1000 + i}|20100101|{flight}|GLD|Sam|CA|USA|02134|{dob}|A\n"
        for i, (dob, flight) in enumerate(EDGE_CASES)
    ]
    path.write_text(HEADER + "".join(lines), encoding="utf-8")
    return path


def _load_raw(con: duckdb.DuckDBPyConnection, path: Path, batch_id: str) -> None:
    _, records, _ = parse_member_feed(path)
    for r in records:
        con.execute(
            "INSERT INTO raw.member_feed_raw (record_type, member_name, member_id, enrollment_date, "
            "last_flight_date, tier_code, agent_name, state, country, post_code, date_of_birth, "
            "active_member, source_system, member_key, file_name, file_row_number, file_ts, batch_id) "
            "VALUES ('D', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SKYPOINTS', ?, ?, ?, ?, ?)",
            [
                r["member_name"], r["member_id"], r["enrollment_date"], r["last_flight_date"],
                r["tier_code"], r["agent_name"], r["state"], r["country"], r["post_code"],
                r["date_of_birth"], r["active_member"], f"SKYPOINTS:{r['member_id']}",
                r["file_name"], r["file_row_number"], r["file_ts"], batch_id,
            ],
        )  # fmt: skip


def _stage(con: duckdb.DuckDBPyConnection, batch_id: str, as_of: date) -> None:
    sql = (DEFAULT_DDL_DIR / "load_staging.sql").read_text(encoding="utf-8")
    con.execute(sql, {"batch_id": batch_id, "as_of_date": as_of})


def _python_expected(raw_dob: str, raw_flight: str, as_of: date) -> tuple[int | None, bool | None]:
    dob = parse_date(raw_dob, "MMDDYYYY").value
    flight = parse_date(raw_flight, "YYYYMMDD").value
    return compute_age(dob, as_of), is_stale(flight, as_of)


@pytest.mark.parametrize("as_of", AS_OF_DATES, ids=lambda d: d.isoformat())
def test_sql_age_and_stale_member_match_the_python_functions(tmp_path: Path, as_of: date) -> None:
    con = create_database()
    _load_raw(con, FIXTURE_FEED, "b1")
    _load_raw(con, _edge_feed(tmp_path), "b1")

    _stage(con, "b1", as_of)

    rows = con.execute(
        "SELECT s.file_name, s.file_row_number, r.date_of_birth, r.last_flight_date, s.age, "
        "s.stale_member, s.as_of_date "
        "FROM staging.member_stg s JOIN raw.member_feed_raw r "
        "ON r.file_name = s.file_name AND r.file_row_number = s.file_row_number "
        "ORDER BY s.file_name, s.file_row_number"
    ).fetchall()
    assert len(rows) == 2 + len(EDGE_CASES)  # nothing rejected, nothing lost
    for file_name, row_number, raw_dob, raw_flight, age, stale, staged_as_of in rows:
        expected = _python_expected(raw_dob, raw_flight, as_of)
        assert (age, stale) == expected, (file_name, row_number, raw_dob, raw_flight, as_of)
        assert staged_as_of == as_of
    con.close()


def test_the_fixture_feed_rows_get_the_expected_age_and_stale_member() -> None:
    con = create_database()
    _load_raw(con, FIXTURE_FEED, "b1")

    _stage(con, "b1", date(2024, 1, 15))

    rows = con.execute(
        "SELECT member_id, age, stale_member FROM staging.member_stg ORDER BY member_id"
    ).fetchall()
    # Raj (born 7 Dec 1990) has no flight date, so staleness is unknown; Elena last flew in 2012.
    assert rows == [("00789", 33, None), ("223457", 38, True)]
    con.close()


def test_the_comparison_is_not_vacuous(tmp_path: Path) -> None:
    """The edge cases must produce every kind of outcome, or matching would prove little."""
    ages: set[int | None] = set()
    stale: set[bool | None] = set()
    for as_of in AS_OF_DATES:
        for dob, flight in EDGE_CASES:
            age, is_old = _python_expected(dob, flight, as_of)
            ages.add(age)
            stale.add(is_old)

    assert None in ages and any(a is not None for a in ages)
    assert stale == {True, False, None}
