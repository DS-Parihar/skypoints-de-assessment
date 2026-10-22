"""The pipeline on the real data/raw folder, driven by the source manifest (skypoints.config).

data/raw holds the client files IND.csv, USA.csv and AUS.xlsx (byte-identical), redemptions.json
and the member feed from the brief, plus README.md. as_of 2024-01-15.
"""

import hashlib
import shutil
import tempfile
from datetime import date, datetime
from functools import cache
from pathlib import Path

import duckdb

from skypoints.config import FEED_COUNTRY_EXTRACT, MANIFEST, SourceEntry
from skypoints.pipeline import RunReport, run_pipeline

REPO = Path(__file__).resolve().parents[2]
RAW = REPO / "data" / "raw"
AS_OF = date(2024, 1, 15)
SHARED = Path(tempfile.mkdtemp(prefix="skypoints_data_raw_test_"))
MEMBER_FEED = "SKYPOINTS_MEMBERS_20240115_00000000.dat"


@cache
def first_run() -> tuple[RunReport, Path]:
    db = SHARED / "first.duckdb"
    return run_pipeline(RAW, db, AS_OF), db


def rows(db: Path, sql: str) -> list[tuple]:
    con = duckdb.connect(str(db), read_only=True)
    try:
        return con.execute(sql).fetchall()
    finally:
        con.close()


def fingerprint(db: Path) -> dict[str, list[tuple]]:
    """Every table without audit-only columns, to show a second run changed nothing."""
    spec = {
        "raw.member_feed_raw": "loaded_at", "raw.redemption_feed_raw": "loaded_at",
        "staging.member_stg": "staged_at", "staging.redemption_stg": "staged_at",
        "dq.quarantine": "quarantine_id, quarantined_at", "dq.row_finding": "finding_id, created_at",
        "dq.run_summary": "batch_id", "core.member_current": "updated_at",
        "core.member_country_history": "recorded_at", "core.redemption_txn": "updated_at",
        "mart.members_aus": "updated_at", "mart.members_can": "updated_at", "mart.members_ind": "updated_at",
        "mart.members_phl": "updated_at", "mart.members_usa": "updated_at", "mart.members_unassigned": "updated_at",
    }  # fmt: skip
    return {t: rows(db, f"SELECT * EXCLUDE ({x}) FROM {t} ORDER BY ALL") for t, x in spec.items()}


# ---- files ---------------------------------------------------------------------------------------


def test_every_file_in_data_raw_is_loaded_or_listed_with_a_reason() -> None:
    report, _ = first_run()

    assert {f.name: (f.kind, f.status) for f in report.files} == {
        "AUS.xlsx": ("country extract", "loaded"),
        "IND.csv": ("country extract", "loaded"),
        "USA.csv": ("country extract", "loaded"),
        "redemptions.json": ("redemptions", "loaded"),
        MEMBER_FEED: ("members", "loaded"),
        "README.md": ("unknown", "skipped"),
    }


def test_a_file_matching_no_rule_says_no_matching_feed_rule() -> None:
    report, _ = first_run()

    assert [(f.name, f.detail) for f in report.files if f.status == "skipped"] == [
        ("README.md", "no matching feed rule")
    ]


def test_nothing_in_data_raw_is_changed_by_a_run() -> None:
    before = {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in RAW.iterdir()}

    first_run()

    assert {p.name: hashlib.sha256(p.read_bytes()).hexdigest() for p in RAW.iterdir()} == before


# ---- rows per layer ---------------------------------------------------------------------------------


def test_rows_per_layer_on_data_raw() -> None:
    report, _ = first_run()

    assert report.layers == {
        "RAW member rows": 14,  # 5 from the brief + 3 IND + 3 USA + 3 AUS
        "STAGING members": 12,
        "Quarantined member rows": 2,  # AUS row 3 (month 13), USA row 3 (ambiguous date)
        "Rows in failed files": 0,
        "CORE members": 12,
        "RAW redemption members": 1,
        "RAW redemption transactions": 2,
        "STAGING redemptions": 2,
        "Quarantined redemption rows": 0,
        "CORE redemption transactions": 2,
    }


