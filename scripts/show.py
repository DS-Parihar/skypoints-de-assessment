"""Print the demo queries (scripts/demo_queries.sql) as readable text tables.

    python scripts/show.py                       # all queries, data/processed/skypoints.duckdb
    python scripts/show.py --only 3,11           # just Q3 and Q11
    python scripts/show.py --db other.duckdb --width 40

Needs only the duckdb package (not the skypoints package), and opens the database read-only: a
missing database is an error ("run the pipeline first"), never an empty file.
Exit code: 0 on success, 2 for a missing database or an unknown query number.
"""

from __future__ import annotations

import argparse
import re
import sys
from collections.abc import Sequence
from dataclasses import dataclass
from pathlib import Path

import duckdb

REPO = Path(__file__).resolve().parents[1]
DEFAULT_DB = REPO / "data" / "processed" / "skypoints.duckdb"
QUERIES_FILE = Path(__file__).resolve().parent / "demo_queries.sql"
_HEADER = re.compile(r"^--\s*Q(\d+)\.\s*(.*?)\s*$")


@dataclass(frozen=True)
class Query:
    number: int  # the n of "-- Qn. title"
    title: str
    sql: str  # one statement, without the trailing semicolon


def parse_queries(sql_text: str) -> list[Query]:
    """Split demo_queries.sql into queries. A query starts at a '-- Q<n>. <title>' comment line
    and runs to the next semicolon; other comments are ignored."""
    queries: list[Query] = []
    number, title, lines = None, "", []
    for line in sql_text.splitlines():
        header = _HEADER.match(line)
        if header:
            number, title, lines = int(header.group(1)), header.group(2), []
        elif number is not None and not line.strip().startswith("--") and line.strip():
            lines.append(line)
            if line.rstrip().endswith(";"):
                sql = "\n".join(lines).strip().rstrip(";").strip()
                queries.append(Query(number, title, sql))
                number, lines = None, []
    return queries


def _cell(value: object, max_width: int) -> str:
    if value is None:
        text = "NULL"
    elif isinstance(value, bool):
        text = "true" if value else "false"
    else:
        text = str(value)
    text = " ".join(text.split("\n")).replace("\r", "")
    return text if len(text) <= max_width else text[: max_width - 1] + "…"


def format_table(columns: Sequence[str], rows: Sequence[Sequence], max_width: int = 60) -> str:
    """A text table: header, a dashed line, the rows (NULL as NULL, booleans as true/false, values
    longer than max_width cut with an ellipsis), and a '(n rows)' footer. No trailing spaces."""
    cells = [[_cell(value, max_width) for value in row] for row in rows]
    widths = [max([len(name), *(len(row[i]) for row in cells)]) for i, name in enumerate(columns)]

    def line(values: Sequence[str]) -> str:
        return "  ".join(value.ljust(width) for value, width in zip(values, widths)).rstrip()

    count = len(cells)
    footer = f"({count} row{'' if count == 1 else 's'})"
    return "\n".join([line(columns), line(["-" * w for w in widths]), *map(line, cells), footer])


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="Print the demo queries as text tables.")
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB, help=f"DuckDB file (default {DEFAULT_DB})"
    )
    parser.add_argument("--only", help="comma separated query numbers, for example 3,11")
    parser.add_argument(
        "--width", type=int, default=60, help="cut values longer than this (default 60)"
    )
    args = parser.parse_args(argv)

    if not args.db.is_file():
        print(f"database not found: {args.db}. Run the pipeline first.", file=sys.stderr)
        return 2
    queries = parse_queries(QUERIES_FILE.read_text(encoding="utf-8"))
    if args.only is not None:
        wanted = args.only.split(",")
        known = {str(q.number) for q in queries}
        unknown = [w for w in wanted if w.strip() not in known]
        if unknown:
            print(f"unknown query number(s): {', '.join(repr(u) for u in unknown)}; "
                  f"choose from 1-{len(queries)}", file=sys.stderr)  # fmt: skip
            return 2
        queries = [q for q in queries if str(q.number) in {w.strip() for w in wanted}]

    con = duckdb.connect(str(args.db), read_only=True)
    try:
        for query in queries:
            result = con.execute(query.sql)
            columns = [d[0] for d in result.description]
            print(f"Q{query.number}. {query.title}")
            print(format_table(columns, result.fetchall(), args.width))
            print()
    finally:
        con.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
