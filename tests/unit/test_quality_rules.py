"""Unit tests for the quality rules (pure: rows in, findings out; no database).

Each rule gets a passing row, a failing row and its edge cases. Rules read PreparedRow objects
(text already cleaned, blank -> None, but the rules also treat whitespace-only text as blank).
Thresholds come from QualityConfig, never from inside a rule.
"""

from datetime import date

import pytest

from skypoints.quality.model import (
    BatchCounts,
    Finding,
    HeaderInfo,
    PreparedRow,
    QualityConfig,
    RuleContext,
    Severity,
)
from skypoints.quality.rules import RULES, apply_rules

AS_OF = date(2024, 1, 15)
FULL_HEADER = [
    "Member_Name", "Member_Id", "Enrollment_Date", "Last_Flight_Date", "Tier_Code", "Agent_Name",
    "State", "Country", "Post_Code", "DOB", "Is_Active",
]  # fmt: skip
SAMPLE_HEADER = [c for c in FULL_HEADER if c != "Post_Code"]  # the brief's 10 fields


def make_row(row_number: int = 2, **overrides) -> PreparedRow:
    """A fully valid member row; override single fields to break one rule at a time."""
    values = dict(
        batch_id="b1",
        file_name="f.dat",
        file_row_number=row_number,
        source_system="SKYPOINTS",
        member_key="SKYPOINTS:223457",
        member_name="Elena",
        member_id="223457",
        enrollment_date="20101012",
        last_flight_date="20121013",
        date_of_birth="03051985",
        tier_code="GLD",
        agent_name="Sam",
        state="CA",
        country="USA",
        post_code="02134",
        active_member="A",
    )
    values.update(overrides)
    return PreparedRow(**values)


def run(code: str, rows: list[PreparedRow], as_of: date = AS_OF, **ctx) -> list[Finding]:
    return RULES[code].check(RuleContext(rows=rows, as_of=as_of, **ctx))


def hits(findings: list[Finding]) -> set[tuple[int | None, str | None]]:
    """(file_row_number, column_name) of every finding."""
    return {(f.file_row_number, f.column_name) for f in findings}


# ---- the rule table ----------------------------------------------------------------------


def test_the_rule_table_has_the_agreed_codes_and_severities() -> None:
    expected = {
        "DQ001": Severity.REJECT, "DQ002": Severity.REJECT, "DQ003": Severity.REJECT,
        "DQ004": Severity.REJECT, "DQ005": Severity.REJECT, "DQ006": Severity.WARN,
        "DQ007": Severity.WARN, "DQ008": Severity.WARN, "DQ009": Severity.WARN,
        "DQ010": Severity.ROUTE, "DQ011": Severity.WARN, "DQ012": Severity.WARN,
        "DQ013": Severity.WARN, "DQ014": Severity.FAIL_FILE, "DQ015": Severity.FAIL_BATCH,
        "DQ016": Severity.FAIL_BATCH, "DQ017": Severity.WARN,
    }  # fmt: skip

    assert {code: rule.severity for code, rule in RULES.items()} == expected
    assert all(rule.description for rule in RULES.values())
    assert all(rule.code == code for code, rule in RULES.items())


