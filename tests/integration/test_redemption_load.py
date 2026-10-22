"""Integration tests: skypoints.ingest.redemptions.load_redemption_feed (DuckDB).

raw (the member object as JSON) -> flatten -> typed STAGING.REDEMPTION_STG, with
  RD001 WARN   empty redemptions array          -> DQ.ROW_FINDING (and the member is in raw)
  RD002 REJECT duplicate txn_id, older version  -> DQ.QUARANTINE, reason_code DUPLICATE_TXN_OLDER
  RD003 REJECT negative miles_redeemed          -> DQ.QUARANTINE
  RD004 REJECT status not COMPLETED / PENDING   -> DQ.QUARANTINE
  RD005 WARN   txn_date after feed_date         -> DQ.ROW_FINDING, the row is still staged
Quarantine rows for redemptions have source_table 'RAW.REDEMPTION_FEED_RAW'; file_row_number is the
position of the member object, and the raw payload names the transaction.
"""

import json
from datetime import date, datetime
from pathlib import Path

import pytest

from skypoints.ingest.redemptions import (
    RedemptionConfig,
    RedemptionLoadResult,
    load_redemption_feed,
)

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "redemptions"
F0 = "SKYPOINTS_REDEMPTIONS_20240114_09000000.json"
F1 = "SKYPOINTS_REDEMPTIONS_20240115_09000000.json"
SOURCE = "RAW.REDEMPTION_FEED_RAW"


def feed(scenario: str, name: str = F1) -> Path:
    return FIXTURES / scenario / name


def load(
    con, scenario: str, name: str = F1, batch_id: str = "b1", **kwargs
) -> RedemptionLoadResult:
    return load_redemption_feed(con, feed(scenario, name), batch_id, **kwargs)


def staged(con) -> list[tuple]:
    return con.execute(
        "SELECT txn_id, status, miles_redeemed FROM staging.redemption_stg ORDER BY txn_id"
    ).fetchall()


def quarantined(con) -> list[tuple]:
    return con.execute(
        "SELECT rule_code, severity, reason_code, file_name, file_row_number FROM dq.quarantine "
        f"WHERE source_table = '{SOURCE}' ORDER BY rule_code, file_name"
    ).fetchall()


def findings(con) -> list[tuple]:
    return con.execute(
        "SELECT rule_code, severity, member_key, column_name, value FROM dq.row_finding ORDER BY rule_code"
    ).fetchall()


# ---- the brief's sample ------------------------------------------------------------------------


def test_the_brief_sample_stages_exactly_two_rows(con) -> None:
    result = load(con, "brief")

    assert staged(con) == [("RX10091", "COMPLETED", 12000), ("RX10092", "PENDING", 5000)]
    assert (result.members_read, result.rows_flattened, result.rows_staged) == (1, 2, 2)
    assert result.rows_quarantined == 0


def test_the_load_returns_a_result_object(con) -> None:
    result = load(con, "brief")

    assert isinstance(result, RedemptionLoadResult)
    assert result.file_name == F1
    assert result.skipped is False
    assert result.empty_members == ()


def test_staged_rows_are_typed(con) -> None:
    load(con, "brief")

    row = con.execute(
        "SELECT feed_date, txn_date, miles_redeemed, partner, member_key "
        "FROM staging.redemption_stg WHERE txn_id = 'RX10091'"
    ).fetchone()

    assert row == (date(2024, 1, 15), date(2024, 1, 10), 12000, "AeroLink", "SKYPOINTS:223457")


def test_staged_rows_carry_the_file_metadata(con) -> None:
    load(con, "multi", batch_id="batch-7")

    rows = con.execute(
        "SELECT txn_id, file_name, file_row_number, txn_seq, file_ts, batch_id "
        "FROM staging.redemption_stg ORDER BY txn_id"
    ).fetchall()

    ts = datetime(2024, 1, 15, 9, 0, 0)
    assert rows == [
        ("RX10091", F1, 1, 1, ts, "batch-7"),
        ("RX10092", F1, 1, 2, ts, "batch-7"),
        ("RX10093", F1, 2, 1, ts, "batch-7"),
    ]


