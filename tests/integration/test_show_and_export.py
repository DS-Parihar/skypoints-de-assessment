"""Integration tests: skypoints.export.export_csv, the pipeline's --export-dir / --no-export
options, and scripts/show.py, all on the pipeline fixture feeds (as_of 2024-01-15).

Fixture run (tests/fixtures/pipeline/raw): 8 raw member rows, 7 staged, 1 quarantined (DQ001),
6 core members (Ravi moved IND -> USA), redemptions with one negative-miles quarantine (RD003),
one empty array (RD001) and one orphan member 999999.
"""

import csv
import importlib.util
import sys
import tempfile
from datetime import date
from functools import cache
from pathlib import Path

import pytest

from skypoints.export import FIXED_FILES, MEMBER_COLUMNS, export_csv
from skypoints.pipeline import RunReport, main, run_pipeline

REPO = Path(__file__).resolve().parents[2]
RAW = REPO / "tests" / "fixtures" / "pipeline" / "raw"
AS_OF = date(2024, 1, 15)
SHARED = Path(tempfile.mkdtemp(prefix="skypoints_export_test_"))
COUNTRY_FILES = [f"members_{c}.csv" for c in ("aus", "can", "ind", "phl", "unassigned", "usa")]


@cache
def pipeline_db() -> tuple[RunReport, Path]:
    db = SHARED / "run.duckdb"
    return run_pipeline(RAW, db, AS_OF), db


@cache
def exported() -> Path:
    _, db = pipeline_db()
    out = SHARED / "export"
    export_csv(db, out)
    return out


def read_csv(name: str) -> tuple[list[str], list[dict[str, str]]]:
    with (exported() / name).open(newline="", encoding="utf-8") as handle:
        reader = csv.DictReader(handle)
        rows = list(reader)
        return list(reader.fieldnames or []), rows


# ---- export_csv --------------------------------------------------------------------------------------


def test_export_writes_one_csv_per_table_and_returns_the_paths() -> None:
    _, db = pipeline_db()
    out = SHARED / "export_names"

    written = export_csv(db, out)

    assert sorted(p.name for p in written) == sorted([*FIXED_FILES, *COUNTRY_FILES])
    assert all(p.is_file() and p.parent == out for p in written)


def test_the_output_folder_is_created_when_missing() -> None:
    _, db = pipeline_db()
    out = SHARED / "a" / "b" / "c"

    export_csv(db, out)

    assert (out / "members.csv").is_file()


def test_members_csv_has_the_readable_columns_and_one_row_per_core_member() -> None:
    header, rows = read_csv("members.csv")

    assert header == list(MEMBER_COLUMNS)
    assert len(rows) == 6


def test_a_member_row_reads_naturally() -> None:
    _, rows = read_csv("members.csv")
    ravi = next(r for r in rows if r["member_key"] == "SKYPOINTS:223458")

    assert ravi["member_name"] == "Ravi"
    assert ravi["country_code"] == "USA"  # after the move
    assert ravi["age"] == "38"
    assert ravi["date_of_birth"] == "1985-03-05"  # ISO date, not a timestamp
    assert ravi["stale_member"] == "false"  # true/false, not 0/1 or True


def test_null_values_are_empty_fields() -> None:
    _, rows = read_csv("members.csv")
    zed = next(r for r in rows if r["member_key"] == "SKYPOINTS:223460")

    assert zed["country_code"] == ""  # unassigned


def test_each_country_file_lists_the_members_of_that_table() -> None:
    report, _ = pipeline_db()

    for table, n in report.country_tables.items():
        header, rows = read_csv(f"{table.lower()}.csv")
        assert header == list(MEMBER_COLUMNS)
        assert len(rows) == n, table


def test_an_empty_country_table_gives_a_header_only_file() -> None:
    header, rows = read_csv("members_ind.csv")

    assert header == list(MEMBER_COLUMNS)
    assert rows == []


def test_the_country_files_are_disjoint_and_cover_members_csv() -> None:
    _, members = read_csv("members.csv")
    keys = [r["member_key"] for name in COUNTRY_FILES for r in read_csv(name)[1]]

    assert sorted(keys) == sorted(r["member_key"] for r in members)


def test_the_history_file_lists_the_move() -> None:
    header, rows = read_csv("member_country_history.csv")

    assert header == [
        "member_key",
        "old_country_code",
        "new_country_code",
        "effective_file_ts",
        "batch_id",
    ]
    assert [(r["member_key"], r["old_country_code"], r["new_country_code"]) for r in rows] == [
        ("SKYPOINTS:223458", "IND", "USA")
    ]