# One passing and one failing case per rule: (rows, extra context). The failing case also fixes
# the severity each finding must carry (DQ014 raises FAIL_FILE here; its optional-column
# finding is WARN and is tested below).
PASSING = {
    "DQ001": ([make_row()], {}),
    "DQ002": ([make_row()], {}),
    "DQ003": ([make_row()], {}),
    "DQ004": ([make_row(2, member_key="SKYPOINTS:1"), make_row(3, member_key="SKYPOINTS:2")], {}),
    "DQ005": ([make_row()], {}),
    "DQ006": ([make_row()], {}),
    "DQ007": ([make_row()], {}),
    "DQ008": ([make_row()], {}),
    "DQ009": ([make_row()], {}),
    "DQ010": ([make_row()], {}),
    "DQ011": ([make_row()], {}),
    "DQ012": ([make_row()], {}),
    "DQ013": ([make_row()], {}),
    "DQ014": ([], {"header": HeaderInfo("f.dat", FULL_HEADER)}),
    "DQ015": ([], {"counts": BatchCounts(raw_rows=5, staged_rows=4, rejected_rows=1)}),
    "DQ016": (
        [],
        {"counts": BatchCounts(5, 5, 0, {"agent_name": 5}, {"agent_name": 5})},
    ),
    "DQ017": ([make_row()], {}),
}
FAILING = {
    "DQ001": ([make_row(member_name=None)], {}, Severity.REJECT),
    "DQ002": ([make_row(member_id=None)], {}, Severity.REJECT),
    "DQ003": ([make_row(enrollment_date="20211313")], {}, Severity.REJECT),
    "DQ004": (
        [make_row(2), make_row(3)],  # same member_key twice
        {},
        Severity.REJECT,
    ),
    "DQ005": ([make_row(state="x" * 6)], {}, Severity.REJECT),
    "DQ006": ([make_row(last_flight_date="20231301")], {}, Severity.WARN),
    "DQ007": ([make_row(source_system="USA", last_flight_date="1052022")], {}, Severity.WARN),
    "DQ008": ([make_row(date_of_birth="01162024")], {}, Severity.WARN),
    "DQ009": ([make_row(last_flight_date="20240116")], {}, Severity.WARN),
    "DQ010": ([make_row(country="ZZZ")], {}, Severity.ROUTE),
    "DQ011": ([make_row(tier_code="ABC")], {}, Severity.WARN),
    "DQ012": ([make_row(active_member="X")], {}, Severity.WARN),
    "DQ013": ([make_row(member_id="12345")], {}, Severity.WARN),
    "DQ014": ([], {"header": HeaderInfo("f.dat", [c for c in FULL_HEADER if c != "Member_Id"])},
              Severity.FAIL_FILE),
    "DQ015": ([], {"counts": BatchCounts(raw_rows=5, staged_rows=3, rejected_rows=1)},
              Severity.FAIL_BATCH),
    "DQ016": (
        [],
        {"counts": BatchCounts(5, 5, 0, {"agent_name": 5}, {"agent_name": 1})},
        Severity.FAIL_BATCH,
    ),
    "DQ017": ([make_row(country="PHIL")], {}, Severity.WARN),
}  # fmt: skip


@pytest.mark.parametrize("code", sorted(PASSING))
def test_a_passing_row_produces_no_findings(code: str) -> None:
    rows, ctx = PASSING[code]

    assert run(code, rows, **ctx) == []


@pytest.mark.parametrize("code", sorted(FAILING))
def test_a_failing_row_produces_a_finding_with_code_severity_and_reason(code: str) -> None:
    rows, ctx, severity = FAILING[code]

    findings = run(code, rows, **ctx)

    assert len(findings) == 1
    assert findings[0].rule_code == code
    assert findings[0].severity is severity
    assert findings[0].reason  # a readable reason, never empty


def test_a_row_finding_carries_the_row_identity_and_the_offending_value() -> None:
    row = make_row(7, member_id="12345", member_key="SKYPOINTS:12345", file_name="g.dat", batch_id="b9")

    (finding,) = run("DQ013", [row])

    assert (finding.batch_id, finding.file_name, finding.file_row_number) == ("b9", "g.dat", 7)
    assert finding.member_key == "SKYPOINTS:12345"
    assert (finding.column_name, finding.value) == ("member_id", "12345")


# ---- DQ001 / DQ002: mandatory text ---------------------------------------------------------


@pytest.mark.parametrize("blank", [None, "", "   "], ids=["NULL", "empty", "spaces"])
@pytest.mark.parametrize(
    ("code", "column"), [("DQ001", "member_name"), ("DQ002", "member_id")]
)
def test_blank_and_null_are_the_same_for_mandatory_text(code: str, column: str, blank) -> None:
    findings = run(code, [make_row(**{column: blank})])

    assert hits(findings) == {(2, column)}


@pytest.mark.parametrize(("code", "column"), [("DQ001", "member_name"), ("DQ002", "member_id")])
def test_a_one_character_value_is_not_blank(code: str, column: str) -> None:
    assert run(code, [make_row(**{column: "x"})]) == []


# ---- DQ003: enrollment date ----------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, "", "   ", "20211313", "20210229", "2021-12-13", "abc"])
def test_dq003_fires_for_a_blank_or_invalid_enrollment_date(bad) -> None:
    assert hits(run("DQ003", [make_row(enrollment_date=bad)])) == {(2, "enrollment_date")}


