"""Integration test: running the sql/duckdb DDL creates every expected table and column."""

from pathlib import Path

import duckdb
import pytest

from skypoints.db import DEFAULT_DDL_DIR, create_database, ddl_files

REPO_ROOT = Path(__file__).resolve().parents[1]

MEMBER_BUSINESS_COLUMNS = [
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
DQ_FLAGS = [
    "dq_dob_invalid",
    "dq_last_flight_date_invalid",
    "dq_country_unknown",
    "dq_post_code_invalid",
    "dq_member_id_short",
    "dq_active_flag_unknown",
]
KEYS = ["source_system", "member_key"]  # member_key = source_system || ':' || member_id
DERIVED = ["country_code", "as_of_date", "age", "stale_member"]
LINEAGE = ["file_name", "file_row_number", "file_ts", "batch_id"]

MEMBER_CURRENT_COLUMNS = [
    *MEMBER_BUSINESS_COLUMNS,
    *KEYS,
    *DERIVED,
    *DQ_FLAGS,
    *LINEAGE,
    "updated_at",
]

# (schema, table) -> column names in order, lower-case.
EXPECTED_COLUMNS: dict[tuple[str, str], list[str]] = {
    ("raw", "member_feed_raw"): [
        "record_type",
        *MEMBER_BUSINESS_COLUMNS,
        *KEYS,
        *LINEAGE,
        "loaded_at",
    ],
    ("raw", "redemption_feed_raw"): ["payload", *LINEAGE, "loaded_at"],
    ("staging", "member_stg"): [
        *MEMBER_BUSINESS_COLUMNS,
        *KEYS,
        *DERIVED,
        *DQ_FLAGS,
        *LINEAGE,
        "staged_at",
    ],
    ("core", "member_current"): MEMBER_CURRENT_COLUMNS,
    ("staging", "member_prepared_v"): [
        *MEMBER_BUSINESS_COLUMNS,
        *KEYS,
        "country_code",
        *DQ_FLAGS,
        "reject_reason",
        *LINEAGE,
        "raw_payload",
    ],
    ("core", "member_country_history"): [
        "member_key",
        "member_id",
        "old_country_code",
        "new_country_code",
        "effective_file_ts",
        "batch_id",
        "recorded_at",
    ],
    ("dq", "quarantine"): [
        "quarantine_id",
        "source_table",
        "file_name",
        "file_row_number",
        "batch_id",
        "reason_code",
        "reason_detail",
        "raw_payload",
        "quarantined_at",
    ],
    ("dq", "member_header_check"): ["file_name", "batch_id", "header_ok"],
    ("ref", "country_code_map"): [
        "raw_country_code",
        "country_code",
        "country_name",
        "is_active",
    ],
    **{
        ("mart", f"members_{iso3}"): MEMBER_CURRENT_COLUMNS
        for iso3 in ("usa", "ind", "can", "phl", "aus", "unassigned")
    },
}


def _columns(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> list[str]:
    rows = con.execute(
        "SELECT lower(column_name) FROM information_schema.columns "
        "WHERE lower(table_schema) = ? AND lower(table_name) = ? "
        "ORDER BY ordinal_position",
        [schema, table],
    ).fetchall()
    return [r[0] for r in rows]


def _types(con: duckdb.DuckDBPyConnection, schema: str, table: str) -> dict[str, str]:
    rows = con.execute(
        "SELECT lower(column_name), data_type FROM information_schema.columns "
        "WHERE lower(table_schema) = ? AND lower(table_name) = ?",
        [schema, table],
    ).fetchall()
    return dict(rows)


@pytest.fixture(scope="module")
def con() -> duckdb.DuckDBPyConnection:
    connection = create_database()
    yield connection
    connection.close()


def test_default_ddl_dir_is_sql_duckdb() -> None:
    assert DEFAULT_DDL_DIR == REPO_ROOT / "sql" / "duckdb"


def test_ddl_files_are_returned_in_numeric_order() -> None:
    names = [p.name for p in ddl_files(DEFAULT_DDL_DIR)]

    assert names == [
        "00_setup.sql",
        "01_raw.sql",
        "02_staging.sql",
        "03_targets.sql",
        "04_member_prepared.sql",
    ]


def test_only_numbered_files_are_ddl(tmp_path: Path) -> None:
    (tmp_path / "00_a.sql").write_text("SELECT 1;", encoding="utf-8")
    (tmp_path / "load_staging.sql").write_text("SELECT $x;", encoding="utf-8")
    (tmp_path / "notes.txt").write_text("-", encoding="utf-8")

    assert [p.name for p in ddl_files(tmp_path)] == ["00_a.sql"]


def test_ddl_files_rejects_a_missing_directory(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        ddl_files(tmp_path / "does_not_exist")


@pytest.mark.parametrize(("schema", "table"), sorted(EXPECTED_COLUMNS))
def test_table_has_expected_columns(
    con: duckdb.DuckDBPyConnection, schema: str, table: str
) -> None:
    assert _columns(con, schema, table) == EXPECTED_COLUMNS[(schema, table)]


def test_no_unexpected_tables(con: duckdb.DuckDBPyConnection) -> None:
    found = {
        (s.lower(), t.lower())
        for s, t in con.execute(
            "SELECT table_schema, table_name FROM information_schema.tables "
            "WHERE table_schema NOT IN ('information_schema', 'main', 'pg_catalog')"
        ).fetchall()
    }

    assert found == set(EXPECTED_COLUMNS)


def test_raw_business_columns_are_all_text(con: duckdb.DuckDBPyConnection) -> None:
    types = _types(con, "raw", "member_feed_raw")

    for column in ["record_type", *MEMBER_BUSINESS_COLUMNS]:
        assert types[column] == "VARCHAR", column


def test_staging_keeps_post_code_as_text_and_dates_typed(
    con: duckdb.DuckDBPyConnection,
) -> None:
    types = _types(con, "staging", "member_stg")

    assert types["post_code"] == "VARCHAR"
    for column in ("enrollment_date", "last_flight_date", "date_of_birth", "as_of_date"):
        assert types[column] == "DATE", column
    assert types["stale_member"] == "BOOLEAN"


def test_country_map_is_seeded(con: duckdb.DuckDBPyConnection) -> None:
    mapping = dict(
        con.execute("SELECT raw_country_code, country_code FROM ref.country_code_map").fetchall()
    )

    assert mapping["PHIL"] == "PHL"
    assert mapping["AU"] == "AUS"
    assert set(mapping.values()) == {"USA", "IND", "CAN", "PHL", "AUS"}


def test_running_the_ddl_twice_is_safe_and_keeps_seed_rows() -> None:
    connection = create_database()
    before = connection.execute("SELECT count(*) FROM ref.country_code_map").fetchone()

    for path in ddl_files(DEFAULT_DDL_DIR):
        connection.execute(path.read_text(encoding="utf-8"))

    assert connection.execute("SELECT count(*) FROM ref.country_code_map").fetchone() == before
    connection.close()


def test_file_database_persists_tables(tmp_path: Path) -> None:
    db_file = tmp_path / "skypoints.duckdb"
    create_database(db_file).close()

    reopened = duckdb.connect(str(db_file))
    try:
        assert _columns(reopened, "core", "member_current") == MEMBER_CURRENT_COLUMNS
    finally:
        reopened.close()


def test_custom_ddl_dir_is_used(tmp_path: Path) -> None:
    (tmp_path / "01_b.sql").write_text("CREATE TABLE t_b (x INTEGER);", encoding="utf-8")
    (tmp_path / "00_a.sql").write_text("CREATE TABLE t_a (x INTEGER);", encoding="utf-8")
    (tmp_path / "02_c.sql").write_text(
        "INSERT INTO t_b SELECT x FROM t_a;", encoding="utf-8"
    )  # fails unless 00 and 01 ran first

    connection = create_database(ddl_dir=tmp_path)

    tables = {r[0] for r in connection.execute("SHOW TABLES").fetchall()}
    assert tables == {"t_a", "t_b"}
    connection.close()


@pytest.mark.parametrize(
    ("schema", "table"),
    [
        ("raw", "member_feed_raw"),
        ("staging", "member_stg"),
        ("core", "member_current"),
        ("mart", "members_usa"),
        ("mart", "members_unassigned"),
    ],
)
def test_source_system_and_member_key_exist_and_are_text(
    con: duckdb.DuckDBPyConnection, schema: str, table: str
) -> None:
    types = _types(con, schema, table)

    assert types["source_system"] == "VARCHAR"
    assert types["member_key"] == "VARCHAR"


def test_member_key_is_the_primary_key_of_member_current(con: duckdb.DuckDBPyConnection) -> None:
    rows = con.execute(
        "SELECT constraint_column_names FROM duckdb_constraints() "
        "WHERE lower(table_name) = 'member_current' AND constraint_type = 'PRIMARY KEY'"
    ).fetchall()

    assert [list(r[0]) for r in rows] == [["MEMBER_KEY"]]


def test_member_id_alone_is_not_unique_in_member_current() -> None:
    connection = create_database()
    row = (
        "INSERT INTO core.member_current (member_name, member_id, enrollment_date, source_system, "
        "member_key, as_of_date, stale_member, file_name, file_row_number, file_ts, batch_id) "
        "VALUES ('A', '1', DATE '2022-01-01', '{sys}', '{sys}:1', DATE '2024-01-15', TRUE, "
        "'f', 1, TIMESTAMP '2024-01-15 00:00:00', 'b')"
    )
    connection.execute(row.format(sys="IND"))
    connection.execute(row.format(sys="USA"))  # same member_id 1, different source: allowed

    assert connection.execute("SELECT count(*) FROM core.member_current").fetchone() == (2,)
    with pytest.raises(duckdb.ConstraintException):
        connection.execute(row.format(sys="USA"))  # same member_key twice: rejected
    connection.close()
