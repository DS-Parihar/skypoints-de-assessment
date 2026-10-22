"""Fixtures for the quality runner integration tests (DuckDB, raw rows inserted directly)."""

from datetime import datetime

import duckdb
import pytest

from skypoints.db import create_database

DEFAULT_FILE = "SKYPOINTS_MEMBERS_20240115_13453025.dat"
DEFAULT_FILE_TS = datetime(2024, 1, 15, 13, 45, 30, 250000)


@pytest.fixture
def con() -> duckdb.DuckDBPyConnection:
    connection = create_database()
    yield connection
    connection.close()


@pytest.fixture
def add_raw(con: duckdb.DuckDBPyConnection):
    """Insert one detail row into RAW.MEMBER_FEED_RAW. Defaults make a valid member."""

    def _add(row_number: int = 2, **overrides) -> None:
        values = dict(
            member_name="Elena", member_id="223457", enrollment_date="20101012",
            last_flight_date="20121013", tier_code="GLD", agent_name="Sam", state="CA",
            country="USA", post_code="02134", date_of_birth="03051985", active_member="A",
            source_system="SKYPOINTS", file_name=DEFAULT_FILE, file_ts=DEFAULT_FILE_TS,
            batch_id="b1",
        )  # fmt: skip
        values.update(overrides)
        member_id = values["member_id"]
        key = f"{values['source_system']}:{member_id.strip()}" if member_id and member_id.strip() else None
        con.execute(
            "INSERT INTO raw.member_feed_raw (record_type, member_name, member_id, enrollment_date, "
            "last_flight_date, tier_code, agent_name, state, country, post_code, date_of_birth, "
            "active_member, source_system, member_key, file_name, file_row_number, file_ts, batch_id) "
            "VALUES ('D', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?)",
            [
                values["member_name"], member_id, values["enrollment_date"],
                values["last_flight_date"], values["tier_code"], values["agent_name"],
                values["state"], values["country"], values["post_code"], values["date_of_birth"],
                values["active_member"], values["source_system"], key, values["file_name"],
                row_number, values["file_ts"], values["batch_id"],
            ],
        )  # fmt: skip

    return _add