def test_the_layers_reconcile_on_data_raw() -> None:
    report, _ = first_run()
    layers = report.layers

    assert (
        layers["RAW member rows"] == layers["STAGING members"] + layers["Quarantined member rows"]
    )
    assert (
        layers["RAW redemption transactions"]
        == layers["STAGING redemptions"] + layers["Quarantined redemption rows"]
    )
    assert layers["CORE members"] == sum(report.country_tables.values())


def test_rows_per_country_table_on_data_raw() -> None:
    report, _ = first_run()

    assert report.country_tables == {
        "MEMBERS_AUS": 3,  # Jacob (AU alias) + AUS:1 + AUS:3
        "MEMBERS_CAN": 1,  # Nora
        "MEMBERS_IND": 4,  # Ravi + IND:1, IND:2, IND:3
        "MEMBERS_PHL": 1,  # Mateo (PHIL alias)
        "MEMBERS_UNASSIGNED": 0,
        "MEMBERS_USA": 3,  # Elena + USA:1 + USA:3
    }


def test_members_of_different_sources_with_the_same_id_do_not_collide() -> None:
    _, db = first_run()

    keys = [
        k
        for (k,) in rows(
            db,
            "SELECT member_key FROM core.member_current WHERE member_id IN ('1', '2', '3') ORDER BY 1",
        )
    ]

    assert keys == ["AUS:1", "AUS:3", "IND:1", "IND:2", "IND:3", "USA:1", "USA:3"]


# ---- warnings and quarantine -----------------------------------------------------------------------


def test_the_dq_summary_on_data_raw() -> None:
    report, _ = first_run()

    assert report.dq_summary == {
        "DQ003": 2,  # the two rejected extract rows
        "DQ013": 9,  # ids shorter than 6: 2 in the brief file, 3 IND, 2 USA, 2 AUS
        "DQ014": 1,  # the brief's header has no Post_Code
        "DQ017": 2,  # PHIL and AU resolved through an alias
    }


def test_the_quarantined_rows_are_the_two_bad_extract_rows_with_their_reasons() -> None:
    _, db = first_run()

    assert rows(
        db,
        "SELECT file_name, file_row_number, rule_code, reason_detail FROM dq.quarantine ORDER BY file_name",
    ) == [
        ("AUS.xlsx", 3, "DQ003", "enrollment_date is not a valid date"),  # 2021-13-13
        ("USA.csv", 3, "DQ003", "enrollment_date is ambiguous"),  # 1052022
    ]


def test_the_quarantined_rows_keep_the_original_values() -> None:
    _, db = first_run()

    payloads = [
        p for (p,) in rows(db, "SELECT raw_payload::VARCHAR FROM dq.quarantine ORDER BY file_name")
    ]

    assert '"ENROLLMENT_DATE":"2021-13-13"' in payloads[0]
    assert '"ENROLLMENT_DATE":"1052022"' in payloads[1]


def test_the_warnings_name_the_post_code_header_and_the_aliases() -> None:
    _, db = first_run()

    assert rows(
        db,
        "SELECT file_name, rule_code, column_name, value FROM dq.row_finding WHERE rule_code IN ('DQ014', 'DQ017') ORDER BY 2, 4",
    ) == [
        (MEMBER_FEED, "DQ014", "Post_Code", None),
        (MEMBER_FEED, "DQ017", "country", "AU"),
        (MEMBER_FEED, "DQ017", "country", "PHIL"),
    ]


# ---- the manifest is what drives the extracts ------------------------------------------------------------


def test_the_extract_rows_use_the_manifest_source_system_country_and_file_ts() -> None:
    _, db = first_run()

    got = rows(
        db,
        "SELECT DISTINCT source_system, country, file_ts, batch_id FROM raw.member_feed_raw "
        "WHERE file_name = 'IND.csv'",
    )

    assert got == [("IND", "IND", MANIFEST["IND.csv"].file_ts, "IND.csv")]


