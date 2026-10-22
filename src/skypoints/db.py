"""DuckDB connection helpers: open a database and apply the sql/duckdb DDL in order."""

from __future__ import annotations

from pathlib import Path

import duckdb

DEFAULT_DDL_DIR = Path(__file__).resolve().parents[2] / "sql" / "duckdb"


def ddl_files(ddl_dir: Path) -> list[Path]:
    """Return the .sql files in `ddl_dir`, sorted by name (00_..., 01_..., ...)."""
    if not ddl_dir.is_dir():
        raise FileNotFoundError(f"DDL directory not found: {ddl_dir}")
    return sorted(ddl_dir.glob("*.sql"))


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
