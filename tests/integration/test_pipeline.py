"""End-to-end tests: python -m skypoints.pipeline on fixture feeds.

Fixture raw folder (tests/fixtures/pipeline/raw), as_of 2024-01-15:
  SKYPOINTS_MEMBERS_20240114_09000000.dat  6 rows: Ravi IND, Elena USA, Mateo PHIL (alias), Zed XYZ
                                           (unassigned), a blank-name row (rejected, file row 6),
                                           Nora with a short id (warning)
  SKYPOINTS_MEMBERS_20240115_09000000.dat  2 rows: Ravi USA (he moves), Jacob AU (alias)
  SKYPOINTS_REDEMPTIONS_20240115_...json   4 members: 223457 with 3 transactions (one negative),
                                           223458 with 1, 999999 (not a member: orphan) with 1,
                                           223459 with an empty array
  stray.csv and notes.txt: not in the manifest and not a feed name: listed as skipped
The shared run is cached, so the many assertions cost one pipeline run. Nothing here mutates it.
"""

import re
import shutil
import subprocess
import sys
import tempfile
from datetime import date, datetime
from functools import cache
from pathlib import Path

import duckdb
import pytest

from skypoints.pipeline import LAYER_KEYS, RunReport, format_report, main, run_pipeline

REPO = Path(__file__).resolve().parents[2]
FIXTURES = REPO / "tests" / "fixtures" / "pipeline"
RAW = FIXTURES / "raw"
AS_OF = date(2024, 1, 15)
DAY1 = "SKYPOINTS_MEMBERS_20240114_09000000.dat"
DAY2 = "SKYPOINTS_MEMBERS_20240115_09000000.dat"
RED = "SKYPOINTS_REDEMPTIONS_20240115_09000000.json"
SHARED_DIR = Path(tempfile.mkdtemp(prefix="skypoints_pipeline_test_"))


@cache
def ran() -> tuple[RunReport, Path, str]:
    """One pipeline run on the fixture folder. Not cached while it raises, so every test that
    needs it fails on its own until the pipeline works."""
    db_path = SHARED_DIR / "skypoints.duckdb"
    report = run_pipeline(RAW, db_path, AS_OF)
    return report, db_path, format_report(report)


def query(sql: str, params: list | None = None) -> list[tuple]:
    _, db_path, _ = ran()
    con = duckdb.connect(str(db_path), read_only=True)
    try:
        return con.execute(sql, params or []).fetchall()
    finally:
        con.close()


def number_in(text: str, label: str) -> int:
    """The integer printed as '<label>: <n>' in the report text."""
    match = re.search(rf"^\s*{re.escape(label)}\s*:\s*(-?\d+)\s*$", text, re.MULTILINE)
    assert match, f"'{label}: <number>' not found in the report:\n{text}"
    return int(match.group(1))


def section(text: str, title: str) -> str:
    """The lines of one '== <title> ==' section, up to the next section."""
    match = re.search(
        rf"^== {re.escape(title)} ==\s*$(.*?)(?=^== |\Z)", text, re.MULTILINE | re.DOTALL
    )
    assert match, f"section '== {title} ==' not found in the report:\n{text}"
    return match.group(1)


EXPECTED_LAYERS = {
    "RAW member rows": 8,
    "STAGING members": 7,
    "Quarantined member rows": 1,
    "Rows in failed files": 0,
    "CORE members": 6,
    "RAW redemption members": 4,
    "RAW redemption transactions": 5,
    "STAGING redemptions": 4,
    "Quarantined redemption rows": 1,
    "CORE redemption transactions": 4,
}
EXPECTED_COUNTRIES = {
    "MEMBERS_AUS": 1, "MEMBERS_CAN": 1, "MEMBERS_IND": 0, "MEMBERS_PHL": 1, "MEMBERS_USA": 2,
    "MEMBERS_UNASSIGNED": 1,
}  # fmt: skip
EXPECTED_RULES = {"DQ001": 1, "DQ010": 1, "DQ013": 1, "DQ017": 2, "RD001": 1, "RD003": 1}


# ---- rows per layer ---------------------------------------------------------------------------


def test_the_report_has_a_count_for_every_layer() -> None:
    report, _, _ = ran()

    assert tuple(report.layers) == LAYER_KEYS


def test_rows_per_layer_match_the_fixture_feeds() -> None:
    report, _, _ = ran()

    assert report.layers == EXPECTED_LAYERS


