"""DuckDB connection helpers: open a database and apply the sql/duckdb DDL in order."""

from __future__ import annotations

import re
from collections.abc import Iterator
from contextlib import contextmanager
from pathlib import Path

import duckdb

DEFAULT_DDL_DIR = Path(__file__).resolve().parents[2] / "sql" / "duckdb"
_NUMBERED_SQL = re.compile(r"\d{2}_.*\.sql")  # 00_setup.sql, 01_raw.sql, ...


@contextmanager
def atomic(con: duckdb.DuckDBPyConnection) -> Iterator[None]:
    """Run a block in one transaction; join the caller's transaction if one is already open."""
    try:
        con.execute("BEGIN TRANSACTION")
    except duckdb.TransactionException:
        yield
        return
    try:
        yield
    except BaseException:
        con.execute("ROLLBACK")
        raise
    con.execute("COMMIT")


def ddl_files(ddl_dir: Path) -> list[Path]:
    """Return the numbered DDL files in `ddl_dir` (00_..., 01_..., ...), sorted by name.

    Other .sql files, such as load_staging.sql, need bound parameters and are not DDL.
    """
    if not ddl_dir.is_dir():
        raise FileNotFoundError(f"DDL directory not found: {ddl_dir}")
    return sorted(p for p in ddl_dir.glob("*.sql") if _NUMBERED_SQL.fullmatch(p.name))


def create_database(
    path: str | Path | None = None,
    ddl_dir: Path = DEFAULT_DDL_DIR,
) -> duckdb.DuckDBPyConnection:
    """Open a DuckDB connection and run every DDL file in `ddl_dir` in name order.

    `path=None` gives an in-memory database; otherwise the database file is created or
    reopened. The DDL is idempotent, so calling this on an existing file is safe.
    """
    files = ddl_files(ddl_dir)
    connection = duckdb.connect(":memory:" if path is None else str(path))
    try:
        for ddl_file in files:
            connection.execute(ddl_file.read_text(encoding="utf-8"))
    except Exception:
        connection.close()
        raise
    return connection
