"""End-to-end pipeline CLI.

    python -m skypoints.pipeline --as-of 2024-01-15 --db data/processed/skypoints.duckdb

Steps (members: files in file_ts order, one batch per file; then redemptions):
  create schema (sql/duckdb DDL) -> ingest every file in the raw folder -> quality rules and staging
  -> merge into CORE.MEMBER_CURRENT and split by country (per batch, so each move is recorded)
  -> flatten and stage redemptions -> promote them to CORE -> print the run report.

What a file is comes from the source manifest (skypoints.config.MANIFEST), not from guessing:
  a file named in the manifest                  country extract (IND.csv, USA.csv, AUS.xlsx) or
                                                redemption feed (redemptions.json), read with the
                                                manifest's source system, country, date format and
                                                fixed file_ts / feed_date
  SKYPOINTS_MEMBERS_YYYYMMDD_HHMMSSTT.dat       member feed (the timestamp is in the name)
  SKYPOINTS_REDEMPTIONS_YYYYMMDD_HHMMSSTT.json  redemption feed (same)
  anything else                                 listed as skipped with the reason
                                                "no matching feed rule", never silently ignored
A file already in raw is reported as "already loaded" and not loaded again, so a re-run is safe.
A file that is rejected whole (bad name, or a header that does not match the layout) is "failed":
nothing of it reaches raw, its data rows are counted in "Rows in failed files", and the exit code
is 1. Exit code: 0 on success; 1 if a file failed or a batch is blocked (FAIL_BATCH); 2 for bad
arguments.

After the run the processed data is exported as CSV files, plus run_report.txt, into
<database folder>/export (--export-dir to change it, --no-export to skip it); see skypoints.export.
"""

from __future__ import annotations

import argparse
import re
from dataclasses import dataclass, field
from datetime import date, datetime
from pathlib import Path

import duckdb

from skypoints.config import FEED_COUNTRY_EXTRACT, MANIFEST, NO_RULE_REASON, SourceEntry
from skypoints.db import create_database
from skypoints.export import DQ_SUMMARY_SQL, export_csv
from skypoints.ingest.country_load import load_country_extract
from skypoints.ingest.flat_file import InvalidFileNameError, parse_feed_file_ts, parse_member_feed
from skypoints.ingest.loader import load_member_feed
from skypoints.ingest.redemptions import load_redemption_feed, promote_redemptions
from skypoints.quality.model import QualityConfig
from skypoints.quality.runner import config_from_ref, run_quality
from skypoints.transform.country_split import BatchBlockedError, split_by_country

DEFAULT_RAW_DIR = Path(__file__).resolve().parents[2] / "data" / "raw"
DEFAULT_DB = Path(__file__).resolve().parents[2] / "data" / "processed" / "skypoints.duckdb"

# Keys of RunReport.layers, in the order they are printed. Reconciliation:
#   RAW member rows = STAGING members + Quarantined member rows
#   RAW redemption transactions = STAGING redemptions + Quarantined redemption rows
# "Rows in failed files" counts data rows of files rejected whole; they never reach raw.
LAYER_KEYS = (
    "RAW member rows",
    "STAGING members",
    "Quarantined member rows",
    "Rows in failed files",
    "CORE members",
    "RAW redemption members",
    "RAW redemption transactions",
    "STAGING redemptions",
    "Quarantined redemption rows",
    "CORE redemption transactions",
)
_DATE_COLUMNS = ("enrollment_date", "last_flight_date", "date_of_birth")
_MEMBER_PREFIX, _REDEMPTION_PREFIX = "SKYPOINTS_MEMBERS_", "SKYPOINTS_REDEMPTIONS_"


@dataclass(frozen=True)
class FileOutcome:
    name: str
    kind: str  # 'members' | 'redemptions' | 'country extract' | 'unknown'
    status: str  # 'loaded' | 'already loaded' | 'skipped' | 'failed'
    detail: str = ""


@dataclass(frozen=True)
class QuarantinedRow:
    source_table: str
    file_name: str
    file_row_number: int
    rule_code: str
    reason_code: str


@dataclass(frozen=True)
class RunReport:
    as_of: date
    db_path: Path
    files: list[FileOutcome] = field(default_factory=list)
    layers: dict[str, int] = field(default_factory=dict)
    country_tables: dict[str, int] = field(default_factory=dict)
    dq_summary: dict[str, int] = field(
        default_factory=dict
    )  # rule code -> failures, DQ and RD rules
    quarantined: list[QuarantinedRow] = field(default_factory=list)
    moves: int = 0
    orphan_redemption_members: int = 0
    blocked_batches: list[str] = field(default_factory=list)

    @property
    def failed(self) -> bool:
        return bool(self.blocked_batches) or any(f.status == "failed" for f in self.files)