def test_the_manifest_date_formats_decide_how_each_extract_is_read() -> None:
    _, db = first_run()

    dates = dict(
        rows(
            db,
            "SELECT member_key, enrollment_date FROM core.member_current "
            "WHERE member_key IN ('IND:1', 'USA:1', 'AUS:1')",
        )
    )

    assert dates == {
        "IND:1": date(2022, 1, 1),
        "USA:1": date(2022, 6, 15),
        "AUS:1": date(2022, 5, 11),
    }


def test_a_changed_manifest_date_format_changes_the_result(tmp_path: Path) -> None:
    """IND.csv read as ISO dates instead of M/D/YYYY: every IND row is rejected."""
    wrong = dict(MANIFEST)
    entry = MANIFEST["IND.csv"]
    wrong["IND.csv"] = SourceEntry(
        entry.feed_type, entry.source_system, entry.country_code, "YYYY-MM-DD", entry.file_ts
    )

    report = run_pipeline(RAW, tmp_path / "db.duckdb", AS_OF, manifest=wrong)

    assert report.layers["CORE members"] == 12 - 3
    assert report.layers["Quarantined member rows"] == 2 + 3


def test_a_file_not_in_the_manifest_is_not_loaded(tmp_path: Path) -> None:
    without_ind = {name: e for name, e in MANIFEST.items() if name != "IND.csv"}

    report = run_pipeline(RAW, tmp_path / "db.duckdb", AS_OF, manifest=without_ind)

    outcomes = {f.name: (f.status, f.detail) for f in report.files}
    assert outcomes["IND.csv"] == ("skipped", "no matching feed rule")
    assert report.layers["CORE members"] == 12 - 3


def test_the_manifest_covers_the_client_files_and_the_redemption_example() -> None:
    assert sorted(MANIFEST) == ["AUS.xlsx", "IND.csv", "USA.csv", "redemptions.json"]
    extracts = {n: e for n, e in MANIFEST.items() if e.feed_type == FEED_COUNTRY_EXTRACT}
    assert {n: (e.source_system, e.country_code, e.date_format) for n, e in extracts.items()} == {
        "IND.csv": ("IND", "IND", "M/D/YYYY"),
        "USA.csv": ("USA", "USA", "MDYYYY"),
        "AUS.xlsx": ("AUS", "AUS", "YYYY-MM-DD"),
    }
    assert all(isinstance(e.file_ts, datetime) for e in MANIFEST.values())
    assert MANIFEST["redemptions.json"].feed_date == "20240115"


def test_the_redemption_file_is_read_with_the_manifest_file_ts() -> None:
    _, db = first_run()

    assert rows(db, "SELECT DISTINCT file_ts FROM staging.redemption_stg") == [
        (MANIFEST["redemptions.json"].file_ts,)
    ]


def test_the_brief_redemption_member_matches_the_brief_member() -> None:
    report, db = first_run()

    assert report.orphan_redemption_members == 0
    assert rows(
        db,
        "SELECT member_key, member_matched, current_country_code, total_completed_miles, pending_miles "
        "FROM mart.member_redemption_summary",
    ) == [("SKYPOINTS:223457", True, "USA", 12000, 5000)]


# ---- running it twice ----------------------------------------------------------------------------------------


def test_a_second_run_on_the_same_database_changes_nothing(tmp_path: Path) -> None:
    first, db = first_run()
    copy = tmp_path / "second.duckdb"
    shutil.copy(db, copy)
    before = fingerprint(copy)

    second = run_pipeline(RAW, copy, AS_OF)

    assert fingerprint(copy) == before
    assert second.layers == first.layers
    assert second.country_tables == first.country_tables
    assert second.dq_summary == first.dq_summary
    assert second.quarantined == first.quarantined
    assert {f.name: f.status for f in second.files if f.kind != "unknown"} == {
        "AUS.xlsx": "already loaded",
        "IND.csv": "already loaded",
        "USA.csv": "already loaded",
        "redemptions.json": "already loaded",
        MEMBER_FEED: "already loaded",
    }
    assert second.failed is False