@pytest.mark.parametrize("good", ["20101012", "20200229"], ids=["ordinary", "leap-day"])
def test_dq003_accepts_a_valid_enrollment_date(good: str) -> None:
    assert run("DQ003", [make_row(enrollment_date=good)]) == []


def test_dq003_uses_the_date_format_of_the_rows_source_system() -> None:
    ind_row = make_row(source_system="IND", enrollment_date="1/1/2022")

    assert run("DQ003", [ind_row]) == []


# ---- DQ004: duplicate member_key inside one file -------------------------------------------


def test_dq004_rejects_earlier_duplicates_and_keeps_the_last() -> None:
    rows = [make_row(2), make_row(3), make_row(4)]  # all SKYPOINTS:223457

    assert hits(run("DQ004", rows)) == {(2, "member_key"), (3, "member_key")}


def test_dq004_keeps_the_row_with_the_highest_row_number_whatever_the_list_order() -> None:
    rows = [make_row(4), make_row(2), make_row(3)]

    assert {f.file_row_number for f in run("DQ004", rows)} == {2, 3}


def test_dq004_ignores_the_same_key_in_different_files() -> None:
    rows = [make_row(2, file_name="a.dat"), make_row(2, file_name="b.dat")]

    assert run("DQ004", rows) == []


def test_dq004_ignores_rows_without_a_member_key() -> None:
    rows = [make_row(2, member_key=None), make_row(3, member_key=None)]

    assert run("DQ004", rows) == []


def test_dq004_treats_different_source_systems_as_different_keys() -> None:
    rows = [make_row(2, member_key="IND:1"), make_row(3, member_key="USA:1")]

    assert run("DQ004", rows) == []


# ---- DQ005: spec lengths -------------------------------------------------------------------

LENGTHS = [
    ("member_name", 255), ("member_id", 18), ("tier_code", 5), ("agent_name", 255),
    ("state", 5), ("country", 5), ("post_code", 5), ("active_member", 1),
]  # fmt: skip


@pytest.mark.parametrize(("column", "limit"), LENGTHS)
def test_dq005_accepts_a_value_exactly_at_the_limit(column: str, limit: int) -> None:
    assert run("DQ005", [make_row(**{column: "x" * limit})]) == []


@pytest.mark.parametrize(("column", "limit"), LENGTHS)
def test_dq005_rejects_a_value_one_over_the_limit(column: str, limit: int) -> None:
    findings = run("DQ005", [make_row(**{column: "x" * (limit + 1)})])

    assert hits(findings) == {(2, column)}


def test_dq005_reports_each_overlong_column() -> None:
    findings = run("DQ005", [make_row(state="x" * 6, country="y" * 6)])

    assert hits(findings) == {(2, "state"), (2, "country")}


# ---- DQ006 / DQ007: optional dates ---------------------------------------------------------


@pytest.mark.parametrize(
    ("column", "bad"),
    [
        ("last_flight_date", "20231301"),
        ("last_flight_date", "abc"),
        ("date_of_birth", "3051985"),  # leading zero lost
        ("date_of_birth", "13051985"),
        ("date_of_birth", "02291999"),
    ],
)
def test_dq006_fires_for_an_optional_date_that_is_present_but_invalid(column: str, bad: str) -> None:
    assert hits(run("DQ006", [make_row(**{column: bad})])) == {(2, column)}


@pytest.mark.parametrize("missing", [None, "", "   ", "NULL", "null"])
def test_dq006_ignores_a_missing_optional_date(missing) -> None:
    row = make_row(last_flight_date=missing, date_of_birth=missing)

    assert run("DQ006", [row]) == []


def test_dq006_does_not_look_at_the_mandatory_enrollment_date() -> None:
    assert run("DQ006", [make_row(enrollment_date="20211313")]) == []  # that is DQ003


def test_dq006_leaves_an_ambiguous_date_to_dq007() -> None:
    assert run("DQ006", [make_row(source_system="USA", last_flight_date="1052022")]) == []


@pytest.mark.parametrize("column", ["last_flight_date", "date_of_birth"])
@pytest.mark.parametrize("ambiguous", ["1052022", "1152022"])
def test_dq007_fires_for_an_ambiguous_date(column: str, ambiguous: str) -> None:
    findings = run("DQ007", [make_row(source_system="USA", **{column: ambiguous})])

    assert hits(findings) == {(2, column)}
    assert findings[0].value == ambiguous