def _classify(path: Path, manifest: dict[str, SourceEntry]) -> tuple[str, SourceEntry | None]:
    """(kind, manifest entry). The manifest wins; then the feed name patterns; else 'unknown'."""
    entry = manifest.get(path.name)
    if entry is not None:
        return (
            "country extract" if entry.feed_type == FEED_COUNTRY_EXTRACT else "redemptions"
        ), entry
    if path.name.startswith(_MEMBER_PREFIX):
        return "members", None
    if path.name.startswith(_REDEMPTION_PREFIX):
        return "redemptions", None
    return "unknown", None


def _data_rows(path: Path) -> int:
    """Data rows of a rejected file (|D| lines, or CSV/XLSX rows below the header), for the report."""
    try:
        if path.suffix.lower() == ".xlsx":
            import openpyxl

            workbook = openpyxl.load_workbook(path, read_only=True)
            try:
                return max(workbook.worksheets[0].max_row - 1, 0)
            finally:
                workbook.close()
        lines = path.read_text(encoding="utf-8-sig").splitlines()
        if path.suffix.lower() == ".csv":
            return max(sum(1 for line in lines if line.strip()) - 1, 0)
        return sum(1 for line in lines if line.startswith("|D|"))
    except Exception:  # noqa: BLE001 - an unreadable file simply has no countable rows
        return 0


def _file_ts(path: Path, kind: str) -> datetime:
    feed, extension = ("MEMBERS", "dat") if kind == "members" else ("REDEMPTIONS", "json")
    return parse_feed_file_ts(path.name, feed, extension)


def _stage_and_split(
    con: duckdb.DuckDBPyConnection, name: str, kind: str, skipped: bool, as_of: date,
    config: QualityConfig, headers: dict[str, list[str]] | None, blocked: list[str],
) -> FileOutcome:  # fmt: skip
    """Quality rules and staging, then merge and country split, for the batch of one file."""
    run_quality(con, name, as_of, headers=headers, config=config)
    try:
        split_by_country(con, name)
    except BatchBlockedError as exc:
        blocked.append(name)
        return FileOutcome(name, kind, "failed", f"batch blocked: {exc}")
    return FileOutcome(name, kind, "already loaded" if skipped else "loaded")


def _ingest_members(
    con: duckdb.DuckDBPyConnection, path: Path, as_of: date, config: QualityConfig, blocked: list[str]
) -> tuple[FileOutcome, int]:  # fmt: skip
    """Load, validate, stage, merge and split one member file. Returns (outcome, failed rows)."""
    try:
        header = parse_member_feed(path)[0]
        loaded = load_member_feed(con, path, path.name)
    except (ValueError, InvalidFileNameError) as exc:  # HeaderMismatchError is a ValueError
        return FileOutcome(path.name, "members", "failed", str(exc)), _data_rows(path)
    outcome = _stage_and_split(
        con, path.name, "members", loaded.skipped, as_of, config, {path.name: header}, blocked
    )
    return outcome, 0


def _ingest_extract(
    con: duckdb.DuckDBPyConnection, path: Path, entry: SourceEntry, as_of: date, config: QualityConfig,
    blocked: list[str],
) -> tuple[FileOutcome, int]:  # fmt: skip
    """Load a country extract with its manifest entry, then validate, stage, merge and split."""
    try:
        loaded = load_country_extract(con, path, entry, path.name)
    except (ValueError, OSError) as exc:  # a header that does not match, or an unreadable file
        return FileOutcome(path.name, "country extract", "failed", str(exc)), _data_rows(path)
    outcome = _stage_and_split(
        con, path.name, "country extract", loaded.skipped, as_of, config, None, blocked
    )
    return outcome, 0


def _ingest_redemptions(
    con: duckdb.DuckDBPyConnection, path: Path, entry: SourceEntry | None
) -> FileOutcome:
    try:
        result = load_redemption_feed(
            con, path, path.name,
            file_ts=entry.file_ts if entry else None,
            default_feed_date=entry.feed_date if entry else None,
        )  # fmt: skip
    except (ValueError, InvalidFileNameError, duckdb.Error) as exc:
        return FileOutcome(path.name, "redemptions", "failed", str(exc))
    return FileOutcome(path.name, "redemptions", "already loaded" if result.skipped else "loaded")


def _scalar(con: duckdb.DuckDBPyConnection, sql: str) -> int:
    return int(con.execute(sql).fetchone()[0])


