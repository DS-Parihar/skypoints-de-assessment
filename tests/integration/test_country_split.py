"""Integration tests: skypoints.transform.country_split (DuckDB).

Pipeline under test: staging -> merge_member_current -> CORE.MEMBER_CURRENT (latest record wins)
-> refresh_country_tables -> MART.MEMBERS_<ISO3> / MEMBERS_UNASSIGNED, plus the append-only
CORE.MEMBER_COUNTRY_HISTORY.

Most scenarios run the full chain through load_feed(): fixture .dat -> parser -> raw -> quality
runner -> staging. Cases a file cannot express (conflicting duplicates, ties in file_ts) insert
into staging directly.

Conventions:
  * "unassigned" (country_code NULL, or not in REF.COUNTRY_CODE_MAP) shows as None in a Move and
    as a NULL country code in the history table;
  * a record replaces the CORE row only if strictly newer by (file_ts, file_row_number);
  * every test ends by checking the one-table-per-member invariant (autouse fixture below).
"""

from collections import Counter
from datetime import date, datetime
from pathlib import Path

import duckdb
import pytest

from skypoints.ingest.flat_file import parse_member_feed
from skypoints.quality.runner import run_quality
from skypoints.transform.country_split import (
    AmbiguousLatestRecordError,
    BatchBlockedError,
    Move,
    merge_member_current,
    refresh_country_tables,
    split_by_country,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "split"
AS_OF = date(2024, 1, 15)
DAY = {
    1: "SKYPOINTS_MEMBERS_20240114_09000000.dat",
    2: "SKYPOINTS_MEMBERS_20240115_09000000.dat",
    3: "SKYPOINTS_MEMBERS_20240116_09000000.dat",
}
TS = {1: datetime(2024, 1, 14, 9), 2: datetime(2024, 1, 15, 9), 3: datetime(2024, 1, 16, 9)}
RAVI, ELENA = "SKYPOINTS:223458", "SKYPOINTS:223457"
UNASSIGNED = None
COUNTRY_TABLES = ["MEMBERS_AUS", "MEMBERS_CAN", "MEMBERS_IND", "MEMBERS_PHL", "MEMBERS_USA"]


# ---- helpers -------------------------------------------------------------------------------


def feed(scenario: str, day: int) -> Path:
    return FIXTURES / scenario / DAY[day]


def load_feed(
    con: duckdb.DuckDBPyConnection, path: Path, as_of: date = AS_OF, batch_id: str | None = None
):
    """Fixture .dat -> parser -> raw -> quality runner -> staging. The batch id defaults to the
    file name. A file already in raw is not inserted again (the loader's contract)."""
    return load_feeds(con, [path], as_of, batch_id or path.name)


def load_feeds(con: duckdb.DuckDBPyConnection, paths: list[Path], as_of: date, batch_id: str):
    headers = {}
    for path in paths:
        header, records, _ = parse_member_feed(path)
        headers[path.name] = header
        loaded = con.execute(
            "SELECT count(*) FROM raw.member_feed_raw WHERE file_name = ?", [path.name]
        ).fetchone()[0]
        if loaded:
            continue
        for r in records:
            key = f"SKYPOINTS:{r['member_id'].strip()}" if r["member_id"].strip() else None
            con.execute(
                "INSERT INTO raw.member_feed_raw (record_type, member_name, member_id, "
                "enrollment_date, last_flight_date, tier_code, agent_name, state, country, "
                "post_code, date_of_birth, active_member, source_system, member_key, file_name, "
                "file_row_number, file_ts, batch_id) "
                "VALUES ('D', ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, ?, 'SKYPOINTS', ?, ?, ?, ?, ?)",
                [
                    r["member_name"], r["member_id"], r["enrollment_date"], r["last_flight_date"],
                    r["tier_code"], r["agent_name"], r["state"], r["country"], r["post_code"],
                    r["date_of_birth"], r["active_member"], key, r["file_name"],
                    r["file_row_number"], r["file_ts"], batch_id,
                ],
            )  # fmt: skip
    return run_quality(con, batch_id, as_of, headers=headers)


def add_staging(
    con: duckdb.DuckDBPyConnection,
    member_id: str,
    country_code: str | None,
    *,
    file_name: str = "f.dat",
    row: int = 2,
    file_ts: datetime = TS[1],
    batch_id: str = "b1",
    member_name: str = "Ravi",
) -> None:
    """Insert one staging row directly (cases a file cannot express)."""
    con.execute(
        "INSERT INTO staging.member_stg (member_name, member_id, enrollment_date, tier_code, "
        "country, source_system, member_key, country_code, as_of_date, file_name, "
        "file_row_number, file_ts, batch_id) "
        "VALUES (?, ?, DATE '2010-01-01', 'GLD', ?, 'SKYPOINTS', ?, ?, DATE '2024-01-15', ?, ?, ?, ?)",
        [member_name, member_id, country_code, f"SKYPOINTS:{member_id}", country_code,
         file_name, row, file_ts, batch_id],
    )  # fmt: skip


def mart_tables(con: duckdb.DuckDBPyConnection) -> list[str]:
    rows = con.execute(
        "SELECT upper(table_name) FROM information_schema.tables "
        "WHERE lower(table_schema) = 'mart' AND upper(table_name) LIKE 'MEMBERS\\_%' ESCAPE '\\' "
        "ORDER BY 1"
    ).fetchall()
    return [r[0] for r in rows]


def tables_holding(con: duckdb.DuckDBPyConnection, member_key: str) -> list[str]:
    return [
        table
        for table in mart_tables(con)
        if con.execute(f"SELECT count(*) FROM mart.{table} WHERE member_key = ?", [member_key])
        .fetchone()[0]
    ]


def core_row(con: duckdb.DuckDBPyConnection, member_key: str) -> tuple:
    rows = con.execute(
        "SELECT country_code, file_ts, file_row_number, file_name FROM core.member_current "
        "WHERE member_key = ?",
        [member_key],
    ).fetchall()
    assert len(rows) == 1, f"expected exactly one CORE row for {member_key}, found {len(rows)}"
    return rows[0]


def history(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    return con.execute(
        "SELECT member_key, old_country_code, new_country_code, effective_file_ts, batch_id "
        "FROM core.member_country_history ORDER BY effective_file_ts, member_key"
    ).fetchall()


def snapshot(con: duckdb.DuckDBPyConnection) -> dict[str, list[tuple]]:
    """Every table's rows, without audit-only columns, for 'nothing changed' assertions."""
    spec = {
        "raw.member_feed_raw": "loaded_at",
        "staging.member_stg": "staged_at",
        "dq.quarantine": "quarantine_id, quarantined_at",
        "dq.row_finding": "finding_id, created_at",
        "dq.run_summary": "batch_id",  # replaced per run; only the counts matter
        "core.member_current": "updated_at",
        "core.member_country_history": "recorded_at",
    }
    for table in mart_tables(con):
        spec[f"mart.{table}"] = "updated_at"
    return {
        name: con.execute(f"SELECT * EXCLUDE ({exclude}) FROM {name} ORDER BY ALL").fetchall()
        for name, exclude in spec.items()
    }


def final_state(con: duckdb.DuckDBPyConnection) -> list[tuple]:
    """Who ends up where, from which record (ignores batch ids and audit columns)."""
    rows = []
    for table in mart_tables(con):
        for key, country_code, file_ts, row_number, file_name in con.execute(
            f"SELECT member_key, country_code, file_ts, file_row_number, file_name FROM mart.{table}"
        ).fetchall():
            rows.append((table, key, country_code, file_ts, row_number, file_name))
    return sorted(rows, key=str)


@pytest.fixture(autouse=True)
def one_country_table_per_member(con: duckdb.DuckDBPyConnection):
    """Invariant checked after EVERY test: each member_key in CORE.MEMBER_CURRENT appears in
    exactly one MART.MEMBERS_* table, and no table holds a key that is not in CORE."""
    yield
    core_keys = {
        k for (k,) in con.execute("SELECT member_key FROM core.member_current").fetchall()
    }
    seen: Counter = Counter()
    for table in mart_tables(con):
        for (key,) in con.execute(f"SELECT member_key FROM mart.{table}").fetchall():
            seen[key] += 1
    assert not (set(seen) - core_keys), f"keys in MART but not in CORE: {set(seen) - core_keys}"
    assert {k: seen[k] for k in core_keys} == dict.fromkeys(core_keys, 1), (
        "every CORE member must be in exactly one country table: "
        f"{ {k: seen[k] for k in core_keys if seen[k] != 1} }"
    )


# ---- 1. a member moves country -------------------------------------------------------------


def run_day1(con):
    load_feed(con, feed("move", 1))
    return split_by_country(con)


def run_day2(con):
    run_day1(con)
    load_feed(con, feed("move", 2))
    return split_by_country(con)


def test_move_after_day1_ravi_is_in_india_only(con) -> None:
    after_day1 = run_day1(con)
    assert tables_holding(con, RAVI) == ["MEMBERS_IND"]


def test_move_first_appearance_is_not_a_move(con) -> None:
    after_day1 = run_day1(con)
    assert after_day1.moves == []
    assert history(con) == []
    assert (after_day1.inserted, after_day1.updated, after_day1.stale_ignored) == (2, 0, 0)


def test_move_after_day2_ravi_is_in_usa_only(con) -> None:
    after_day2 = run_day2(con)
    assert tables_holding(con, RAVI) == ["MEMBERS_USA"]


def test_move_member_current_has_one_row_for_ravi_from_day2(con) -> None:
    after_day2 = run_day2(con)
    country_code, file_ts, _, file_name = core_row(con, RAVI)

    assert (country_code, file_ts, file_name) == ("USA", TS[2], DAY[2])


def test_move_history_has_one_row_india_to_usa_dated_day2(con) -> None:
    after_day2 = run_day2(con)
    assert history(con) == [(RAVI, "IND", "USA", TS[2], DAY[2])]


def test_move_result_lists_the_move(con) -> None:
    after_day2 = run_day2(con)
    assert after_day2.moves == [Move(RAVI, "IND", "USA", TS[2], DAY[2])]
    assert (after_day2.inserted, after_day2.updated) == (0, 1)


def test_move_members_who_did_not_move_are_untouched(con) -> None:
    after_day2 = run_day2(con)
    assert tables_holding(con, ELENA) == ["MEMBERS_USA"]
    assert core_row(con, ELENA)[1] == TS[1]


def test_move_rows_per_table_covers_every_country_table_and_unassigned(con) -> None:
    after_day1 = run_day1(con)
    assert after_day1.rows_per_table == {
        "MEMBERS_AUS": 0, "MEMBERS_CAN": 0, "MEMBERS_IND": 1, "MEMBERS_PHL": 0,
        "MEMBERS_USA": 1, "MEMBERS_UNASSIGNED": 0,
    }  # fmt: skip


# ---- 2. the same member twice in one file ---------------------------------------------------


def test_same_file_duplicate_dq004_quarantines_the_earlier_row_and_ravi_ends_in_usa_only(con) -> None:
    load_feed(con, feed("same_file", 1))

    split_by_country(con)

    assert con.execute("SELECT file_row_number, rule_code FROM dq.quarantine").fetchall() == [
        (2, "DQ004")
    ]
    assert tables_holding(con, RAVI) == ["MEMBERS_USA"]
    assert history(con) == []


def test_same_file_safety_net_the_higher_row_number_wins_when_staging_has_both(con) -> None:
    add_staging(con, "223458", "IND", row=2)
    add_staging(con, "223458", "USA", row=3)

    result = split_by_country(con)

    assert core_row(con, RAVI)[:3] == ("USA", TS[1], 3)
    assert tables_holding(con, RAVI) == ["MEMBERS_USA"]
    assert (result.inserted, result.updated) == (1, 0)


# ---- 3. idempotency --------------------------------------------------------------------------


def test_running_the_split_twice_changes_nothing(con) -> None:
    after_day1 = run_day1(con)
    before = snapshot(con)

    second = split_by_country(con)

    assert snapshot(con) == before
    assert (second.inserted, second.updated) == (0, 0)
    assert second.moves == []
    assert second.rows_per_table == after_day1.rows_per_table


def test_running_the_split_twice_adds_no_history_rows_after_a_move(con) -> None:
    after_day2 = run_day2(con)
    split_by_country(con)
    split_by_country(con)

    assert history(con) == [(RAVI, "IND", "USA", TS[2], DAY[2])]


def test_reloading_the_whole_day1_file_through_the_chain_changes_no_table(con) -> None:
    after_day1 = run_day1(con)
    before = snapshot(con)

    load_feed(con, feed("move", 1))
    after_reload = snapshot(con)
    split_by_country(con)

    assert after_reload == before, "raw / staging / quarantine / findings changed on reload"
    assert snapshot(con) == before


# ---- 4. late arrival -------------------------------------------------------------------------


def test_late_arrival_an_older_file_does_not_move_ravi_back(con) -> None:
    load_feed(con, feed("move", 2))
    split_by_country(con, DAY[2])
    load_feed(con, feed("move", 1))

    result = split_by_country(con, DAY[1])

    assert tables_holding(con, RAVI) == ["MEMBERS_USA"]
    assert core_row(con, RAVI)[1] == TS[2]
    assert history(con) == []
    assert result.stale_ignored == 1
    assert result.moves == []


def test_late_arrival_members_only_in_the_late_file_are_still_added(con) -> None:
    load_feed(con, feed("move", 2))
    split_by_country(con, DAY[2])
    load_feed(con, feed("move", 1))

    result = split_by_country(con, DAY[1])

    assert tables_holding(con, ELENA) == ["MEMBERS_USA"]
    assert result.inserted == 1


# ---- 5. unassigned ---------------------------------------------------------------------------


@pytest.mark.parametrize(
    "member_key",
    [pytest.param("SKYPOINTS:223460", id="Zed-country-XYZ"),
     pytest.param("SKYPOINTS:223461", id="Nul-blank-country")],
)
def test_unknown_or_blank_country_goes_to_unassigned_only(con, member_key: str) -> None:
    load_feed(con, feed("unassigned", 1))

    result = split_by_country(con)

    assert tables_holding(con, member_key) == ["MEMBERS_UNASSIGNED"]
    assert result.rows_per_table["MEMBERS_UNASSIGNED"] == 2


def test_a_country_code_missing_from_the_ref_map_is_unassigned(con) -> None:
    add_staging(con, "223465", "BRA")  # a code, but not in REF.COUNTRY_CODE_MAP

    split_by_country(con)

    assert tables_holding(con, "SKYPOINTS:223465") == ["MEMBERS_UNASSIGNED"]


# ---- 6. aliases ------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("member_id", "table"),
    [pytest.param("223462", "MEMBERS_AUS", id="Jacob-AU"),
     pytest.param("223459", "MEMBERS_PHL", id="Mateo-PHIL")],
)
def test_an_alias_country_routes_to_its_iso_table(con, member_id: str, table: str) -> None:
    load_feed(con, feed("aliases", 1))

    split_by_country(con)

    assert tables_holding(con, f"SKYPOINTS:{member_id}") == [table]


# ---- 7. move back ----------------------------------------------------------------------------


def _load_and_split_each_day(con, scenario: str, days=(1, 2, 3)) -> None:
    for day in days:
        load_feed(con, feed(scenario, day))
        split_by_country(con)


def test_move_back_ends_in_india_only(con) -> None:
    _load_and_split_each_day(con, "move_back")

    assert tables_holding(con, RAVI) == ["MEMBERS_IND"]
    assert core_row(con, RAVI)[1] == TS[3]


def test_move_back_history_has_both_moves(con) -> None:
    _load_and_split_each_day(con, "move_back")

    assert history(con) == [
        (RAVI, "IND", "USA", TS[2], DAY[2]),
        (RAVI, "USA", "IND", TS[3], DAY[3]),
    ]


def test_all_three_files_in_one_batch_end_where_one_at_a_time_ends(tmp_path) -> None:
    one_at_a_time = duckdb_with_schema()
    _load_and_split_each_day(one_at_a_time, "move_back")
    one_batch = duckdb_with_schema()
    load_feeds(one_batch, [feed("move_back", d) for d in (1, 2, 3)], AS_OF, "all-three")
    split_by_country(one_batch, "all-three")

    assert final_state(one_batch) == final_state(one_at_a_time)
    one_at_a_time.close()
    one_batch.close()


def test_moves_inside_one_batch_collapse_to_a_single_history_row(con) -> None:
    load_feed(con, feed("collapse", 1))
    split_by_country(con, DAY[1])  # Ravi is in IND
    load_feeds(con, [feed("collapse", 2), feed("collapse", 3)], AS_OF, "day2-and-day3")

    result = split_by_country(con, "day2-and-day3")  # USA on day2, CAN on day3, one batch

    assert tables_holding(con, RAVI) == ["MEMBERS_CAN"]
    assert history(con) == [(RAVI, "IND", "CAN", TS[3], DAY[3])]
    assert result.moves == [Move(RAVI, "IND", "CAN", TS[3], DAY[3])]


# ---- 8. latest record has a blank country ----------------------------------------------------


def test_documented_decision_latest_record_with_blank_country_moves_member_to_unassigned(con) -> None:
    """Latest wins even when the newest record has no country: the member leaves their old
    country table and is shown as unassigned. This is a documented decision, not an accident."""
    load_feed(con, feed("blank_country", 1))
    split_by_country(con)
    assert tables_holding(con, RAVI) == ["MEMBERS_IND"]

    load_feed(con, feed("blank_country", 2))
    result = split_by_country(con)

    assert tables_holding(con, RAVI) == ["MEMBERS_UNASSIGNED"]
    assert result.moves == [Move(RAVI, "IND", UNASSIGNED, TS[2], DAY[2])]
    assert history(con) == [(RAVI, "IND", None, TS[2], DAY[2])]


def test_moving_from_unassigned_to_a_country_is_a_move_too(con) -> None:
    add_staging(con, "223458", None, file_name="a.dat", row=2, file_ts=TS[1], batch_id="b1")
    split_by_country(con, "b1")
    add_staging(con, "223458", "IND", file_name="b.dat", row=2, file_ts=TS[2], batch_id="b2")

    result = split_by_country(con, "b2")

    assert result.moves == [Move(RAVI, UNASSIGNED, "IND", TS[2], "b2")]
    assert tables_holding(con, RAVI) == ["MEMBERS_IND"]


# ---- 9. config-driven countries ------------------------------------------------------------


def add_japan(con: duckdb.DuckDBPyConnection) -> None:
    con.execute(
        "INSERT INTO ref.country_code_map (raw_country_code, country_code, country_name) "
        "VALUES ('JPN', 'JPN', 'Japan')"
    )


def test_the_quality_runner_resolves_countries_from_ref_country_code_map(con) -> None:
    """Prerequisite for the config-driven split: a country added to REF.COUNTRY_CODE_MAP must be
    resolved by the quality runner too, so staging.country_code is 'JPN' and not NULL."""
    add_japan(con)

    load_feed(con, feed("jpn", 1))

    assert con.execute("SELECT country_code FROM staging.member_stg").fetchall() == [("JPN",)]


def test_a_country_added_to_the_ref_map_gets_its_own_table_without_a_code_change(con) -> None:
    add_japan(con)
    add_staging(con, "223463", "JPN", member_name="Kenji")

    result = split_by_country(con)

    assert "MEMBERS_JPN" in mart_tables(con)
    assert tables_holding(con, "SKYPOINTS:223463") == ["MEMBERS_JPN"]
    assert result.rows_per_table["MEMBERS_JPN"] == 1


def test_an_inactive_ref_country_is_not_a_target_table(con) -> None:
    con.execute(
        "INSERT INTO ref.country_code_map (raw_country_code, country_code, country_name, is_active) "
        "VALUES ('JPN', 'JPN', 'Japan', FALSE)"
    )
    add_staging(con, "223463", "JPN", member_name="Kenji")

    split_by_country(con)

    assert tables_holding(con, "SKYPOINTS:223463") == ["MEMBERS_UNASSIGNED"]


# ---- 10. rejected rows never get that far ---------------------------------------------------


def test_a_rejected_row_never_reaches_core_or_a_country_table(con) -> None:
    load_feed(con, feed("rejected", 1))

    result = split_by_country(con)

    assert con.execute("SELECT rule_code FROM dq.quarantine").fetchall() == [("DQ001",)]
    assert tables_holding(con, "SKYPOINTS:223464") == []
    assert con.execute("SELECT member_key FROM core.member_current").fetchall() == [(RAVI,)]
    assert result.inserted == 1


# ---- 11. blocked batches and conflicting duplicates ------------------------------------------


def test_a_batch_with_a_fail_batch_from_the_runner_is_refused_and_nothing_is_written(con) -> None:
    load_feed(con, feed("move", 1))
    con.execute("UPDATE staging.member_stg SET agent_name = NULL")  # staging loses a column
    header = parse_member_feed(feed("move", 1))[0]
    summary = run_quality(con, DAY[1], AS_OF, headers={DAY[1]: header})
    assert summary.promotion_blocked  # the runner recorded a FAIL_BATCH finding (DQ016)

    with pytest.raises(BatchBlockedError):
        split_by_country(con, DAY[1])

    assert con.execute("SELECT count(*) FROM core.member_current").fetchone() == (0,)
    assert con.execute("SELECT count(*) FROM core.member_country_history").fetchone() == (0,)
    assert all(
        con.execute(f"SELECT count(*) FROM mart.{t}").fetchone() == (0,) for t in mart_tables(con)
    )


def test_a_fail_batch_finding_blocks_only_its_own_batch(con) -> None:
    add_staging(con, "223458", "IND", file_name="a.dat", batch_id="blocked")
    add_staging(con, "223457", "USA", file_name="b.dat", batch_id="clean")
    con.execute(
        "INSERT INTO dq.row_finding (batch_id, rule_code, severity, reason) "
        "VALUES ('blocked', 'DQ015', 'FAIL_BATCH', 'raw rows differ from staged plus rejected')"
    )

    with pytest.raises(BatchBlockedError):
        split_by_country(con, "blocked")
    result = split_by_country(con, "clean")

    assert result.inserted == 1
    assert tables_holding(con, ELENA) == ["MEMBERS_USA"]
    assert tables_holding(con, RAVI) == []


def test_documented_decision_without_a_batch_id_any_blocked_batch_refuses_the_whole_run(con) -> None:
    add_staging(con, "223458", "IND", file_name="a.dat", batch_id="blocked")
    add_staging(con, "223457", "USA", file_name="b.dat", batch_id="clean")
    con.execute(
        "INSERT INTO dq.row_finding (batch_id, rule_code, severity, reason) "
        "VALUES ('blocked', 'DQ015', 'FAIL_BATCH', 'x')"
    )

    with pytest.raises(BatchBlockedError):
        split_by_country(con)

    assert con.execute("SELECT count(*) FROM core.member_current").fetchone() == (0,)


def test_conflicting_duplicates_raise_and_write_nothing_for_any_member(con) -> None:
    add_staging(con, "223458", "IND", file_name="a.dat", row=2, file_ts=TS[1])
    add_staging(con, "223458", "USA", file_name="b.dat", row=2, file_ts=TS[1])  # same ts and row
    add_staging(con, "223457", "USA", file_name="c.dat", row=2, file_ts=TS[1], member_name="Elena")

    with pytest.raises(AmbiguousLatestRecordError):
        split_by_country(con)

    assert con.execute("SELECT count(*) FROM core.member_current").fetchone() == (0,)
    assert con.execute("SELECT count(*) FROM core.member_country_history").fetchone() == (0,)
    assert all(
        con.execute(f"SELECT count(*) FROM mart.{t}").fetchone() == (0,) for t in mart_tables(con)
    )


def test_identical_duplicates_collapse_silently(con) -> None:
    add_staging(con, "223458", "IND", file_name="a.dat", row=2, file_ts=TS[1])
    add_staging(con, "223458", "IND", file_name="b.dat", row=2, file_ts=TS[1])  # same content

    result = split_by_country(con)

    assert result.inserted == 1
    assert tables_holding(con, RAVI) == ["MEMBERS_IND"]
    assert con.execute("SELECT count(*) FROM core.member_current").fetchone() == (1,)


def test_conflicting_duplicates_against_an_existing_core_row_also_write_nothing(con) -> None:
    add_staging(con, "223458", "CAN", file_name="old.dat", row=2, file_ts=TS[1], batch_id="b0")
    split_by_country(con, "b0")
    before = snapshot(con)
    add_staging(con, "223458", "IND", file_name="a.dat", row=2, file_ts=TS[2], batch_id="b1")
    add_staging(con, "223458", "USA", file_name="b.dat", row=2, file_ts=TS[2], batch_id="b1")
    staging_after_setup = snapshot(con)["staging.member_stg"]

    with pytest.raises(AmbiguousLatestRecordError):
        split_by_country(con, "b1")

    after = snapshot(con)
    assert after["core.member_current"] == before["core.member_current"]
    assert after["core.member_country_history"] == before["core.member_country_history"]
    assert after["staging.member_stg"] == staging_after_setup
    assert tables_holding(con, RAVI) == ["MEMBERS_CAN"]


# ---- strictly newer: (file_ts, then file_row_number) ------------------------------------------


@pytest.mark.parametrize(
    ("file_ts", "row", "country", "updated", "stale"),
    [
        pytest.param(TS[2], 6, "USA", 1, 0, id="same-file_ts-higher-row-wins"),
        pytest.param(TS[3], 1, "USA", 1, 0, id="newer-file_ts-wins-whatever-the-row"),
        pytest.param(TS[2], 4, "IND", 0, 1, id="same-file_ts-lower-row-is-ignored"),
        pytest.param(TS[1], 9, "IND", 0, 1, id="older-file_ts-is-ignored-whatever-the-row"),
    ],
)
def test_a_record_replaces_core_only_if_strictly_newer(
    con, file_ts: datetime, row: int, country: str, updated: int, stale: int
) -> None:
    add_staging(con, "223458", "IND", file_name="a.dat", row=5, file_ts=TS[2], batch_id="b1")
    split_by_country(con, "b1")
    add_staging(con, "223458", "USA", file_name="b.dat", row=row, file_ts=file_ts, batch_id="b2")

    result = split_by_country(con, "b2")

    assert tables_holding(con, RAVI) == [f"MEMBERS_{'USA' if country == 'USA' else 'IND'}"]
    assert (result.updated, result.stale_ignored) == (updated, stale)
    assert core_row(con, RAVI)[0] == country


# ---- the two steps on their own --------------------------------------------------------------


def test_merge_member_current_alone_fills_core_and_leaves_the_country_tables_empty(con) -> None:
    load_feed(con, feed("move", 1))

    result = merge_member_current(con)

    assert (result.inserted, result.updated, result.stale_ignored) == (2, 0, 0)
    assert con.execute("SELECT count(*) FROM core.member_current").fetchone() == (2,)
    assert all(
        con.execute(f"SELECT count(*) FROM mart.{t}").fetchone() == (0,) for t in mart_tables(con)
    )


def test_refresh_country_tables_rebuilds_the_tables_from_core(con) -> None:
    load_feed(con, feed("move", 1))
    merge_member_current(con)

    counts = refresh_country_tables(con)

    assert counts["MEMBERS_IND"] == 1 and counts["MEMBERS_USA"] == 1
    assert tables_holding(con, RAVI) == ["MEMBERS_IND"]


def test_refresh_country_tables_removes_a_member_from_the_old_table_when_core_changes(con) -> None:
    load_feed(con, feed("move", 1))
    split_by_country(con)
    con.execute("UPDATE core.member_current SET country_code = 'CAN' WHERE member_key = ?", [RAVI])

    counts = refresh_country_tables(con)

    assert tables_holding(con, RAVI) == ["MEMBERS_CAN"]
    assert counts["MEMBERS_IND"] == 0 and counts["MEMBERS_CAN"] == 1


# ---- helper local to this module --------------------------------------------------------------


def duckdb_with_schema() -> duckdb.DuckDBPyConnection:
    from skypoints.db import create_database

    return create_database()
