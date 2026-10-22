"""Integration test: the DuckDB staging load SQL (sql/duckdb/transform) over RAW.MEMBER_FEED_RAW.

Blank and NULL are the same thing in staging: '', whitespace-only and NULL all become NULL.
"""

from datetime import date, datetime

import duckdb
import pytest

from skypoints.db import DEFAULT_DDL_DIR, create_database

TRANSFORM_DIR = DEFAULT_DDL_DIR / "transform"
AS_OF = date(2024, 1, 15)
FILE_TS = datetime(2024, 1, 15, 13, 45, 30, 250000)

OPTIONAL_TEXT_COLUMNS = [
    "last_flight_date",
    "tier_code",
    "agent_name",
    "state",
    "country",
    "post_code",
    "date_of_birth",
    "active_member",
]


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    connection = create_database()
    yield connection
    connection.close()


def _add_raw(
    con: duckdb.DuckDBPyConnection,
    row_number: int,
    *,
    record_type: str = "D",
    member_name: str | None = "Elena",
    member_id: str | None = "223457",
    enrollment_date: str | None = "20101012",
    last_flight_date: str | None = "20231201",
    tier_code: str | None = "GLD",
    agent_name: str | None = "Sam",
    state: str | None = "CA",
    country: str | None = "USA",
    post_code: str | None = "02134",
    date_of_birth: str | None = "03051985",
    active_member: str | None = "A",
    source_system: str = "SKYPOINTS",
    file_ts: datetime | None = FILE_TS,
    batch_id: str = "b1",
) -> None:
    key = f"{source_system}:{member_id}" if member_id else None
    con.execute(
        "INSERT INTO raw.member_feed_raw (record_type, member_name, member_id, enrollment_date, "
        "last_flight_date, tier_code, agent_name, state, country, post_code, date_of_birth, "
        "active_member, source_system, member_key, file_name, file_row_number, file_ts, batch_id) "
        "VALUES (?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SKYPOINTS_MEMBERS_20240115_13453025.dat', "
        "?, ?, ?)",
        [
            record_type, member_name, member_id, enrollment_date, last_flight_date, tier_code,
            agent_name, state, country, post_code, date_of_birth, active_member, source_system,
            key, row_number, file_ts, batch_id,
        ],
    )  # fmt: skip


def _run(con: duckdb.DuckDBPyConnection, as_of: date = AS_OF, batch_id: str = "b1") -> None:
    quarantine_sql = (TRANSFORM_DIR / "10_quarantine_members.sql").read_text(encoding="utf-8")
    stage_sql = (DEFAULT_DDL_DIR / "load_staging.sql").read_text(encoding="utf-8")
    con.execute(quarantine_sql, {"batch_id": batch_id})
    con.execute(stage_sql, {"batch_id": batch_id, "as_of_date": as_of})


def _staged(con: duckdb.DuckDBPyConnection, column: str, member_id: str = "223457"):
    return con.execute(
        f"SELECT {column} FROM staging.member_stg WHERE member_id = ?", [member_id]
    ).fetchone()[0]


# ---- blank and NULL are the same ------------------------------------------------------------


@pytest.mark.parametrize("blank", ["", "   ", None], ids=["empty", "spaces", "null"])
@pytest.mark.parametrize("column", OPTIONAL_TEXT_COLUMNS)
def test_blank_and_null_in_raw_both_become_null_in_staging(
    con: duckdb.DuckDBPyConnection, column: str, blank: str | None
) -> None:
    _add_raw(con, 2, **{column: blank})

    _run(con)

    assert _staged(con, column) is None


def test_a_blank_row_and_a_null_row_end_up_identical_in_staging(
    con: duckdb.DuckDBPyConnection,
) -> None:
    blanks = dict.fromkeys(OPTIONAL_TEXT_COLUMNS, "")
    nulls = dict.fromkeys(OPTIONAL_TEXT_COLUMNS)
    _add_raw(con, 2, member_id="1001", **blanks)
    _add_raw(con, 3, member_id="1002", **nulls)

    _run(con)

    columns = ", ".join(OPTIONAL_TEXT_COLUMNS)
    rows = con.execute(
        f"SELECT {columns} FROM staging.member_stg ORDER BY member_id"
    ).fetchall()
    assert rows == [(None,) * len(OPTIONAL_TEXT_COLUMNS)] * 2