def test_dq007_accepts_a_date_with_one_valid_reading() -> None:
    assert run("DQ007", [make_row(source_system="USA", last_flight_date="6152022")]) == []


def test_dq007_cannot_fire_for_a_format_with_separators_or_padding() -> None:
    assert run("DQ007", [make_row(source_system="SKYPOINTS", last_flight_date="20240101")]) == []


def test_dq007_does_not_fire_for_a_missing_date() -> None:
    assert run("DQ007", [make_row(source_system="USA", last_flight_date=None)]) == []


# ---- DQ008: date of birth plausibility -----------------------------------------------------


@pytest.mark.parametrize(
    "dob",
    [
        pytest.param("01162024", id="born-tomorrow"),
        pytest.param("01011900", id="age-123"),
        pytest.param("01141903", id="age-121"),
    ],
)
def test_dq008_fires_for_a_future_dob_or_an_age_above_max_age(dob: str) -> None:
    findings = run("DQ008", [make_row(date_of_birth=dob)])

    assert hits(findings) == {(2, "date_of_birth")}
    assert findings[0].value == dob  # flag only: the value is reported as received


@pytest.mark.parametrize(
    "dob",
    [
        pytest.param("01151904", id="age-exactly-120"),
        pytest.param("01152024", id="born-on-as-of"),
        pytest.param("03051985", id="ordinary"),
    ],
)
def test_dq008_accepts_the_edges_of_the_valid_range(dob: str) -> None:
    assert run("DQ008", [make_row(date_of_birth=dob)]) == []


def test_dq008_uses_max_age_from_the_config() -> None:
    config = QualityConfig(max_age=30)

    assert hits(run("DQ008", [make_row(date_of_birth="03051985")], config=config)) == {
        (2, "date_of_birth")
    }


@pytest.mark.parametrize("dob", [None, "", "3051985", "13051985"])
def test_dq008_ignores_a_missing_or_unreadable_dob(dob) -> None:
    assert run("DQ008", [make_row(date_of_birth=dob)]) == []  # DQ006 covers unreadable values


# ---- DQ009: flight date sequence ------------------------------------------------------------


def test_dq009_fires_for_a_flight_before_enrollment() -> None:
    findings = run("DQ009", [make_row(enrollment_date="20101012", last_flight_date="20101011")])

    assert hits(findings) == {(2, "last_flight_date")}


def test_dq009_fires_for_a_flight_after_as_of() -> None:
    assert hits(run("DQ009", [make_row(last_flight_date="20240116")])) == {(2, "last_flight_date")}


@pytest.mark.parametrize(
    ("enrollment", "flight"),
    [
        pytest.param("20101012", "20101012", id="flew-on-enrollment-day"),
        pytest.param("20101012", "20240115", id="flew-on-as-of"),
        pytest.param("20101012", None, id="no-flight"),
        pytest.param("20101012", "abc", id="unreadable-flight"),
        pytest.param("20211313", "20101012", id="unreadable-enrollment"),
    ],
)
def test_dq009_accepts_valid_sequences_and_ignores_unreadable_dates(enrollment, flight) -> None:
    assert run("DQ009", [make_row(enrollment_date=enrollment, last_flight_date=flight)]) == []


def test_dq009_still_checks_as_of_when_enrollment_is_missing() -> None:
    findings = run("DQ009", [make_row(enrollment_date=None, last_flight_date="20240116")])

    assert hits(findings) == {(2, "last_flight_date")}


def test_dq009_depends_on_the_explicit_as_of() -> None:
    row = make_row(last_flight_date="20240116")

    assert run("DQ009", [row], as_of=date(2024, 1, 15)) != []
    assert run("DQ009", [row], as_of=date(2024, 1, 16)) == []


# ---- DQ010 / DQ017: country ----------------------------------------------------------------


@pytest.mark.parametrize("bad", [None, "", "   ", "ZZZ", "XYZ"])
def test_dq010_routes_a_blank_or_unknown_country(bad) -> None:
    findings = run("DQ010", [make_row(country=bad)])

    assert hits(findings) == {(2, "country")}
    assert findings[0].severity is Severity.ROUTE


@pytest.mark.parametrize("good", ["USA", "usa", "IND", "PHIL", "AU", "PHL"])
def test_dq010_accepts_a_country_that_resolves_including_by_alias(good: str) -> None:
    assert run("DQ010", [make_row(country=good)]) == []