def test_the_layer_numbers_in_the_report_are_the_database_counts() -> None:
    report, _, _ = ran()

    assert (
        report.layers["RAW member rows"]
        == query("SELECT count(*) FROM raw.member_feed_raw WHERE record_type = 'D'")[0][0]
    )
    assert (
        report.layers["STAGING members"] == query("SELECT count(*) FROM staging.member_stg")[0][0]
    )
    assert report.layers["CORE members"] == query("SELECT count(*) FROM core.member_current")[0][0]
    assert (
        report.layers["STAGING redemptions"]
        == query("SELECT count(*) FROM staging.redemption_stg")[0][0]
    )
    assert (
        report.layers["CORE redemption transactions"]
        == query("SELECT count(*) FROM core.redemption_txn")[0][0]
    )
    assert (
        report.layers["Quarantined member rows"]
        == query("SELECT count(*) FROM dq.quarantine WHERE source_table = 'RAW.MEMBER_FEED_RAW'")[
            0
        ][0]
    )


# ---- the numbers reconcile (read back from the printed text) -----------------------------------


def test_raw_member_rows_equal_staged_plus_quarantined_in_the_printed_report() -> None:
    _, _, text = ran()

    raw = number_in(text, "RAW member rows")
    accounted = number_in(text, "STAGING members") + number_in(text, "Quarantined member rows")
    assert raw == accounted
    assert number_in(text, "Rows in failed files") == 0  # a clean run


def test_raw_redemption_transactions_equal_staged_plus_quarantined_in_the_printed_report() -> None:
    _, _, text = ran()

    assert number_in(text, "RAW redemption transactions") == (
        number_in(text, "STAGING redemptions") + number_in(text, "Quarantined redemption rows")
    )


def test_country_tables_add_up_to_core_members_in_the_printed_report() -> None:
    _, _, text = ran()

    countries = section(text, "Rows per country table")
    total = sum(
        int(n) for n in re.findall(r"^\s*MEMBERS_\w+\s*:\s*(\d+)\s*$", countries, re.MULTILINE)
    )
    assert total == number_in(text, "CORE members")


def test_core_members_are_the_distinct_member_keys_of_staging() -> None:
    report, _, _ = ran()

    assert (
        report.layers["CORE members"]
        == query("SELECT count(DISTINCT member_key) FROM staging.member_stg")[0][0]
    )


def test_the_dq_summary_in_the_report_equals_the_findings_and_quarantine_in_the_database() -> None:
    report, _, _ = ran()

    member_rules = dict(
        query("SELECT rule_code, sum(failed_count) FROM dq.run_summary GROUP BY rule_code")
    )
    rd001 = query("SELECT count(*) FROM dq.row_finding WHERE rule_code = 'RD001'")[0][0]
    rd003 = query("SELECT count(*) FROM dq.quarantine WHERE rule_code = 'RD003'")[0][0]
    assert report.dq_summary == {
        **{k: int(v) for k, v in member_rules.items()},
        "RD001": rd001,
        "RD003": rd003,
    }


# ---- rows per country table --------------------------------------------------------------------


def test_rows_per_country_table() -> None:
    report, _, _ = ran()

    assert report.country_tables == EXPECTED_COUNTRIES


def test_the_report_country_numbers_are_the_table_row_counts() -> None:
    report, _, _ = ran()

    for table, n in report.country_tables.items():
        assert query(f"SELECT count(*) FROM mart.{table}")[0][0] == n, table


def test_every_core_member_is_in_exactly_one_country_table() -> None:
    keys = [k for (k,) in query("SELECT member_key FROM core.member_current")]
    seen: dict[str, int] = {}
    for table in EXPECTED_COUNTRIES:
        for (key,) in query(f"SELECT member_key FROM mart.{table}"):
            seen[key] = seen.get(key, 0) + 1

    assert sorted(keys) == sorted(seen) and set(seen.values()) == {1}


# ---- a move across the two member files ------------------------------------------------------------


def test_ravi_moved_to_the_usa_and_left_india() -> None:
    assert query("SELECT count(*) FROM mart.members_ind WHERE member_key = 'SKYPOINTS:223458'") == [
        (0,)
    ]
    assert query("SELECT count(*) FROM mart.members_usa WHERE member_key = 'SKYPOINTS:223458'") == [
        (1,)
    ]


def test_the_move_is_in_the_history_and_in_the_report() -> None:
    report, _, _ = ran()

    assert query(
        "SELECT member_key, old_country_code, new_country_code, effective_file_ts "
        "FROM core.member_country_history"
    ) == [("SKYPOINTS:223458", "IND", "USA", datetime(2024, 1, 15, 9, 0))]
    assert report.moves == 1


