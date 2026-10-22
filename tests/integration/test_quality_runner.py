"""Integration tests: run_quality / reconcile over RAW.MEMBER_FEED_RAW in DuckDB.

The runner reads STAGING.MEMBER_PREPARED_V, applies the rules (rules decide, the runner applies):
  REJECT          -> DQ.QUARANTINE (once per row, every rule code), not staged
  WARN / ROUTE    -> DQ.ROW_FINDING, the row is still staged
  FAIL_FILE       -> that file is not staged; other files in the batch continue
  FAIL_BATCH      -> promotion from staging to CORE is blocked; raw is untouched
and writes DQ.RUN_SUMMARY (batch_id, rule_code, failed_count) for every rule that fired.
"""

import json
from datetime import date
from pathlib import Path

import duckdb
import pytest

from skypoints.ingest.flat_file import parse_member_feed
from skypoints.quality.model import RunSummary, Severity
from skypoints.quality.runner import reconcile, run_quality

AS_OF = date(2024, 1, 15)
FULL_HEADER = [
    "Member_Name", "Member_Id", "Enrollment_Date", "Last_Flight_Date", "Tier_Code", "Agent_Name",
    "State", "Country", "Post_Code", "DOB", "Is_Active",
]  # fmt: skip
SAMPLE_HEADER = [c for c in FULL_HEADER if c != "Post_Code"]
FILE_A = "SKYPOINTS_MEMBERS_20240115_13453025.dat"
FILE_B = "SKYPOINTS_MEMBERS_20240116_08000000.dat"


def _one(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None):
    return con.execute(sql, params or []).fetchone()


def _all(con: duckdb.DuckDBPyConnection, sql: str, params: list | None = None) -> list[tuple]:
    return con.execute(sql, params or []).fetchall()


# ---- REJECT -> DQ.QUARANTINE ---------------------------------------------------------------


def test_a_reject_row_goes_to_quarantine_with_batch_rule_reason_severity_and_raw_values(
    con, add_raw
) -> None:
    add_raw(2, member_name="", member_id=" 223457 ")

    run_quality(con, "b1", AS_OF)

    rows = _all(
        con,
        "SELECT batch_id, rule_code, severity, reason_detail, file_name, file_row_number, "
        "raw_payload::VARCHAR FROM dq.quarantine",
    )
    assert len(rows) == 1
    batch_id, rule_code, severity, reason, file_name, row_number, payload = rows[0]
    assert (batch_id, rule_code, severity) == ("b1", "DQ001", "REJECT")
    assert "member_name" in reason
    assert (file_name, row_number) == (FILE_A, 2)
    raw = json.loads(payload)
    assert raw["member_name"] == ""  # the original raw values, untouched
    assert raw["member_id"] == " 223457 "  # not trimmed
    assert raw["enrollment_date"] == "20101012"


def test_a_rejected_row_is_not_staged(con, add_raw) -> None:
    add_raw(2, member_name="")
    add_raw(3, member_id="223458")

    run_quality(con, "b1", AS_OF)

    assert _all(con, "SELECT member_id FROM staging.member_stg") == [("223458",)]


def test_a_row_that_breaks_two_reject_rules_is_quarantined_once_with_both_codes(con, add_raw) -> None:
    add_raw(2, member_name="", member_id="")

    summary = run_quality(con, "b1", AS_OF)

    rows = _all(con, "SELECT rule_code, reason_detail FROM dq.quarantine")
    assert len(rows) == 1
    rule_code, reason = rows[0]
    assert set(rule_code.split(",")) == {"DQ001", "DQ002"}
    assert "member_name" in reason and "member_id" in reason
    assert summary.rows_rejected == 1


def test_each_rule_of_a_double_reject_still_counts_in_the_run_summary(con, add_raw) -> None:
    add_raw(2, member_name="", member_id="")

    run_quality(con, "b1", AS_OF)

    assert _all(con, "SELECT rule_code, failed_count FROM dq.run_summary ORDER BY rule_code") == [
        ("DQ001", 1),
        ("DQ002", 1),
    ]


def test_dq004_keeps_the_last_duplicate_in_a_file_and_quarantines_the_earlier_ones(
    con, add_raw
) -> None:
    add_raw(2, member_name="First")
    add_raw(3, member_name="Second")
    add_raw(4, member_name="Last")  # same member_key SKYPOINTS:223457 three times

    run_quality(con, "b1", AS_OF)

    assert _all(con, "SELECT member_name FROM staging.member_stg") == [("Last",)]
    assert _all(con, "SELECT file_row_number, rule_code FROM dq.quarantine ORDER BY 1") == [
        (2, "DQ004"),
        (3, "DQ004"),
    ]