@pytest.mark.parametrize("alias", ["PHIL", "AU", "US", "phil"])
def test_dq017_flags_a_country_resolved_through_an_alias(alias: str) -> None:
    findings = run("DQ017", [make_row(country=alias)])

    assert hits(findings) == {(2, "country")}
    assert findings[0].value == alias


@pytest.mark.parametrize("good", ["USA", "usa", "IND", "CAN", "PHL"])
def test_dq017_accepts_a_three_letter_iso_code(good: str) -> None:
    assert run("DQ017", [make_row(country=good)]) == []


@pytest.mark.parametrize("unresolved", [None, "", "XYZ"])
def test_dq017_does_not_fire_when_the_country_does_not_resolve(unresolved) -> None:
    assert run("DQ017", [make_row(country=unresolved)]) == []


def test_dq010_and_dq017_are_exclusive_phil_resolves_xyz_does_not() -> None:
    rows = [make_row(2, country="PHIL"), make_row(3, country="XYZ")]

    assert hits(run("DQ010", rows)) == {(3, "country")}
    assert hits(run("DQ017", rows)) == {(2, "country")}


def test_country_rules_use_the_country_map_from_the_config() -> None:
    config = QualityConfig(country_map={"GER": "DEU", "DEU": "DEU"})
    rows = [make_row(2, country="GER"), make_row(3, country="USA")]

    assert hits(run("DQ010", rows, config=config)) == {(3, "country")}
    assert hits(run("DQ017", rows, config=config)) == {(2, "country")}


# ---- DQ011 / DQ012 / DQ013 -------------------------------------------------------------------


@pytest.mark.parametrize("tier", ["GLD", "SLV", "PLT"])
def test_dq011_accepts_the_allowed_tiers(tier: str) -> None:
    assert run("DQ011", [make_row(tier_code=tier)]) == []


@pytest.mark.parametrize("tier", ["ABC", "gld", "GOLD"])
def test_dq011_flags_a_tier_that_is_not_allowed(tier: str) -> None:
    assert hits(run("DQ011", [make_row(tier_code=tier)])) == {(2, "tier_code")}


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_dq011_allows_a_blank_tier(blank) -> None:
    assert run("DQ011", [make_row(tier_code=blank)]) == []


def test_dq011_uses_the_tiers_from_the_config() -> None:
    config = QualityConfig(tiers=frozenset({"BRZ"}))

    assert hits(run("DQ011", [make_row(tier_code="GLD")], config=config)) == {(2, "tier_code")}
    assert run("DQ011", [make_row(tier_code="BRZ")], config=config) == []


@pytest.mark.parametrize("value", ["A", "I"])
def test_dq012_accepts_the_allowed_active_values(value: str) -> None:
    assert run("DQ012", [make_row(active_member=value)]) == []


@pytest.mark.parametrize("value", ["X", "a", "Y"])
def test_dq012_flags_a_value_that_is_not_allowed(value: str) -> None:
    assert hits(run("DQ012", [make_row(active_member=value)])) == {(2, "active_member")}


@pytest.mark.parametrize("blank", [None, "", "   "])
def test_dq012_allows_a_blank_value(blank) -> None:
    assert run("DQ012", [make_row(active_member=blank)]) == []


def test_dq012_uses_the_active_values_from_the_config() -> None:
    config = QualityConfig(active_values=frozenset({"Y", "N"}))

    assert hits(run("DQ012", [make_row(active_member="A")], config=config)) == {(2, "active_member")}
    assert run("DQ012", [make_row(active_member="Y")], config=config) == []


@pytest.mark.parametrize("member_id", ["12345", "2256", "1"])
def test_dq013_flags_an_id_shorter_than_the_expected_length(member_id: str) -> None:
    assert hits(run("DQ013", [make_row(member_id=member_id)])) == {(2, "member_id")}


@pytest.mark.parametrize("member_id", ["223457", "2234567", "000789"])
def test_dq013_accepts_an_id_at_or_above_the_expected_length(member_id: str) -> None:
    assert run("DQ013", [make_row(member_id=member_id)]) == []


def test_dq013_leaves_a_missing_id_to_dq002() -> None:
    assert run("DQ013", [make_row(member_id=None)]) == []