def _build_report(
    con: duckdb.DuckDBPyConnection, as_of: date, db_path: Path, files: list[FileOutcome],
    failed_rows: int, blocked: list[str],
) -> RunReport:  # fmt: skip
    layers = {
        "RAW member rows": _scalar(con, "SELECT count(*) FROM raw.member_feed_raw WHERE record_type = 'D'"),
        "STAGING members": _scalar(con, "SELECT count(*) FROM staging.member_stg"),
        "Quarantined member rows": _scalar(
            con, "SELECT count(*) FROM dq.quarantine WHERE source_table = 'RAW.MEMBER_FEED_RAW'"),
        "Rows in failed files": failed_rows,
        "CORE members": _scalar(con, "SELECT count(*) FROM core.member_current"),
        "RAW redemption members": _scalar(con, "SELECT count(*) FROM raw.redemption_feed_raw"),
        "RAW redemption transactions": _scalar(
            con, "SELECT coalesce(sum(json_array_length(json_extract(payload, '$.redemptions'))), 0) "
                 "FROM raw.redemption_feed_raw"),
        "STAGING redemptions": _scalar(con, "SELECT count(*) FROM staging.redemption_stg"),
        "Quarantined redemption rows": _scalar(
            con, "SELECT count(*) FROM dq.quarantine WHERE source_table = 'RAW.REDEMPTION_FEED_RAW'"),
        "CORE redemption transactions": _scalar(con, "SELECT count(*) FROM core.redemption_txn"),
    }  # fmt: skip
    tables = [
        name for (name,) in con.execute(
            "SELECT upper(table_name) FROM information_schema.tables WHERE lower(table_schema) = 'mart' "
            "AND upper(table_name) LIKE 'MEMBERS\\_%' ESCAPE '\\' ORDER BY 1"
        ).fetchall()
    ]  # fmt: skip
    country_tables = {t: _scalar(con, f"SELECT count(*) FROM mart.{t}") for t in tables}

    dq_summary = {rule: int(n) for rule, n in con.execute(DQ_SUMMARY_SQL).fetchall()}

    quarantined = [
        QuarantinedRow(*row)
        for row in con.execute(
            "SELECT source_table, file_name, file_row_number, rule_code, reason_code FROM dq.quarantine "
            "ORDER BY source_table, file_name, file_row_number"
        ).fetchall()
    ]
    return RunReport(
        as_of=as_of,
        db_path=db_path,
        files=files,
        layers=layers,
        country_tables=country_tables,
        dq_summary=dq_summary,
        quarantined=quarantined,
        moves=_scalar(con, "SELECT count(*) FROM core.member_country_history"),
        orphan_redemption_members=_scalar(
            con,
            "SELECT count(DISTINCT member_key) FROM core.redemption_txn "
            "WHERE member_key NOT IN (SELECT member_key FROM core.member_current)",
        ),
        blocked_batches=blocked,
    )


def run_pipeline(
    raw_dir: str | Path, db_path: str | Path, as_of: date,
    manifest: dict[str, SourceEntry] | None = None,
) -> RunReport:  # fmt: skip
    """Run every step and return the report. The database file and its folder are created.
    `manifest` defaults to skypoints.config.MANIFEST."""
    raw_dir, db_path = Path(raw_dir), Path(db_path)
    manifest = MANIFEST if manifest is None else manifest
    if not raw_dir.is_dir():
        raise FileNotFoundError(f"raw folder not found: {raw_dir}")
    db_path.parent.mkdir(parents=True, exist_ok=True)
    con = create_database(db_path)  # the DDL is idempotent
    try:
        # The manifest's date formats join the built-in ones for the quality rules.
        date_formats = {
            e.source_system: dict.fromkeys(_DATE_COLUMNS, e.date_format)
            for e in manifest.values() if e.feed_type == FEED_COUNTRY_EXTRACT
        }  # fmt: skip
        config = config_from_ref(con, date_formats)

        outcomes: dict[str, FileOutcome] = {}
        member_files: list[tuple[datetime, str, Path, SourceEntry | None]] = []
        redemption_files: list[tuple[datetime, str, Path, SourceEntry | None]] = []
        for path in sorted(
            p for p in raw_dir.iterdir() if p.is_file() and not p.name.startswith(".")
        ):
            kind, entry = _classify(path, manifest)
            if kind == "unknown":
                outcomes[path.name] = FileOutcome(path.name, kind, "skipped", NO_RULE_REASON)
                continue
            try:
                file_ts = entry.file_ts if entry else _file_ts(path, kind)
            except InvalidFileNameError as exc:
                outcomes[path.name] = FileOutcome(path.name, kind, "failed", str(exc))
                continue
            target = redemption_files if kind == "redemptions" else member_files
            target.append((file_ts, path.name, path, entry))

        blocked: list[str] = []
        failed_rows = sum(
            _data_rows(raw_dir / name) for name, o in outcomes.items() if o.status == "failed"
        )
        for _, _, path, entry in sorted(
            member_files, key=lambda f: (f[0], f[1])
        ):  # oldest file first
            if entry is None:
                outcome, rows = _ingest_members(con, path, as_of, config, blocked)
            else:
                outcome, rows = _ingest_extract(con, path, entry, as_of, config, blocked)
            outcomes[path.name] = outcome
            failed_rows += rows
        for _, _, path, entry in sorted(redemption_files, key=lambda f: (f[0], f[1])):
            outcomes[path.name] = _ingest_redemptions(con, path, entry)
        promote_redemptions(con)
        return _build_report(
            con, as_of, db_path, [outcomes[name] for name in sorted(outcomes)], failed_rows, blocked
        )
    finally:
        con.close()