def test_the_quarantine_file_has_the_rule_the_reason_and_the_original_values() -> None:
    header, rows = read_csv("quarantine.csv")

    assert header == [
        "source_table",
        "file_name",
        "file_row_number",
        "rule_code",
        "reason_code",
        "reason_detail",
        "raw_payload",
    ]
    assert sorted((r["rule_code"], r["file_row_number"]) for r in rows) == [
        ("DQ001", "6"),
        ("RD003", "1"),
    ]
    assert any("223464" in r["raw_payload"] for r in rows)  # the blank-name row, original values


def test_the_findings_file_has_one_row_per_warning() -> None:
    header, rows = read_csv("dq_findings.csv")

    assert header == [
        "batch_id",
        "file_name",
        "file_row_number",
        "member_key",
        "rule_code",
        "severity",
        "column_name",
        "value",
        "reason",
    ]
    assert {r["rule_code"] for r in rows} >= {"DQ010", "DQ013", "DQ017", "RD001"}


def test_the_summary_file_equals_the_report_dq_summary() -> None:
    report, _ = pipeline_db()
    header, rows = read_csv("dq_summary.csv")

    assert header == ["rule_code", "failed_count"]
    assert {r["rule_code"]: int(r["failed_count"]) for r in rows} == report.dq_summary


def test_the_redemption_files_show_the_orphan() -> None:
    _, txns = read_csv("redemptions.csv")
    _, summary = read_csv("redemption_summary.csv")

    assert sorted(r["txn_id"] for r in txns) == ["RX10091", "RX10092", "RX10093", "RX10094"]
    orphan = [r for r in summary if r["member_matched"] == "false"]
    assert [r["member_key"] for r in orphan] == ["SKYPOINTS:999999"]


def test_a_value_with_a_comma_or_a_quote_survives_a_round_trip() -> None:
    _, rows = read_csv("quarantine.csv")

    assert all(r["raw_payload"].startswith("{") and r["raw_payload"].endswith("}") for r in rows)


def test_the_files_are_utf8_text_with_a_header_line_and_no_binary() -> None:
    for name in FIXED_FILES:
        data = (exported() / name).read_bytes()
        data.decode("utf-8")
        assert b"\x00" not in data and data.splitlines()[0].count(b",") >= 1


def test_exporting_twice_overwrites_with_the_same_content() -> None:
    _, db = pipeline_db()
    out = SHARED / "twice"
    export_csv(db, out)
    first = {p.name: p.read_bytes() for p in out.iterdir()}

    export_csv(db, out)

    assert {p.name: p.read_bytes() for p in out.iterdir()} == first


def test_export_only_reads_the_database() -> None:
    _, db = pipeline_db()
    before = db.stat().st_mtime_ns

    export_csv(db, SHARED / "readonly_check")

    assert db.stat().st_mtime_ns == before


def test_exporting_a_database_with_no_rows_gives_header_only_files(tmp_path: Path) -> None:
    empty_raw = tmp_path / "raw"
    empty_raw.mkdir()
    db = tmp_path / "empty.duckdb"
    run_pipeline(empty_raw, db, AS_OF)

    export_csv(db, tmp_path / "out")

    for name in FIXED_FILES:
        lines = (tmp_path / "out" / name).read_text(encoding="utf-8").splitlines()
        assert len(lines) == 1, name


def test_a_missing_database_is_an_error_not_an_empty_export(tmp_path: Path) -> None:
    with pytest.raises(FileNotFoundError):
        export_csv(tmp_path / "nope.duckdb", tmp_path / "out")

    assert not (tmp_path / "out").exists()


# ---- the pipeline writes the export ------------------------------------------------------------------------------


def test_the_pipeline_exports_next_to_the_database_by_default(tmp_path: Path, capsys) -> None:
    db = tmp_path / "processed" / "skypoints.duckdb"

    code = main(["--as-of", "2024-01-15", "--db", str(db), "--raw-dir", str(RAW)])

    out = db.parent / "export"
    assert code == 0
    assert sorted(p.name for p in out.glob("*.csv")) == sorted([*FIXED_FILES, *COUNTRY_FILES])
    assert "export" in capsys.readouterr().out.lower()  # the report says where the files went