def test_age_and_stale_member_use_the_as_of_date_passed_on_the_command_line() -> None:
    rows = dict(
        query(
            "SELECT member_name, age FROM core.member_current WHERE member_name IN ('Elena', 'Ravi')"
        )
    )

    assert rows == {"Elena": 38, "Ravi": 38}  # born 5 March 1985, as of 2024-01-15
    assert query("SELECT DISTINCT as_of_date FROM core.member_current") == [(AS_OF,)]


# ---- DQ summary and quarantine -------------------------------------------------------------------


def test_dq_summary_by_rule() -> None:
    report, _, _ = ran()

    assert report.dq_summary == EXPECTED_RULES


def test_quarantined_rows_are_listed_with_file_row_and_rule() -> None:
    report, _, _ = ran()

    assert sorted(
        (q.source_table, q.file_name, q.file_row_number, q.rule_code, q.reason_code)
        for q in report.quarantined
    ) == [
        ("RAW.MEMBER_FEED_RAW", DAY1, 6, "DQ001", "DQ001"),
        ("RAW.REDEMPTION_FEED_RAW", RED, 1, "RD003", "NEGATIVE_MILES"),
    ]


def test_the_printed_report_has_the_four_sections() -> None:
    _, _, text = ran()

    for title in (
        "Rows per layer",
        "Rows per country table",
        "DQ summary by rule",
        "Quarantined rows",
    ):
        assert re.search(rf"^== {re.escape(title)} ==\s*$", text, re.MULTILINE), title


def test_the_printed_report_has_every_number_of_the_report_object() -> None:
    report, _, text = ran()

    for label, n in report.layers.items():
        assert number_in(section(text, "Rows per layer"), label) == n
    for table, n in report.country_tables.items():
        assert number_in(section(text, "Rows per country table"), table) == n
    for rule, n in report.dq_summary.items():
        assert number_in(section(text, "DQ summary by rule"), rule) == n


def test_the_printed_report_lists_the_quarantined_rows() -> None:
    _, _, text = ran()

    quarantined = section(text, "Quarantined rows")
    assert DAY1 in quarantined and "DQ001" in quarantined
    assert RED in quarantined and "RD003" in quarantined


# ---- redemptions ------------------------------------------------------------------------------------


def test_redemptions_are_flattened_staged_and_promoted() -> None:
    assert query("SELECT txn_id FROM core.redemption_txn ORDER BY txn_id") == [
        ("RX10091",),
        ("RX10092",),
        ("RX10093",),
        ("RX10094",),
    ]


def test_an_orphan_redemption_is_counted_and_visible_in_the_summary() -> None:
    report, _, _ = ran()

    assert report.orphan_redemption_members == 1
    assert query(
        "SELECT member_key FROM mart.member_redemption_summary WHERE NOT member_matched"
    ) == [("SKYPOINTS:999999",)]


def test_the_empty_redemptions_array_is_recorded() -> None:
    assert query("SELECT member_key FROM dq.row_finding WHERE rule_code = 'RD001'") == [
        ("SKYPOINTS:223459",)
    ]
    assert query("SELECT count(*) FROM raw.redemption_feed_raw") == [(4,)]


# ---- files ----------------------------------------------------------------------------------------------


def test_every_file_in_the_raw_folder_gets_an_outcome() -> None:
    report, _, _ = ran()

    outcomes = {f.name: (f.kind, f.status) for f in report.files}
    assert outcomes == {
        DAY1: ("members", "loaded"),
        DAY2: ("members", "loaded"),
        RED: ("redemptions", "loaded"),
        "stray.csv": ("unknown", "skipped"),
        "notes.txt": ("unknown", "skipped"),
    }


def test_skipped_files_say_why() -> None:
    report, _, text = ran()

    reasons = {f.name: f.detail for f in report.files if f.status == "skipped"}
    assert reasons == {"stray.csv": "no matching feed rule", "notes.txt": "no matching feed rule"}
    assert "notes.txt" in text and "stray.csv" in text


def test_a_clean_run_does_not_fail() -> None:
    report, _, _ = ran()

    assert report.failed is False
    assert report.blocked_batches == []


# ---- running it again ----------------------------------------------------------------------------------


def test_running_the_pipeline_twice_on_the_same_database_changes_nothing(tmp_path: Path) -> None:
    first, db_path, _ = ran()
    copy = tmp_path / "copy.duckdb"
    shutil.copy(db_path, copy)

    second = run_pipeline(RAW, copy, AS_OF)

    assert second.layers == first.layers
    assert second.country_tables == first.country_tables
    assert second.dq_summary == first.dq_summary
    assert second.moves == first.moves
    statuses = {f.name: f.status for f in second.files}
    assert statuses[DAY1] == statuses[DAY2] == statuses[RED] == "already loaded"