def test_dq013_uses_the_expected_length_from_the_config() -> None:
    config = QualityConfig(expected_member_id_length=8)

    assert hits(run("DQ013", [make_row(member_id="223457")], config=config)) == {(2, "member_id")}


# ---- DQ014: header --------------------------------------------------------------------------


def header_findings(columns: list[str]) -> list[Finding]:
    return run("DQ014", [], header=HeaderInfo("f.dat", columns))


def test_dq014_accepts_the_full_header_in_any_order() -> None:
    assert header_findings(list(reversed(FULL_HEADER))) == []


@pytest.mark.parametrize("mandatory", ["Member_Name", "Member_Id", "Enrollment_Date"])
def test_dq014_fails_the_file_for_a_missing_mandatory_column(mandatory: str) -> None:
    (finding,) = header_findings([c for c in FULL_HEADER if c != mandatory])

    assert finding.severity is Severity.FAIL_FILE
    assert finding.column_name == mandatory
    assert finding.file_name == "f.dat"
    assert finding.file_row_number is None


def test_dq014_fails_the_file_for_an_unknown_column() -> None:
    (finding,) = header_findings([*FULL_HEADER, "Region"])

    assert (finding.severity, finding.column_name) == (Severity.FAIL_FILE, "Region")


def test_dq014_fails_the_file_for_a_duplicate_column() -> None:
    (finding,) = header_findings([*FULL_HEADER, "State"])

    assert (finding.severity, finding.column_name) == (Severity.FAIL_FILE, "State")


def test_dq014_warns_for_a_missing_optional_column_such_as_post_code() -> None:
    (finding,) = header_findings(SAMPLE_HEADER)

    assert finding.severity is Severity.WARN
    assert finding.column_name == "Post_Code"
    assert finding.file_name == "f.dat"
    assert finding.file_row_number is None
    assert finding.member_key is None


def test_dq014_warns_once_for_each_missing_optional_column() -> None:
    findings = header_findings([c for c in SAMPLE_HEADER if c not in ("Agent_Name", "State")])

    assert {(f.severity, f.column_name) for f in findings} == {
        (Severity.WARN, "Post_Code"),
        (Severity.WARN, "Agent_Name"),
        (Severity.WARN, "State"),
    }


def test_dq014_reports_every_problem_in_one_header() -> None:
    columns = [c for c in FULL_HEADER if c not in ("Member_Id", "Post_Code")] + ["Region", "State"]

    findings = header_findings(columns)

    assert {(f.severity, f.column_name) for f in findings} == {
        (Severity.FAIL_FILE, "Member_Id"),
        (Severity.WARN, "Post_Code"),
        (Severity.FAIL_FILE, "Region"),
        (Severity.FAIL_FILE, "State"),
    }


def test_dq014_does_nothing_without_a_header() -> None:
    assert run("DQ014", [make_row()]) == []


# ---- DQ015 / DQ016: reconciliation -----------------------------------------------------------


@pytest.mark.parametrize(
    ("raw", "staged", "rejected"),
    [(5, 5, 0), (5, 4, 1), (5, 0, 5), (0, 0, 0)],
)
def test_dq015_accepts_counts_that_add_up(raw: int, staged: int, rejected: int) -> None:
    assert run("DQ015", [], counts=BatchCounts(raw, staged, rejected)) == []


@pytest.mark.parametrize(
    ("raw", "staged", "rejected"),
    [(5, 4, 0), (5, 3, 1), (5, 5, 1), (5, 6, 0)],
)
def test_dq015_fails_the_batch_when_rows_are_lost_or_invented(
    raw: int, staged: int, rejected: int
) -> None:
    (finding,) = run("DQ015", [], counts=BatchCounts(raw, staged, rejected))

    assert finding.severity is Severity.FAIL_BATCH
    assert finding.file_row_number is None


def test_dq015_does_nothing_without_counts() -> None:
    assert run("DQ015", []) == []


def test_dq016_fails_the_batch_for_each_text_column_that_lost_values() -> None:
    counts = BatchCounts(
        5, 5, 0,
        raw_nonblank={"agent_name": 5, "state": 5, "member_name": 5},
        staged_nonblank={"agent_name": 1, "state": 5, "member_name": 4},
    )  # fmt: skip

    findings = run("DQ016", [], counts=counts)

    assert {f.column_name for f in findings} == {"agent_name", "member_name"}
    assert {f.severity for f in findings} == {Severity.FAIL_BATCH}


