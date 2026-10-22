"""Unit tests for scripts/show.py (loaded from its path: scripts/ is not a package)."""

import importlib.util
import sys
from pathlib import Path

REPO = Path(__file__).resolve().parents[2]


def load_show():
    spec = importlib.util.spec_from_file_location("show", REPO / "scripts" / "show.py")
    module = importlib.util.module_from_spec(spec)
    sys.modules["show"] = module  # a dataclass in the script looks its module up here
    spec.loader.exec_module(module)
    return module


show = load_show()


# ---- format_table --------------------------------------------------------------------------------


def test_a_table_has_a_header_a_dashed_line_the_rows_and_a_footer() -> None:
    text = show.format_table(["a", "b"], [(1, "x"), (22, None)])

    assert text == "a   b\n--  ----\n1   x\n22  NULL\n(2 rows)"


def test_a_single_row_says_row_not_rows() -> None:
    assert show.format_table(["n"], [(5,)]).splitlines()[-1] == "(1 row)"


def test_an_empty_result_shows_the_header_and_zero_rows() -> None:
    text = show.format_table(["rule_code", "n"], [])

    assert text == "rule_code  n\n---------  -\n(0 rows)"


def test_columns_are_as_wide_as_their_widest_value() -> None:
    lines = show.format_table(["name"], [("Elena",), ("Ravi",)]).splitlines()

    assert lines[0] == "name" and lines[1] == "-----"  # 5 wide because of Elena


def test_no_line_has_trailing_spaces() -> None:
    text = show.format_table(["a", "b"], [("x", ""), ("yyy", "z")])

    assert all(line == line.rstrip() for line in text.splitlines())


def test_booleans_and_dates_print_as_text_people_expect() -> None:
    from datetime import date, datetime

    text = show.format_table(
        ["ok", "d", "ts"], [(True, date(2024, 1, 15), datetime(2024, 1, 15, 9, 0))]
    )

    assert text.splitlines()[2] == "true  2024-01-15  2024-01-15 09:00:00"


def test_a_value_longer_than_max_width_is_cut_with_an_ellipsis() -> None:
    long = "x" * 100

    row = show.format_table(["v"], [(long,)], max_width=10).splitlines()[2]

    assert row == "x" * 9 + "…"


def test_a_value_exactly_max_width_long_is_not_cut() -> None:
    assert show.format_table(["v"], [("x" * 10,)], max_width=10).splitlines()[2] == "x" * 10


def test_multi_line_values_are_shown_on_one_line() -> None:
    row = show.format_table(["v"], [("a\nb",)]).splitlines()[2]

    assert row == "a b"


# ---- parse_queries ---------------------------------------------------------------------------------


def test_queries_are_split_at_the_q_comments_and_their_semicolons() -> None:
    text = (
        "-- header comment\n"
        "-- Q1. First query\n"
        "SELECT 1 AS one;\n"
        "\n"
        "-- Q2. Second query, over two lines\n"
        "-- a comment inside\n"
        "SELECT 2\n"
        "  AS two;\n"
    )

    queries = show.parse_queries(text)

    assert [(q.number, q.title) for q in queries] == [
        (1, "First query"),
        (2, "Second query, over two lines"),
    ]
    assert queries[0].sql == "SELECT 1 AS one"
    assert queries[1].sql.split() == ["SELECT", "2", "AS", "two"]


def test_the_demo_file_has_twelve_numbered_queries() -> None:
    queries = show.parse_queries(
        (REPO / "scripts" / "demo_queries.sql").read_text(encoding="utf-8")
    )

    assert [q.number for q in queries] == list(range(1, 13))
    assert all(q.title and q.sql.upper().startswith("SELECT") for q in queries)
    assert not any(q.sql.rstrip().endswith(";") for q in queries)
