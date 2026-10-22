# Project: SkyPoints member & redemption pipeline (Incubyte DE assessment)

## Context
Airline loyalty program. Daily pipe-delimited member feed + JSON redemption feed.
Split members into per-country target tables, "latest record wins" when a member moves country.
Production target: Snowflake (sql/snowflake). Local runnable target: DuckDB (sql/duckdb, src/).

## Rules for you (Claude)
- Do NOT run git commit or git push. I review diffs and commit myself.
- Follow TDD: when I ask for a feature, write tests first and stop so I can see them fail.
- Python 3, type hints, small pure functions, no hidden global state.
- Never use today's date implicitly: all date logic takes an explicit `as_of` date.
- Raw layer keeps every field as text. Parsing/typing happens in staging only.
- Never silently drop records: invalid rows go to a quarantine table with a reason code.
- I'm on Windows using Git Bash; venv is at .venv/Scripts.

## Commands
- Tests: `pytest -q --cov=src`
- Lint: `ruff check . && ruff format --check .`
- Run pipeline: `python -m skypoints.pipeline --as-of 2024-01-15`