def test_a_value_over_the_spec_length_is_quarantined_by_dq005(con, add_raw) -> None:
    add_raw(2, state="x" * 6)

    run_quality(con, "b1", AS_OF)

    assert _all(con, "SELECT rule_code FROM dq.quarantine") == [("DQ005",)]
    assert _one(con, "SELECT count(*) FROM staging.member_stg") == (0,)


# ---- WARN / ROUTE -> DQ.ROW_FINDING ----------------------------------------------------------


def test_a_warn_row_is_recorded_in_row_finding_and_still_staged(con, add_raw) -> None:
    add_raw(2, tier_code="XXX")

    run_quality(con, "b1", AS_OF)

    assert _all(
        con,
        "SELECT batch_id, member_key, rule_code, severity, column_name, value FROM dq.row_finding",
    ) == [("b1", "SKYPOINTS:223457", "DQ011", "WARN", "tier_code", "XXX")]
    assert _one(con, "SELECT count(*) FROM staging.member_stg") == (1,)
    assert _one(con, "SELECT count(*) FROM dq.quarantine") == (0,)


def test_a_route_row_is_recorded_in_row_finding_and_staged_without_a_country_code(
    con, add_raw
) -> None:
    add_raw(2, country="ZZZ")

    run_quality(con, "b1", AS_OF)

    assert _all(con, "SELECT rule_code, severity, column_name, value FROM dq.row_finding") == [
        ("DQ010", "ROUTE", "country", "ZZZ")
    ]
    assert _one(con, "SELECT country_code FROM staging.member_stg") == (None,)


def test_an_invalid_optional_date_becomes_null_in_staging_with_a_warning(con, add_raw) -> None:
    add_raw(2, date_of_birth="13051985")

    run_quality(con, "b1", AS_OF)

    assert _one(con, "SELECT date_of_birth FROM staging.member_stg") == (None,)
    assert _all(con, "SELECT rule_code, severity, column_name, value FROM dq.row_finding") == [
        ("DQ006", "WARN", "date_of_birth", "13051985")
    ]


def test_an_ambiguous_date_becomes_null_in_staging_with_a_warning(con, add_raw) -> None:
    add_raw(2, source_system="USA", member_id="1", last_flight_date="1052022")

    run_quality(con, "b1", AS_OF)

    assert _one(con, "SELECT last_flight_date FROM staging.member_stg") == (None,)
    assert ("DQ007", "WARN", "last_flight_date") in _all(
        con, "SELECT rule_code, severity, column_name FROM dq.row_finding"
    )


def test_an_implausible_dob_is_flagged_but_the_value_is_not_changed(con, add_raw) -> None:
    add_raw(2, date_of_birth="01011900")

    run_quality(con, "b1", AS_OF)

    assert _one(con, "SELECT date_of_birth FROM staging.member_stg") == (date(1900, 1, 1),)
    assert ("DQ008", "WARN", "date_of_birth") in _all(
        con, "SELECT rule_code, severity, column_name FROM dq.row_finding"
    )


# ---- run summary and return value --------------------------------------------------------


def test_the_run_summary_table_counts_failures_per_rule(con, add_raw) -> None:
    add_raw(2, member_id="12345")
    add_raw(3, member_id="12346")
    add_raw(4, member_id="223459", tier_code="XXX")

    run_quality(con, "b1", AS_OF)

    assert _all(
        con, "SELECT batch_id, rule_code, failed_count FROM dq.run_summary ORDER BY rule_code"
    ) == [("b1", "DQ011", 1), ("b1", "DQ013", 2)]


def test_run_quality_returns_a_summary_object(con, add_raw) -> None:
    add_raw(2, member_name="")
    add_raw(3, member_id="12346")
    add_raw(4, member_id="223459")

    summary = run_quality(con, "b1", AS_OF)

    assert isinstance(summary, RunSummary)
    assert summary.batch_id == "b1"
    assert (summary.rows_read, summary.rows_staged, summary.rows_rejected) == (3, 2, 1)
    assert dict(summary.findings_by_rule) == {"DQ001": 1, "DQ013": 1}
    assert summary.failed_files == ()
    assert summary.promotion_blocked is False