# ---- a file that fails its header check -----------------------------------------------------------------


def test_a_file_with_a_bad_header_fails_alone_and_the_other_file_still_loads(
    tmp_path: Path,
) -> None:
    report = run_pipeline(FIXTURES / "bad_header", tmp_path / "db.duckdb", AS_OF)

    assert {f.name: f.status for f in report.files} == {DAY1: "failed", DAY2: "loaded"}
    assert report.layers["CORE members"] == 1
    assert (
        report.layers["Rows in failed files"] == 1
    )  # counted from the rejected file; it never reaches raw
    assert report.layers["RAW member rows"] == 1  # only the good file
    assert report.layers["RAW member rows"] == (
        report.layers["STAGING members"] + report.layers["Quarantined member rows"]
    )
    assert report.failed is True


def test_the_command_line_exits_with_1_when_a_file_failed(tmp_path: Path, capsys) -> None:
    code = main(
        [
            "--as-of",
            "2024-01-15",
            "--db",
            str(tmp_path / "db.duckdb"),
            "--raw-dir",
            str(FIXTURES / "bad_header"),
        ]
    )

    assert code == 1
    assert DAY1 in capsys.readouterr().out


# ---- the folder is empty ---------------------------------------------------------------------------------


def test_an_empty_raw_folder_gives_a_report_of_zeros(tmp_path: Path) -> None:
    raw = tmp_path / "raw"
    raw.mkdir()

    report = run_pipeline(raw, tmp_path / "db.duckdb", AS_OF)

    assert set(report.layers.values()) == {0}
    assert report.files == [] and report.failed is False


# ---- the command line -------------------------------------------------------------------------------------


def test_main_runs_the_pipeline_prints_the_report_and_returns_0(tmp_path: Path, capsys) -> None:
    code = main(
        ["--as-of", "2024-01-15", "--db", str(tmp_path / "db.duckdb"), "--raw-dir", str(RAW)]
    )

    out = capsys.readouterr().out
    assert code == 0
    assert "== Rows per layer ==" in out
    assert number_in(out, "CORE members") == 6


def test_the_database_folder_is_created_when_missing(tmp_path: Path, capsys) -> None:
    db = tmp_path / "data" / "processed" / "skypoints.duckdb"

    main(["--as-of", "2024-01-15", "--db", str(db), "--raw-dir", str(RAW)])

    assert db.is_file()


@pytest.mark.parametrize(
    "argv",
    [[], ["--as-of", "15/01/2024"], ["--as-of", "2024-13-01"]],
    ids=["missing-as-of", "wrong-format", "impossible-date"],
)
def test_a_missing_or_invalid_as_of_is_rejected_with_exit_code_2(argv: list[str]) -> None:
    with pytest.raises(SystemExit) as excinfo:
        main(argv)

    assert excinfo.value.code == 2


def test_it_runs_as_python_dash_m_skypoints_pipeline(tmp_path: Path) -> None:
    result = subprocess.run(
        [sys.executable, "-m", "skypoints.pipeline", "--as-of", "2024-01-15",
         "--db", str(tmp_path / "db.duckdb"), "--raw-dir", str(RAW)],
        capture_output=True, text=True, check=False, env={"PYTHONPATH": str(REPO / "src"), "SYSTEMROOT": "C:\\Windows"},
        timeout=120,
    )  # fmt: skip

    assert result.returncode == 0, result.stderr[-600:]
    assert "== Rows per layer ==" in result.stdout


# ---- scripts/demo_queries.sql ------------------------------------------------------------------------------


def demo_statements() -> list[str]:
    text = (REPO / "scripts" / "demo_queries.sql").read_text(encoding="utf-8")
    body = "\n".join(line for line in text.splitlines() if not line.strip().startswith("--"))
    return [s.strip() for s in body.split(";") if s.strip()]


def test_every_demo_query_runs_against_the_pipeline_database() -> None:
    statements = demo_statements()
    assert len(statements) == 12

    for number, sql in enumerate(statements, start=1):
        try:
            query(sql)
        except Exception as exc:  # noqa: BLE001 - name the query that broke
            pytest.fail(f"demo query Q{number} failed: {exc}")


def test_the_demo_reconciliation_query_shows_no_unexplained_rows() -> None:
    q3 = demo_statements()[2]

    assert query(q3) == [(0,)]


def test_the_demo_orphan_query_shows_the_orphan() -> None:
    q12 = demo_statements()[11]

    assert [row[0] for row in query(q12)] == ["SKYPOINTS:999999"]