def test_blank_and_null_do_not_raise_data_quality_flags_for_optional_dates(
    con: duckdb.DuckDBPyConnection,
) -> None:
    _add_raw(con, 2, member_id="1001", date_of_birth="", last_flight_date="")
    _add_raw(con, 3, member_id="1002", date_of_birth=None, last_flight_date=None)

    _run(con)

    rows = con.execute(
        "SELECT dq_dob_invalid, dq_last_flight_date_invalid FROM staging.member_stg"
    ).fetchall()
    assert rows == [(False, False)] * 2


def test_text_is_trimmed(con: duckdb.DuckDBPyConnection) -> None:
    _add_raw(con, 2, member_name="  Elena  ", agent_name=" Sam ")

    _run(con)

    assert _staged(con, "member_name") == "Elena"
    assert _staged(con, "agent_name") == "Sam"


# ---- mandatory columns: quarantine, not staging --------------------------------------------


@pytest.mark.parametrize(
    ("overrides", "reason"),
    [
        ({"member_id": ""}, "MISSING_MEMBER_ID"),
        ({"member_id": None}, "MISSING_MEMBER_ID"),
        ({"member_name": "  "}, "MISSING_MEMBER_NAME"),
        ({"member_name": None}, "MISSING_MEMBER_NAME"),
        ({"enrollment_date": ""}, "MISSING_ENROLLMENT_DATE"),
        ({"enrollment_date": None}, "MISSING_ENROLLMENT_DATE"),
        ({"enrollment_date": "20211313"}, "INVALID_ENROLLMENT_DATE"),
        ({"enrollment_date": "2021-12-13"}, "INVALID_ENROLLMENT_DATE"),
        ({"file_ts": None}, "NO_FILE_TS"),
        ({"member_name": "x" * 256}, "FIELD_TOO_LONG"),
    ],
)
def test_a_row_missing_a_mandatory_value_goes_to_quarantine_not_staging(
    con: duckdb.DuckDBPyConnection, overrides: dict, reason: str
) -> None:
    _add_raw(con, 2, **overrides)

    _run(con)

    assert con.execute("SELECT count(*) FROM staging.member_stg").fetchone() == (0,)
    rows = con.execute(
        "SELECT reason_code, source_table, file_row_number FROM dq.quarantine"
    ).fetchall()
    assert rows == [(reason, "RAW.MEMBER_FEED_RAW", 2)]


def test_quarantined_row_keeps_its_raw_payload(con: duckdb.DuckDBPyConnection) -> None:
    _add_raw(con, 2, member_id="", member_name="Elena")

    _run(con)

    payload = con.execute("SELECT raw_payload::VARCHAR FROM dq.quarantine").fetchone()[0]
    assert '"Elena"' in payload


# ---- keys, types, derived columns ------------------------------------------------------------


def test_member_key_is_source_system_colon_member_id(con: duckdb.DuckDBPyConnection) -> None:
    _add_raw(con, 2)
    _add_raw(con, 3, member_id="1", source_system="IND")
    _add_raw(con, 4, member_id="1", source_system="USA")

    _run(con)

    keys = con.execute("SELECT member_key FROM staging.member_stg ORDER BY member_key").fetchall()
    assert keys == [("IND:1",), ("SKYPOINTS:223457",), ("USA:1",)]


def test_leading_zeros_survive_in_text_columns(con: duckdb.DuckDBPyConnection) -> None:
    _add_raw(con, 2, member_id="00789", post_code="02134")

    _run(con, as_of=AS_OF)

    assert _staged(con, "member_id", "00789") == "00789"
    assert _staged(con, "post_code", "00789") == "02134"
    assert _staged(con, "member_key", "00789") == "SKYPOINTS:00789"