def test_the_member_object_is_kept_whole_in_raw(con) -> None:
    load(con, "brief", batch_id="b1")

    rows = con.execute(
        "SELECT payload::VARCHAR, file_name, file_row_number, batch_id FROM raw.redemption_feed_raw"
    ).fetchall()

    assert len(rows) == 1
    payload, file_name, file_row_number, batch_id = rows[0]
    assert (file_name, file_row_number, batch_id) == (F1, 1, "b1")
    assert [t["txn_id"] for t in json.loads(payload)["redemptions"]] == ["RX10091", "RX10092"]


# ---- empty redemptions array: 0 rows, but recorded ----------------------------------------------


def test_an_empty_redemptions_array_stages_nothing_for_that_member(con) -> None:
    result = load(con, "empty_array")

    assert staged(con) == [("RX10093", "COMPLETED", 8000)]
    assert (result.members_read, result.rows_flattened, result.rows_staged) == (2, 1, 1)


def test_an_empty_redemptions_array_is_recorded_in_raw_in_the_finding_and_in_the_result(
    con,
) -> None:
    result = load(con, "empty_array")

    raw_members = con.execute(
        "SELECT json_extract_string(payload, '$.member_id') FROM raw.redemption_feed_raw "
        "ORDER BY file_row_number"
    ).fetchall()
    assert raw_members == [("223457",), ("223458",)]  # the member with no transactions is in raw
    assert findings(con) == [("RD001", "WARN", "SKYPOINTS:223457", None, None)]
    assert result.empty_members == ("223457",)
    assert quarantined(con) == []  # recorded, not rejected


# ---- duplicate txn_id ---------------------------------------------------------------------------


def test_a_duplicate_txn_id_in_one_file_keeps_the_last_occurrence(con) -> None:
    result = load(con, "duplicate_same_file")

    assert staged(con) == [("RX10091", "COMPLETED", 5000), ("RX10092", "PENDING", 5000)]
    assert (result.rows_flattened, result.rows_staged) == (3, 2)


def test_the_dropped_duplicate_is_recorded_not_silently_lost(con) -> None:
    load(con, "duplicate_same_file")

    assert quarantined(con) == [("RD002", "REJECT", "DUPLICATE_TXN_OLDER", F1, 1)]
    payload = con.execute("SELECT raw_payload::VARCHAR FROM dq.quarantine").fetchone()[0]
    assert "RX10091" in payload and "PENDING" in payload  # the dropped version, by status


@pytest.mark.parametrize(
    "order", [(F0, F1), (F1, F0)], ids=["older-file-first", "newer-file-first"]
)
def test_a_txn_id_re_sent_in_a_later_feed_ends_with_the_newest_version_in_either_load_order(
    con, order
) -> None:
    for i, name in enumerate(order):
        load(con, "duplicate_across", name, batch_id=f"b{i}")

    assert staged(con) == [("RX10091", "COMPLETED", 12000)]  # the 20240115 feed wins
    assert [(rule, file_name) for rule, _, _, file_name, _ in quarantined(con)] == [("RD002", F0)]


def test_a_duplicate_does_not_create_two_staged_rows_for_the_same_txn_id(con) -> None:
    load(con, "duplicate_same_file")

    assert con.execute(
        "SELECT count(*) - count(DISTINCT txn_id) FROM staging.redemption_stg"
    ).fetchone() == (0,)


# ---- negative miles and unknown status are quarantined ---------------------------------------------


def test_a_negative_miles_transaction_is_quarantined_and_not_staged(con) -> None:
    result = load(con, "negative_miles")

    assert staged(con) == [("RX10092", "COMPLETED", 5000)]
    assert quarantined(con) == [("RD003", "REJECT", "NEGATIVE_MILES", F1, 1)]
    assert (result.rows_flattened, result.rows_staged, result.rows_quarantined) == (2, 1, 1)


def test_the_quarantined_negative_transaction_keeps_its_original_values(con) -> None:
    load(con, "negative_miles")

    payload = json.loads(
        con.execute("SELECT raw_payload::VARCHAR FROM dq.quarantine").fetchone()[0]
    )
    text = json.dumps(payload)
    assert "RX10091" in text and "-500" in text and "223457" in text