def test_rows_of_other_batches_are_left_alone(con, add_raw) -> None:
    add_raw(2, batch_id="b1")
    add_raw(3, member_id="223458", batch_id="b2", member_name="")

    run_quality(con, "b1", AS_OF)

    assert _one(con, "SELECT count(*) FROM dq.quarantine") == (0,)
    assert _all(con, "SELECT batch_id FROM staging.member_stg") == [("b1",)]


def test_running_a_batch_twice_does_not_duplicate_anything(con, add_raw) -> None:
    add_raw(2, member_name="")
    add_raw(3, member_id="12346")
    add_raw(4, member_id="223459")

    run_quality(con, "b1", AS_OF)
    run_quality(con, "b1", AS_OF)

    assert _one(con, "SELECT count(*) FROM dq.quarantine") == (1,)
    assert _one(con, "SELECT count(*) FROM staging.member_stg") == (2,)
    assert _one(con, "SELECT count(*) FROM dq.row_finding") == (1,)
    assert _all(con, "SELECT rule_code, failed_count FROM dq.run_summary ORDER BY 1") == [
        ("DQ001", 1),
        ("DQ013", 1),
    ]


def test_the_as_of_date_is_passed_in_and_reaches_staging(con, add_raw) -> None:
    add_raw(2)

    run_quality(con, "b1", date(2024, 3, 5))

    assert _one(con, "SELECT as_of_date, age FROM staging.member_stg") == (date(2024, 3, 5), 39)


def test_as_of_has_no_default() -> None:
    with pytest.raises(TypeError):
        run_quality(None, "b1")  # type: ignore[call-arg]


# ---- FAIL_FILE: stops only that file ----------------------------------------------------


def test_a_fail_file_header_stops_that_file_but_not_the_other_files(con, add_raw) -> None:
    add_raw(2, file_name=FILE_A)
    add_raw(2, member_id="223458", file_name=FILE_B)
    headers = {FILE_A: [*FULL_HEADER, "Region"], FILE_B: FULL_HEADER}

    summary = run_quality(con, "b1", AS_OF, headers=headers)

    assert _all(con, "SELECT DISTINCT file_name FROM staging.member_stg") == [(FILE_B,)]
    assert summary.failed_files == (FILE_A,)
    assert _one(con, "SELECT count(*) FROM dq.quarantine") == (0,)  # not row by row
    assert _all(
        con, "SELECT file_name, file_row_number, rule_code, severity, column_name FROM dq.row_finding"
    ) == [(FILE_A, None, "DQ014", "FAIL_FILE", "Region")]


def test_a_fail_file_does_not_block_promotion_of_the_batch(con, add_raw) -> None:
    add_raw(2)

    summary = run_quality(con, "b1", AS_OF, headers={FILE_A: [*FULL_HEADER, "Region"]})

    assert summary.promotion_blocked is False


def test_a_missing_optional_column_is_only_a_warning_and_the_file_continues(con, add_raw) -> None:
    add_raw(2, post_code="")

    summary = run_quality(con, "b1", AS_OF, headers={FILE_A: SAMPLE_HEADER})

    assert _one(con, "SELECT count(*) FROM staging.member_stg") == (1,)
    assert summary.failed_files == ()
    assert _all(con, "SELECT rule_code, severity, column_name, member_key FROM dq.row_finding") == [
        ("DQ014", "WARN", "Post_Code", None)
    ]


# ---- FAIL_BATCH: reconciliation ---------------------------------------------------------


def _stage_the_brief_sample(con, add_raw, tmp_path: Path) -> None:
    feed = tmp_path / FILE_A
    feed.write_text(
        "|H|" + "|".join(SAMPLE_HEADER) + "\n"
        "|D|Elena|223457|20101012|20121013|GLD|Sam|CA|USA|03051985|A\n"
        "|D|Ravi|223458|20101012|20121013|SLV|Sam|TN|IND|03051985|A\n"
        "|D|Mateo|223459|20101012|20121013|GLD|Sam|NCR|PHIL|03051985|A\n"
        "|D|Nora|22345|20101012|20121013|PLT|Sam|ONT|CAN|03051985|A\n"
        "|D|Jacob|2256|20101012|20121013|SLV|Sam|VIC|AU|03051985|A\n",
        encoding="utf-8",
    )
    _, records, _ = parse_member_feed(feed)  # by-name parser: the missing Post_Code becomes ''
    for record in records:
        add_raw(
            record["file_row_number"],
            **{k: v for k, v in record.items() if k not in ("file_row_number",)},
        )