def test_the_run_report_is_saved_as_text_in_the_export_folder(tmp_path: Path, capsys) -> None:
    db = tmp_path / "skypoints.duckdb"

    main(["--as-of", "2024-01-15", "--db", str(db), "--raw-dir", str(RAW)])

    saved = (db.parent / "export" / "run_report.txt").read_text(encoding="utf-8")
    assert "== Rows per layer ==" in saved and "CORE members: 6" in saved


def test_export_dir_chooses_the_folder(tmp_path: Path) -> None:
    custom = tmp_path / "my_csv"

    main(
        [
            "--as-of",
            "2024-01-15",
            "--db",
            str(tmp_path / "db.duckdb"),
            "--raw-dir",
            str(RAW),
            "--export-dir",
            str(custom),
        ]
    )

    assert (custom / "members.csv").is_file()
    assert not (tmp_path / "export").exists()


def test_no_export_skips_the_csv_files(tmp_path: Path) -> None:
    main(
        [
            "--as-of",
            "2024-01-15",
            "--db",
            str(tmp_path / "db.duckdb"),
            "--raw-dir",
            str(RAW),
            "--no-export",
        ]
    )

    assert not (tmp_path / "export").exists()


def test_the_export_is_written_even_when_a_file_failed(tmp_path: Path) -> None:
    bad = REPO / "tests" / "fixtures" / "pipeline" / "bad_header"

    code = main(
        ["--as-of", "2024-01-15", "--db", str(tmp_path / "db.duckdb"), "--raw-dir", str(bad)]
    )

    assert code == 1
    assert (tmp_path / "export" / "members.csv").is_file()


# ---- scripts/show.py on a real database -------------------------------------------------------------------------


def load_show():
    spec = importlib.util.spec_from_file_location("show", REPO / "scripts" / "show.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["show"] = module  # a dataclass in the script looks its module up here
    spec.loader.exec_module(module)
    return module


def test_show_prints_every_demo_query_with_its_title_and_a_table(capsys) -> None:
    _, db = pipeline_db()
    show = load_show()

    code = show.main(["--db", str(db)])

    out = capsys.readouterr().out
    assert code == 0
    for number in range(1, 13):
        assert f"Q{number}." in out
    assert out.count("rows)") + out.count("row)") == 12


def test_show_prints_real_values_from_the_database(capsys) -> None:
    _, db = pipeline_db()
    show = load_show()

    show.main(["--db", str(db), "--only", "3,12"])

    out = capsys.readouterr().out
    assert "unexplained_member_rows" in out and "SKYPOINTS:999999" in out
    assert "Q1." not in out


def test_show_only_one_query(capsys) -> None:
    _, db = pipeline_db()
    show = load_show()

    show.main(["--db", str(db), "--only", "5"])

    out = capsys.readouterr().out
    assert "Q5." in out and "IND" in out and "USA" in out and "Q4." not in out


def test_show_cuts_long_values_to_the_width(capsys) -> None:
    _, db = pipeline_db()
    show = load_show()

    show.main(["--db", str(db), "--only", "9", "--width", "30"])

    out = capsys.readouterr().out
    assert "…" in out  # the raw_payload column is long: it is cut with an ellipsis, never wrapped
    assert max(len(line) for line in out.splitlines()) < 400


def test_show_with_a_missing_database_says_to_run_the_pipeline_and_creates_nothing(
    tmp_path: Path, capsys
) -> None:
    show = load_show()
    missing = tmp_path / "nope.duckdb"

    code = show.main(["--db", str(missing)])

    assert code == 2
    assert "run the pipeline first" in capsys.readouterr().err.lower()
    assert not missing.exists()


@pytest.mark.parametrize("only", ["0", "13", "x", "3,,4"])
def test_show_rejects_an_unknown_query_number(only: str, capsys) -> None:
    _, db = pipeline_db()
    show = load_show()

    code = show.main(["--db", str(db), "--only", only])

    assert code == 2
    assert "query" in capsys.readouterr().err.lower()


def test_show_runs_as_a_plain_script_without_the_package_on_the_path(tmp_path: Path) -> None:
    import subprocess
    import sys

    _, db = pipeline_db()

    result = subprocess.run(
        [sys.executable, str(REPO / "scripts" / "show.py"), "--db", str(db), "--only", "3"],
        capture_output=True,
        text=True,
        check=False,
        timeout=60,
        env={"SYSTEMROOT": "C:\\Windows"},
    )

    assert result.returncode == 0, result.stderr[-500:]
    assert "unexplained_member_rows" in result.stdout