def test_dates_are_typed_and_dob_is_month_first(con: duckdb.DuckDBPyConnection) -> None:
    _add_raw(con, 2)

    _run(con)

    assert _staged(con, "enrollment_date") == date(2010, 10, 12)
    assert _staged(con, "last_flight_date") == date(2023, 12, 1)
    assert _staged(con, "date_of_birth") == date(1985, 3, 5)  # 03051985 = 5 March, not 3 May


def test_unreadable_optional_dates_become_null_with_a_flag(con: duckdb.DuckDBPyConnection) -> None:
    _add_raw(con, 2, date_of_birth="13051985", last_flight_date="NULL")

    _run(con)

    assert _staged(con, "date_of_birth") is None
    assert _staged(con, "dq_dob_invalid") is True
    assert _staged(con, "last_flight_date") is None
    assert _staged(con, "dq_last_flight_date_invalid") is True


@pytest.mark.parametrize(("as_of", "age"), [(date(2024, 3, 4), 38), (date(2024, 3, 5), 39)])
def test_age_is_whole_years_at_the_explicit_as_of_date(
    con: duckdb.DuckDBPyConnection, as_of: date, age: int
) -> None:
    _add_raw(con, 2)  # born 5 March 1985

    _run(con, as_of=as_of)

    assert _staged(con, "age") == age
    assert _staged(con, "as_of_date") == as_of


def test_age_is_null_without_a_date_of_birth(con: duckdb.DuckDBPyConnection) -> None:
    _add_raw(con, 2, date_of_birth="")

    _run(con)

    assert _staged(con, "age") is None


@pytest.mark.parametrize(
    ("last_flight", "stale"),
    [
        ("20231216", False),  # 30 days before as_of
        ("20231017", False),  # exactly 90 days before as_of
        ("20231016", True),  # 91 days before
        ("", None),  # no flight date: unknown, not stale
        ("20240116", None),  # flight date after as_of: bad data, unknown
    ],
)
def test_stale_member_is_more_than_90_days_before_as_of(
    con: duckdb.DuckDBPyConnection, last_flight: str, stale: bool | None
) -> None:
    _add_raw(con, 2, last_flight_date=last_flight)

    _run(con)

    assert _staged(con, "stale_member") is stale


def test_country_is_mapped_to_iso3_and_unknown_country_is_flagged(
    con: duckdb.DuckDBPyConnection,
) -> None:
    _add_raw(con, 2, member_id="1001", country="PHIL")
    _add_raw(con, 3, member_id="1002", country="ZZZ")
    _add_raw(con, 4, member_id="1003", country="")

    _run(con)

    assert _staged(con, "country_code", "1001") == "PHL"
    assert _staged(con, "dq_country_unknown", "1001") is False
    for member_id in ("1002", "1003"):
        assert _staged(con, "country_code", member_id) is None
        assert _staged(con, "dq_country_unknown", member_id) is True


# ---- scope and re-runs -----------------------------------------------------------------------


def test_header_rows_are_not_staged_or_quarantined(con: duckdb.DuckDBPyConnection) -> None:
    _add_raw(con, 1, record_type="H", member_name="Member_Name", member_id="Member_Id")
    _add_raw(con, 2)

    _run(con)

    assert con.execute("SELECT count(*) FROM staging.member_stg").fetchone() == (1,)
    assert con.execute("SELECT count(*) FROM dq.quarantine").fetchone() == (0,)


def test_only_the_requested_batch_is_processed(con: duckdb.DuckDBPyConnection) -> None:
    _add_raw(con, 2, batch_id="b1")
    _add_raw(con, 3, member_id="999999", batch_id="b2")

    _run(con, batch_id="b1")

    assert con.execute("SELECT member_id FROM staging.member_stg").fetchall() == [("223457",)]


def test_running_the_load_twice_does_not_duplicate_rows(con: duckdb.DuckDBPyConnection) -> None:
    _add_raw(con, 2)
    _add_raw(con, 3, member_id="")

    _run(con)
    _run(con)

    assert con.execute("SELECT count(*) FROM staging.member_stg").fetchone() == (1,)
    assert con.execute("SELECT count(*) FROM dq.quarantine").fetchone() == (1,)