def test_an_unknown_status_transaction_is_quarantined_and_not_staged(con) -> None:
    result = load(con, "unknown_status")

    assert staged(con) == [("RX10092", "COMPLETED", 12000)]
    assert quarantined(con) == [("RD004", "REJECT", "UNKNOWN_STATUS", F1, 1)]
    assert result.rows_quarantined == 1


def test_the_allowed_statuses_come_from_the_config(con) -> None:
    load(con, "unknown_status", config=RedemptionConfig(statuses=frozenset({"REFUNDED"})))

    assert staged(con) == [("RX10091", "REFUNDED", 12000)]  # COMPLETED is now the unknown one
    assert [r[0] for r in quarantined(con)] == ["RD004"]


def test_a_transaction_breaking_two_reject_rules_is_quarantined_once_with_both_codes(con) -> None:
    result = load(con, "both_bad")

    rows = con.execute(
        "SELECT rule_code, reason_detail FROM dq.quarantine WHERE source_table = ?", [SOURCE]
    ).fetchall()
    assert len(rows) == 1
    assert set(rows[0][0].split(",")) == {"RD003", "RD004"}
    assert "miles_redeemed" in rows[0][1] and "status" in rows[0][1]
    assert staged(con) == [("RX10092", "COMPLETED", 12000)]
    assert result.rows_quarantined == 1


# ---- txn_date after feed_date is flagged ---------------------------------------------------------


def test_a_txn_date_after_the_feed_date_is_flagged_and_the_row_is_still_staged(con) -> None:
    load(con, "txn_after_feed")

    assert staged(con) == [("RX10091", "COMPLETED", 12000), ("RX10092", "COMPLETED", 12000)]
    assert findings(con) == [("RD005", "WARN", "SKYPOINTS:223457", "txn_date", "20240116")]
    assert quarantined(con) == []


def test_the_staged_row_carries_the_flag(con) -> None:
    load(con, "txn_after_feed")

    assert con.execute(
        "SELECT txn_id, dq_txn_after_feed FROM staging.redemption_stg ORDER BY txn_id"
    ).fetchall() == [("RX10091", True), ("RX10092", False)]


def test_a_txn_date_equal_to_the_feed_date_is_not_flagged(con, tmp_path: Path) -> None:
    content = [
        {
            "member_id": "223457",
            "feed_date": "20240115",
            "redemptions": [
                {
                    "txn_id": "RX1",
                    "txn_date": "20240115",
                    "partner": "P",
                    "miles_redeemed": 1,
                    "status": "COMPLETED",
                }
            ],
        }
    ]
    path = tmp_path / F1
    path.write_text(json.dumps(content), encoding="utf-8")

    load_redemption_feed(con, path, "b1")

    assert findings(con) == []


# ---- member_id stays text ------------------------------------------------------------------------


def test_member_id_keeps_its_leading_zeros_in_raw_staging_and_the_key(con) -> None:
    load(con, "leading_zeros")

    assert con.execute("SELECT member_id, member_key FROM staging.redemption_stg").fetchall() == [
        ("007890", "SKYPOINTS:007890")
    ]
    assert con.execute(
        "SELECT json_extract_string(payload, '$.member_id') FROM raw.redemption_feed_raw"
    ).fetchall() == [("007890",)]


# ---- re-running ----------------------------------------------------------------------------------


def test_loading_the_same_file_twice_is_skipped_and_changes_nothing(con) -> None:
    load(con, "brief")
    before = (
        staged(con),
        con.execute("SELECT count(*) FROM raw.redemption_feed_raw").fetchone(),
        quarantined(con),
        findings(con),
    )

    second = load(con, "brief", batch_id="b2")

    assert second.skipped is True
    assert (second.rows_flattened, second.rows_staged) == (0, 0)
    assert (
        staged(con),
        con.execute("SELECT count(*) FROM raw.redemption_feed_raw").fetchone(),
        quarantined(con),
        findings(con),
    ) == before
