"""The rule table. The logic of every rule is SQL (sql/duckdb/05_validations.sql, views V_DQ0nn);
`check` loads a RuleContext into the engine and returns that view's findings."""

from __future__ import annotations

from dataclasses import dataclass

from skypoints.quality import engine
from skypoints.quality.model import Finding, RuleContext, Severity


@dataclass(frozen=True)
class Rule:
    code: str
    description: str
    severity: Severity  # DQ014 is FAIL_FILE; its missing-optional-column findings are WARN

    def check(self, ctx: RuleContext) -> list[Finding]:
        con = engine.scratch_connection()
        engine.load_context(con, ctx)
        return engine.fetch_findings(con, f"dq.v_{self.code.lower()}")


_R, _W, _RT, _FF, _FB = (
    Severity.REJECT, Severity.WARN, Severity.ROUTE, Severity.FAIL_FILE, Severity.FAIL_BATCH,
)  # fmt: skip

RULES: dict[str, Rule] = {
    r.code: r
    for r in (
        Rule("DQ001", "member_name blank or NULL", _R),
        Rule("DQ002", "member_id blank or NULL", _R),
        Rule("DQ003", "enrollment_date blank or not a valid date", _R),
        Rule("DQ004", "duplicate member_key within one file: earlier rows rejected, last kept", _R),
        Rule("DQ005", "value longer than the spec length", _R),
        Rule("DQ006", "optional date present but INVALID (value becomes NULL)", _W),
        Rule("DQ007", "date AMBIGUOUS, e.g. MDYYYY 1052022 (value becomes NULL)", _W),
        Rule("DQ008", "date of birth in the future or age above max_age (flag only)", _W),
        Rule("DQ009", "last_flight_date before enrollment_date or after as_of", _W),
        Rule("DQ010", "country blank or not in the country map", _RT),
        Rule("DQ011", "tier present but not an allowed tier", _W),
        Rule("DQ012", "is_active present but not an allowed value", _W),
        Rule("DQ013", "member_id shorter than the expected length", _W),
        Rule(
            "DQ014", "header: missing mandatory/unknown/duplicate column, or missing optional", _FF
        ),
        Rule("DQ015", "raw row count differs from staged plus rejected rows", _FB),
        Rule("DQ016", "non-blank count of a text column is higher in raw than in staging", _FB),
        Rule("DQ017", "country resolved through an alias, not a 3-letter ISO code", _W),
    )
}


def apply_rules(ctx: RuleContext, rules: dict[str, Rule] | None = None) -> list[Finding]:
    """Run the rules on the context and return all findings. The runner decides what to do."""
    selected = RULES if rules is None else rules
    con = engine.scratch_connection()
    engine.load_context(con, ctx)
    return engine.fetch_findings(con, "dq.v_findings", rule_codes=list(selected))