def test_dq016_fails_the_batch_for_a_column_that_lost_values_between_raw_and_staging(
    con, add_raw, tmp_path: Path
) -> None:
    _stage_the_brief_sample(con, add_raw, tmp_path)
    run_quality(con, "b1", AS_OF, headers={FILE_A: SAMPLE_HEADER})
    # What the brief's intermediate table shows: agent_name survives for Elena only.
    con.execute("UPDATE staging.member_stg SET agent_name = NULL WHERE member_name <> 'Elena'")

    findings = reconcile(con, "b1")

    assert {(f.rule_code, f.column_name, f.severity) for f in findings} == {
        ("DQ016", "agent_name", Severity.FAIL_BATCH)
    }
    (finding,) = findings
    assert "5" in finding.reason and "1" in finding.reason  # raw 5 non-blank, staging 1


def test_reconcile_is_clean_when_raw_and_staging_agree(con, add_raw, tmp_path: Path) -> None:
    _stage_the_brief_sample(con, add_raw, tmp_path)
    run_quality(con, "b1", AS_OF, headers={FILE_A: SAMPLE_HEADER})

    assert reconcile(con, "b1") == []


def test_dq015_fails_the_batch_when_a_staged_row_goes_missing(con, add_raw) -> None:
    add_raw(2)
    add_raw(3, member_id="223458")
    run_quality(con, "b1", AS_OF)
    con.execute("DELETE FROM staging.member_stg WHERE member_id = '223458'")

    findings = reconcile(con, "b1")

    assert "DQ015" in {f.rule_code for f in findings}
    assert {f.severity for f in findings} == {Severity.FAIL_BATCH}


def test_dq016_ignores_rows_that_were_rejected(con, add_raw) -> None:
    add_raw(2)
    add_raw(3, member_id="223458", member_name="", agent_name="Sam")  # rejected, agent non-blank

    run_quality(con, "b1", AS_OF)

    assert reconcile(con, "b1") == []  # the rejected row's agent_name is not "lost"


def test_a_fail_batch_blocks_promotion_and_leaves_raw_untouched(con, add_raw) -> None:
    add_raw(2)
    add_raw(3, member_id="223458")
    run_quality(con, "b1", AS_OF)
    con.execute("UPDATE staging.member_stg SET agent_name = NULL")
    raw_before = _all(con, "SELECT * FROM raw.member_feed_raw ORDER BY file_row_number")

    summary = run_quality(con, "b1", AS_OF)

    assert summary.promotion_blocked is True
    assert ("DQ016" in dict(summary.findings_by_rule)) is True
    assert _one(con, "SELECT failed_count FROM dq.run_summary WHERE rule_code = 'DQ016'")[0] >= 1
    assert _all(con, "SELECT * FROM raw.member_feed_raw ORDER BY file_row_number") == raw_before


# ---- the brief's 5-row sample, end to end --------------------------------------------------


def test_the_brief_sample_fires_only_the_expected_rules_and_all_five_reach_staging(
    con, add_raw, tmp_path: Path
) -> None:
    _stage_the_brief_sample(con, add_raw, tmp_path)

    summary = run_quality(con, "b1", AS_OF, headers={FILE_A: SAMPLE_HEADER})

    findings = {
        (rule_code, severity, row_number, column_name)
        for rule_code, severity, row_number, column_name in _all(
            con, "SELECT rule_code, severity, file_row_number, column_name FROM dq.row_finding"
        )
    }
    assert findings == {
        ("DQ014", "WARN", None, "Post_Code"),
        ("DQ013", "WARN", 5, "member_id"),  # Nora, 22345
        ("DQ013", "WARN", 6, "member_id"),  # Jacob, 2256
        ("DQ017", "WARN", 4, "country"),  # Mateo, PHIL
        ("DQ017", "WARN", 6, "country"),  # Jacob, AU
    }
    assert _one(con, "SELECT count(*) FROM dq.quarantine") == (0,)  # nothing rejected
    assert not _all(con, "SELECT 1 FROM dq.row_finding WHERE severity = 'ROUTE'")  # nothing routed
    assert _all(
        con, "SELECT country_code FROM staging.member_stg ORDER BY file_row_number"
    ) == [("USA",), ("IND",), ("PHL",), ("CAN",), ("AUS",)]
    assert _one(con, "SELECT count(*) FROM staging.member_stg") == (5,)
    assert dict(summary.findings_by_rule) == {"DQ013": 2, "DQ014": 1, "DQ017": 2}
    assert (summary.rows_read, summary.rows_staged, summary.rows_rejected) == (5, 5, 0)
    assert summary.promotion_blocked is False
    assert summary.failed_files == ()