def format_report(report: RunReport) -> str:
    """The readable report. Numbers print as '<label>: <integer>' so they can be read back."""
    lines = [
        "SkyPoints pipeline run report",
        f"as of {report.as_of.isoformat()}  |  database {report.db_path}",
        "",
        "== Files ==",
    ]
    lines += [
        f"  {f.name}  [{f.kind}]  {f.status}" + (f"  - {f.detail}" if f.detail else "")
        for f in report.files
    ] or ["  (no files)"]
    lines += ["", "== Rows per layer =="]
    lines += [
        f"  {label}: {report.layers[label]}" for label in LAYER_KEYS if label in report.layers
    ]
    lines += ["", "== Rows per country table =="]
    lines += [f"  {table}: {n}" for table, n in report.country_tables.items()]
    lines += ["", "== DQ summary by rule =="]
    lines += [f"  {rule}: {n}" for rule, n in report.dq_summary.items()] or ["  (no rule fired)"]
    lines += ["", "== Quarantined rows =="]
    lines += [
        f"  {q.source_table}  {q.file_name}  row {q.file_row_number}  {q.rule_code}  {q.reason_code}"
        for q in report.quarantined
    ] or ["  (none)"]
    lines += [
        "",
        "== Other ==",
        f"  Country moves recorded: {report.moves}",
        f"  Orphan redemption members: {report.orphan_redemption_members}",
    ]
    if report.blocked_batches:
        lines.append("  Blocked batches (FAIL_BATCH): " + ", ".join(report.blocked_batches))
    return "\n".join(lines) + "\n"


def _iso_date(text: str) -> date:
    if not re.fullmatch(r"\d{4}-\d{2}-\d{2}", text):
        raise argparse.ArgumentTypeError(f"{text!r} is not a date; use YYYY-MM-DD")
    try:
        return date.fromisoformat(text)
    except ValueError as exc:
        raise argparse.ArgumentTypeError(f"{text!r} is not a valid date: {exc}") from exc


def main(argv: list[str] | None = None) -> int:
    """Parse --as-of (required, YYYY-MM-DD), --db and --raw-dir, run, print the report."""
    parser = argparse.ArgumentParser(
        prog="python -m skypoints.pipeline", description=__doc__.split("\n\n")[0]
    )
    parser.add_argument(
        "--as-of",
        required=True,
        type=_iso_date,
        help="the date Age and Stale_Member are computed for",
    )
    parser.add_argument(
        "--db", type=Path, default=DEFAULT_DB, help=f"DuckDB file (default {DEFAULT_DB})"
    )
    parser.add_argument(
        "--raw-dir",
        type=Path,
        default=DEFAULT_RAW_DIR,
        help=f"folder of feeds (default {DEFAULT_RAW_DIR})",
    )
    parser.add_argument(
        "--export-dir", type=Path, help="where the CSV files go (default: <database folder>/export)"
    )
    parser.add_argument("--no-export", action="store_true", help="do not write the CSV files")
    args = parser.parse_args(argv)
    if not args.raw_dir.is_dir():
        parser.error(f"raw folder not found: {args.raw_dir}")
    report = run_pipeline(args.raw_dir, args.db, args.as_of)
    text = format_report(report)
    print(text, end="")
    if not args.no_export:
        out_dir = args.export_dir or args.db.parent / "export"
        files = export_csv(args.db, out_dir)
        (out_dir / "run_report.txt").write_text(text, encoding="utf-8")
        print()
        print(f"Exported {len(files)} CSV files and run_report.txt to {out_dir}")
    return 1 if report.failed else 0


if __name__ == "__main__":
    raise SystemExit(main())