def test_dq016_has_zero_tolerance() -> None:
    counts = BatchCounts(5, 5, 0, {"agent_name": 5}, {"agent_name": 4})

    assert {f.column_name for f in run("DQ016", [], counts=counts)} == {"agent_name"}


def test_dq016_accepts_equal_or_higher_staged_counts() -> None:
    counts = BatchCounts(5, 5, 0, {"agent_name": 3, "state": 3}, {"agent_name": 3, "state": 4})

    assert run("DQ016", [], counts=counts) == []


def test_dq016_looks_at_text_columns_only() -> None:
    counts = BatchCounts(
        5, 5, 0,
        raw_nonblank={"post_code": 5, "date_of_birth": 5, "enrollment_date": 5},
        staged_nonblank={"post_code": 0, "date_of_birth": 0, "enrollment_date": 0},
    )  # fmt: skip

    assert run("DQ016", [], counts=counts) == []


@pytest.mark.parametrize(
    "column",
    ["member_name", "member_id", "tier_code", "agent_name", "state", "country", "active_member"],
)
def test_dq016_covers_every_text_column(column: str) -> None:
    counts = BatchCounts(5, 5, 0, {column: 5}, {column: 0})

    assert {f.column_name for f in run("DQ016", [], counts=counts)} == {column}


def test_dq016_does_nothing_without_counts() -> None:
    assert run("DQ016", []) == []


# ---- the brief's sample, all rules -----------------------------------------------------------

BRIEF_ROWS = [  # (name, id, tier, state, country) - the rest is identical in every row
    ("Elena", "223457", "GLD", "CA", "USA"),
    ("Ravi", "223458", "SLV", "TN", "IND"),
    ("Mateo", "223459", "GLD", "NCR", "PHIL"),
    ("Nora", "22345", "PLT", "ONT", "CAN"),
    ("Jacob", "2256", "SLV", "VIC", "AU"),
]


def brief_rows() -> list[PreparedRow]:
    return [
        make_row(
            row_number,
            member_name=name, member_id=member_id, member_key=f"SKYPOINTS:{member_id}",
            tier_code=tier, state=state, country=country, post_code=None,
        )  # fmt: skip
        for row_number, (name, member_id, tier, state, country) in enumerate(BRIEF_ROWS, start=2)
    ]


def test_the_brief_sample_fires_only_the_expected_rules() -> None:
    ctx = RuleContext(
        rows=brief_rows(),
        as_of=AS_OF,
        header=HeaderInfo("SKYPOINTS_MEMBERS_20240115_13453025.dat", SAMPLE_HEADER),
    )

    findings = apply_rules(ctx)

    assert {(f.rule_code, f.file_row_number) for f in findings} == {
        ("DQ014", None),  # Post_Code missing from the header (file level)
        ("DQ013", 5),  # Nora, id 22345
        ("DQ013", 6),  # Jacob, id 2256
        ("DQ017", 4),  # Mateo, PHIL
        ("DQ017", 6),  # Jacob, AU
    }
    assert {f.severity for f in findings if f.rule_code == "DQ014"} == {Severity.WARN}
    assert not {f.severity for f in findings} & {Severity.REJECT, Severity.ROUTE}


def test_a_prepared_row_with_the_leading_zero_lost_gives_dq006_on_dob() -> None:
    row = make_row(date_of_birth="3051985")  # 03051985 after the zero was dropped

    findings = apply_rules(RuleContext(rows=[row], as_of=AS_OF))

    dq006 = [f for f in findings if f.rule_code == "DQ006"]
    assert [(f.column_name, f.severity, f.value) for f in dq006] == [
        ("date_of_birth", Severity.WARN, "3051985")
    ]


def test_apply_rules_runs_every_rule_and_returns_a_flat_list_of_findings() -> None:
    row = make_row(member_name=None, tier_code="ABC", country="ZZZ")

    codes = {f.rule_code for f in apply_rules(RuleContext(rows=[row], as_of=AS_OF))}

    assert codes == {"DQ001", "DQ010", "DQ011"}


def test_apply_rules_on_valid_rows_returns_nothing() -> None:
    assert apply_rules(RuleContext(rows=[make_row()], as_of=AS_OF)) == []
