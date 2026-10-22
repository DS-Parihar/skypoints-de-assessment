# SkyPoints Data Engineering Assessment

[![CI](https://github.com/DS-Parihar/skypoints-de-assessment/actions/workflows/ci.yml/badge.svg)](https://github.com/DS-Parihar/skypoints-de-assessment/actions/workflows/ci.yml)

Ingestion and transformation pipeline for the SkyPoints loyalty program: a daily pipe-delimited
member feed and a JSON redemption feed become per-country member tables, a flattened redemption
table with a per-member summary, a quarantine table with reason codes, and a data-quality report.

- **Production target:** Snowflake SQL in `sql/snowflake/`. It is reviewed, not run, in this repository.
- **Runnable target:** the same layers and rules on DuckDB (`sql/duckdb/`, `src/skypoints/`). One command
  runs the whole pipeline on a laptop, and the tests run against it.

## Run it

Python 3.12. From the repository root:

```bash
# Git Bash
python -m venv .venv && source .venv/Scripts/activate
pip install -r requirements.txt
export PYTHONPATH=src
python -m skypoints.pipeline --as-of 2024-01-15
```

```bat
:: Windows cmd
python -m venv .venv && .venv\Scripts\activate
pip install -r requirements.txt
set PYTHONPATH=src
python -m skypoints.pipeline --as-of 2024-01-15
```

`PYTHONPATH=src` is needed because the package lives in `src/` (pytest sets it for the tests;
VS Code reads it from the `.env` file; `pip install -e .` should also do it). `--as-of` is required:
no date logic ever uses today's date.

The command reads `data/raw`, writes `data/processed/skypoints.duckdb`, prints the run report and
writes readable CSV files plus `run_report.txt` to `data/processed/export/`. Options: `--db`,
`--raw-dir`, `--export-dir`, `--no-export`. Running it again changes nothing (files already loaded are
skipped). Exit code 0 on success, 1 if a file failed or a batch is blocked, 2 for bad arguments.

To look at the result:

```bash
python scripts/show.py                 # the 12 demo queries as text tables
python scripts/show.py --only 3,11     # just some of them
```

`scripts/demo_queries.sql` holds the same queries for any DuckDB client.

Tests and checks:

```bash
pytest -q --cov=src --cov-fail-under=80
ruff check . && ruff format --check .
```

## Deliverables

| Deliverable | Where |
|---|---|
| 1. DDL | `sql/snowflake/00_setup.sql` to `03_targets.sql`; DuckDB twins in `sql/duckdb/` |
| 2. Staging, Age, Stale_Member | `sql/snowflake/02_staging.sql`, `02_staging_load.sql`; `sql/duckdb/load_staging.sql`; `src/skypoints/transform/derive.py` |
| 3. Country split, latest record wins | `sql/snowflake/04_country_split.sql`; `sql/duckdb/transform/30-34_*.sql`; `src/skypoints/transform/country_split.py` |
| 4. JSON flattening and join back | `sql/snowflake/06_redemptions.sql`; `sql/duckdb/transform/40-47_*.sql`, `06_redemption_summary.sql`; `src/skypoints/ingest/redemptions.py`; [architecture, redemptions](docs/architecture.md#redemptions) |
| 5. Validations | `sql/snowflake/05_validations.sql`; `sql/duckdb/05_validations.sql`; `src/skypoints/quality/` |
| 6. Demo | `python -m skypoints.pipeline --as-of 2024-01-15`; `scripts/show.py`; `scripts/demo_queries.sql` |
| Design | [architecture](docs/architecture.md), [ADRs](docs/adr/), [scaling in Snowflake](docs/scaling-in-snowflake.md) |
| Data issues found | [data quality findings](docs/data-quality-findings.md), [initial understanding](notes/initial-understanding.md) |
| AI use | [AI usage log](docs/ai-usage-log.md) |

## Design in short

```
files in data/raw -> RAW (all text) -> quality rules -> STAGING (typed) -> CORE.MEMBER_CURRENT
                                          |  \                                 |
                                   quarantine  findings               MART.MEMBERS_<ISO3> (one per country)
redemptions -> RAW (VARIANT) -> flatten -> rules -> STAGING -> CORE.REDEMPTION_TXN -> MART.MEMBER_REDEMPTION_SUMMARY
```

Raw keeps every value as text, so a load never fails on content and `03051985` is never turned into
`3051985`. Typing happens only in staging. Invalid rows are never dropped: they go to
`DQ.QUARANTINE` with every rule code that rejected them and their original values. Rules are SQL
views, so the same logic runs on DuckDB and ports to Snowflake. See
[architecture](docs/architecture.md) for the diagram and the decisions.

## Assumptions

Where the brief was ambiguous I decided, and wrote the decision down (ADRs in `docs/adr/`).

- **Key:** `member_key = source_system:member_id`. Names repeat, and IDs 1, 2, 3 repeat across the IND, USA
  and AUS files. Redemptions join to the member feed ids. Linking the extracts to master members is out
  of scope.
- **Latest record:** the newest file timestamp, then the row number; Last_Flight_Date is a business event,
  not a record version.
- **Dates:** DOB is month-first (MMDDYYYY). Unreadable or ambiguous dates (`2021-13-13`, `1052022`) become
  NULL with a finding, never a guess.
- **Stale_Member:** no flight in the 90 days before `as_of`; unknown (NULL) when there is no flight date.
- **File names:** `SKYPOINTS_MEMBERS_YYYYMMDD_HHMMSSTT.dat` (TT = hundredths of a second) is assumed; the
  client extracts have no timestamp, so the source manifest in `src/skypoints/config.py` fixes one. Its
  `file_ts` and `feed_date` values are placeholders to confirm.
- **Header:** the sample header has no `Post_Code`, which the spec lists. Columns are mapped by name; a
  missing optional column is a warning, a missing mandatory one fails the file.
- `data/raw/README.md` says which files are the client's (checksummed, never edited) and which I created
  from the PDF.

## Data quality findings

The sample files have: Member Name as the spec key; a 10-field header against an 11-field spec; a DOB
that lost its leading zero in staging; Agent_Name present in the source and blank in 4 of 5 staging
rows; the `County` typo; mixed country codes (`PHIL`, `AU`); three extracts with three layouts and
colliding ids; USA dates that can mean two days; an AUS month 13 and the text `NULL`. Each one, with its
handling, is in [docs/data-quality-findings.md](docs/data-quality-findings.md). On `data/raw` the run
reports 14 member rows, 12 staged, 2 quarantined (`AUS.xlsx` row 3, `USA.csv` row 3) and 12 warnings.

## Trade-offs and what next

- Snowflake SQL is not executed here; the DuckDB twin is. The two need to be kept in step, and a first run
  in a real account would be the next step.
- One table per country is delivered as asked; one clustered table with secure views is cheaper at scale
  (ADR 0007).
- The IND "Individual or Corporate" column is not stored. The country extracts are loaded and staged but
  not linked to master members.
- Next: confirm the placeholders above with the data owner, a trailer record with a row count on the member
  feed, line-delimited JSON for redemptions (ADR 0008), and SCD Type 2 if the country at transaction time
  is needed.
