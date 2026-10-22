"""Profile every source file in data/raw. Read-only: reports, never fixes.

Usage: python scripts/profile_sources.py [raw_dir]
"""

from __future__ import annotations

import json
import re
import sys
from collections.abc import Callable
from datetime import datetime
from pathlib import Path

import pandas as pd

RAW_DIR = Path(__file__).resolve().parents[1] / "data" / "raw"
LOW_CARDINALITY_MAX = 10
DATE_COLUMN_MIN_RATIO = 0.8  # share of non-null values that must parse to call a column a date
NULL_TOKENS = {"NULL", "NONE", "N/A", "NA", "NAN", "NIL", "-"}
DATE_NAME_HINT = re.compile(r"date|dob|enrol|flight", re.IGNORECASE)


def _strptime(fmt: str) -> Callable[[str], bool]:
    def check(value: str) -> bool:
        try:
            datetime.strptime(value, fmt)
        except ValueError:
            return False
        return True

    return check


def _digits(pattern: str, fmt: str) -> Callable[[str], bool]:
    """Fixed-width digit string split by a regex into (a, b, c) and validated via strptime."""
    rx = re.compile(pattern)

    def check(value: str) -> bool:
        m = rx.fullmatch(value)
        return bool(m) and _strptime(fmt)(" ".join(m.groups()))

    return check


DATE_FORMATS: dict[str, Callable[[str], bool]] = {
    "YYYYMMDD": _digits(r"(\d{4})(\d{2})(\d{2})", "%Y %m %d"),
    "MMDDYYYY": _digits(r"(\d{2})(\d{2})(\d{4})", "%m %d %Y"),
    "DDMMYYYY": _digits(r"(\d{2})(\d{2})(\d{4})", "%d %m %Y"),
    "M/D/YYYY": _strptime("%m/%d/%Y"),
    "YYYY-MM-DD": _strptime("%Y-%m-%d"),
    "MDDYYYY (unpadded month)": _digits(r"(\d)(\d{2})(\d{4})", "%m %d %Y"),
    "MMDYYYY (unpadded day, extra)": _digits(r"(\d{2})(\d)(\d{4})", "%m %d %Y"),
}


def is_blank(value: object) -> bool:
    return (
        value is None or (isinstance(value, float) and pd.isna(value)) or str(value).strip() == ""
    )


def is_null_token(value: object) -> bool:
    return not is_blank(value) and str(value).strip().upper() in NULL_TOKENS


def non_missing(series: pd.Series) -> list[str]:
    return [str(v).strip() for v in series if not is_blank(v) and not is_null_token(v)]


def date_format_hits(values: list[str]) -> dict[str, int]:
    return {name: sum(check(v) for v in values) for name, check in DATE_FORMATS.items()}


def looks_like_date(name: str, hits: dict[str, int], n_values: int) -> bool:
    best = max(hits.values(), default=0)
    if n_values and best / n_values >= DATE_COLUMN_MIN_RATIO:
        return True
    return bool(DATE_NAME_HINT.search(name))


def load_json(path: Path) -> pd.DataFrame:
    """Explode member -> redemptions into one row per redemption; every value as text."""
    rows: list[dict[str, str | None]] = []
    for member in json.loads(path.read_text(encoding="utf-8")):
        parents = {k: v for k, v in member.items() if k != "redemptions"}
        children = member.get("redemptions") or [{}]
        for child in children:
            merged = {**parents, **child}
            rows.append({k: None if v is None else str(v) for k, v in merged.items()})
    return pd.DataFrame(rows, dtype="string")


def load_file(path: Path) -> pd.DataFrame:
    suffix = path.suffix.lower()
    if suffix == ".csv":
        return pd.read_csv(path, dtype=str, keep_default_na=False)
    if suffix in {".xlsx", ".xls"}:
        return pd.read_excel(path, dtype=str, keep_default_na=False)
    if suffix == ".dat":
        return pd.read_csv(path, sep="|", dtype=str, keep_default_na=False)
    if suffix == ".json":
        return load_json(path)
    raise ValueError(f"unsupported file type: {path.name}")


def profile_column(name: str, series: pd.Series) -> list[str]:
    n = len(series)
    blanks = sum(is_blank(v) for v in series)
    tokens = sum(is_null_token(v) for v in series)
    values = non_missing(series)
    distinct = sorted(set(values))
    lines = [f"  - {name!r}: blank={blanks} null_token={tokens} non_missing={len(values)}/{n}"]
    if 0 < len(distinct) <= LOW_CARDINALITY_MAX:
        lines.append(f"      distinct ({len(distinct)}): {distinct}")
    hits = date_format_hits(values)
    if looks_like_date(name, hits, len(values)):
        lines.append("      date formats parsed:")
        for fmt, count in hits.items():
            lines.append(f"        {fmt:<32} {count}/{len(values)}")
        failed = [v for v in values if not any(c(v) for c in DATE_FORMATS.values())]
        if failed:
            lines.append(f"        parsed by NO format: {failed}")
    return lines


def profile_file(path: Path) -> str:
    df = load_file(path)
    out = [
        f"=== {path.name}",
        f"rows: {len(df)}",
        f"columns ({len(df.columns)}): {list(df.columns)}",
    ]
    for col in df.columns:
        out.extend(profile_column(str(col), df[col]))
    return "\n".join(out)


def main(raw_dir: Path) -> int:
    files = sorted(p for p in raw_dir.iterdir() if p.is_file())
    if not files:
        print(f"no files in {raw_dir}")
        return 1
    for path in files:
        try:
            print(profile_file(path), end="\n\n")
        except ValueError as exc:
            print(f"=== {path.name}\nSKIPPED: {exc}\n")
    return 0


if __name__ == "__main__":
    sys.exit(main(Path(sys.argv[1]) if len(sys.argv) > 1 else RAW_DIR))
