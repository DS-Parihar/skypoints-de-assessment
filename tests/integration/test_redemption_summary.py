"""Integration tests: promote_redemptions (STAGING.REDEMPTION_STG -> CORE.REDEMPTION_TXN) and the
view MART.MEMBER_REDEMPTION_SUMMARY (CORE.REDEMPTION_TXN LEFT JOIN CORE.MEMBER_CURRENT).

The view starts from the transactions, so a redemption whose member is not in the member feed is
visible (MEMBER_MATCHED = FALSE). It joins on member_key ('SKYPOINTS:' || member_id, ADR 0002),
never on the bare member_id, which repeats across source systems.
"""

from datetime import date
from pathlib import Path

from skypoints.ingest.redemptions import PromoteResult, load_redemption_feed, promote_redemptions

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "redemptions"
F0 = "SKYPOINTS_REDEMPTIONS_20240114_09000000.json"
F1 = "SKYPOINTS_REDEMPTIONS_20240115_09000000.json"


def feed(scenario: str, name: str = F1) -> Path:
    return FIXTURES / scenario / name


def add_member(
    con, member_id: str, country_code: str | None, source: str = "SKYPOINTS", name: str = "Elena"
) -> None:
    """A row in CORE.MEMBER_CURRENT (only the NOT NULL columns and the ones the view reads)."""
    con.execute(
        "INSERT INTO core.member_current (member_name, member_id, enrollment_date, source_system, "
        "member_key, country_code, as_of_date, stale_member, file_name, file_row_number, file_ts, "
        "batch_id) VALUES (?, ?, DATE '2010-01-01', ?, ?, ?, DATE '2024-01-15', FALSE, 'f.dat', 1, "
        "TIMESTAMP '2024-01-15 00:00:00', 'b')",
        [name, member_id, source, f"{source}:{member_id}", country_code],
    )


def add_txn(con, txn_id: str, member_id: str, status: str, miles: int, txn_date: str) -> None:
    """A row straight into CORE.REDEMPTION_TXN."""
    con.execute(
        "INSERT INTO core.redemption_txn (txn_id, member_id, member_key, feed_date, txn_date, "
        "partner, miles_redeemed, status, file_name, file_row_number, txn_seq, file_ts, batch_id) "
        "VALUES (?, ?, ?, DATE '2024-01-15', CAST(? AS DATE), 'P', ?, ?, 'f.json', 1, 1, "
        "TIMESTAMP '2024-01-15 00:00:00', 'b')",
        [txn_id, member_id, f"SKYPOINTS:{member_id}", txn_date, miles, status],
    )


def summary(con) -> list[tuple]:
    return con.execute(
        "SELECT member_key, member_matched, member_name, current_country_code, "
        "total_completed_miles, pending_miles, txn_count, last_redemption_date "
        "FROM mart.member_redemption_summary ORDER BY member_key"
    ).fetchall()


def core_txns(con) -> list[tuple]:
    return con.execute(
        "SELECT txn_id, status, miles_redeemed, member_key FROM core.redemption_txn ORDER BY txn_id"
    ).fetchall()


# ---- promote_redemptions ------------------------------------------------------------------------


def test_promote_copies_the_staged_redemptions_to_core(con) -> None:
    load_redemption_feed(con, feed("multi"), "b1")

    result = promote_redemptions(con)

    assert isinstance(result, PromoteResult)
    assert (result.inserted, result.updated) == (3, 0)
    assert core_txns(con) == [
        ("RX10091", "COMPLETED", 12000, "SKYPOINTS:223457"),
        ("RX10092", "PENDING", 5000, "SKYPOINTS:223457"),
        ("RX10093", "COMPLETED", 8000, "SKYPOINTS:223458"),
    ]


def test_promoting_twice_changes_nothing(con) -> None:
    load_redemption_feed(con, feed("multi"), "b1")
    promote_redemptions(con)
    before = con.execute(
        "SELECT * EXCLUDE (updated_at) FROM core.redemption_txn ORDER BY txn_id"
    ).fetchall()

    second = promote_redemptions(con)

    assert (second.inserted, second.updated) == (0, 0)
    assert (
        con.execute(
            "SELECT * EXCLUDE (updated_at) FROM core.redemption_txn ORDER BY txn_id"
        ).fetchall()
        == before
    )


def test_a_newer_version_of_a_transaction_replaces_the_core_row(con) -> None:
    load_redemption_feed(con, feed("duplicate_across", F0), "b0")
    promote_redemptions(con, "b0")
    assert core_txns(con) == [("RX10091", "PENDING", 12000, "SKYPOINTS:223457")]
    load_redemption_feed(con, feed("duplicate_across", F1), "b1")

    result = promote_redemptions(con, "b1")

    assert (result.inserted, result.updated) == (0, 1)
    assert core_txns(con) == [("RX10091", "COMPLETED", 12000, "SKYPOINTS:223457")]


def test_promote_can_be_limited_to_one_batch(con, tmp_path: Path) -> None:
    load_redemption_feed(con, feed("brief"), "b1")
    second = tmp_path / "SKYPOINTS_REDEMPTIONS_20240116_09000000.json"
    second.write_text(
        '[{"member_id": "223458", "feed_date": "20240116", "redemptions": [{"txn_id": "RX20001", '
        '"txn_date": "20240115", "partner": "P", "miles_redeemed": 10, "status": "COMPLETED"}]}]',
        encoding="utf-8",
    )
    load_redemption_feed(con, second, "b2")

    result = promote_redemptions(con, "b2")

    assert result.inserted == 1
    assert [row[0] for row in core_txns(con)] == ["RX20001"]


def test_a_rejected_transaction_is_never_promoted(con) -> None:
    load_redemption_feed(con, feed("negative_miles"), "b1")

    promote_redemptions(con)

    assert [row[0] for row in core_txns(con)] == ["RX10092"]


# ---- MART.MEMBER_REDEMPTION_SUMMARY -----------------------------------------------------------


def test_the_summary_totals_completed_and_pending_miles_with_the_last_date_and_country(con) -> None:
    add_member(con, "223457", "IND")
    load_redemption_feed(con, feed("brief"), "b1")
    promote_redemptions(con)

    assert summary(con) == [
        ("SKYPOINTS:223457", True, "Elena", "IND", 12000, 5000, 2, date(2024, 1, 13)),
    ]


def test_an_orphan_redemption_is_visible_in_the_summary(con) -> None:
    add_member(con, "223457", "IND")
    load_redemption_feed(con, feed("multi"), "b1")  # 223458 is not a member
    promote_redemptions(con)

    rows = {row[0]: row for row in summary(con)}

    assert rows["SKYPOINTS:223458"] == (
        "SKYPOINTS:223458",
        False,
        None,
        None,
        8000,
        0,
        1,
        date(2024, 1, 14),
    )
    assert rows["SKYPOINTS:223457"][1] is True


def test_the_summary_shows_the_members_current_country_after_a_move(con) -> None:
    add_member(con, "223457", "IND")
    load_redemption_feed(con, feed("brief"), "b1")
    promote_redemptions(con)
    assert summary(con)[0][3] == "IND"

    con.execute(
        "UPDATE core.member_current SET country_code = 'USA' WHERE member_key = 'SKYPOINTS:223457'"
    )

    assert summary(con)[0][3] == "USA"


def test_an_unassigned_member_has_no_current_country_in_the_summary(con) -> None:
    add_member(con, "223457", None)
    add_member(con, "223458", "BRA")  # a code that is not in REF.COUNTRY_CODE_MAP
    add_txn(con, "T1", "223457", "COMPLETED", 100, "2024-01-10")
    add_txn(con, "T2", "223458", "COMPLETED", 100, "2024-01-10")

    rows = {row[0]: row for row in summary(con)}

    assert rows["SKYPOINTS:223457"][1:4] == (True, "Elena", None)
    assert rows["SKYPOINTS:223458"][1:4] == (True, "Elena", None)


def test_a_member_with_only_pending_miles_has_zero_completed_miles(con) -> None:
    add_member(con, "223457", "IND")
    add_txn(con, "T1", "223457", "PENDING", 700, "2024-01-10")

    assert summary(con)[0][4:7] == (0, 700, 1)


def test_a_member_with_only_completed_miles_has_zero_pending_miles(con) -> None:
    add_member(con, "223457", "IND")
    add_txn(con, "T1", "223457", "COMPLETED", 300, "2024-01-10")
    add_txn(con, "T2", "223457", "COMPLETED", 400, "2024-01-12")

    assert summary(con)[0][4:8] == (700, 0, 2, date(2024, 1, 12))


def test_the_last_redemption_date_is_the_latest_txn_date_of_any_status(con) -> None:
    add_member(con, "223457", "IND")
    add_txn(con, "T1", "223457", "COMPLETED", 300, "2024-01-10")
    add_txn(con, "T2", "223457", "PENDING", 400, "2024-01-14")

    assert summary(con)[0][7] == date(2024, 1, 14)


def test_a_member_without_redemptions_is_not_in_the_summary(con) -> None:
    add_member(con, "223457", "IND")
    add_member(con, "223999", "USA")
    add_txn(con, "T1", "223457", "COMPLETED", 300, "2024-01-10")

    assert [row[0] for row in summary(con)] == ["SKYPOINTS:223457"]


def test_the_summary_joins_on_member_key_not_on_the_bare_member_id(con) -> None:
    add_member(
        con, "223457", "IND", source="IND", name="Someone else"
    )  # IND:223457, same member_id
    add_txn(con, "T1", "223457", "COMPLETED", 300, "2024-01-10")  # SKYPOINTS:223457

    rows = summary(con)

    assert rows == [("SKYPOINTS:223457", False, None, None, 300, 0, 1, date(2024, 1, 10))]


def test_the_summary_counts_each_transaction_once(con) -> None:
    add_member(con, "223457", "IND")
    add_member(con, "223457", "USA", source="USA")  # another source with the same id: no fan-out
    add_txn(con, "T1", "223457", "COMPLETED", 300, "2024-01-10")

    assert summary(con)[0][4:7] == (300, 0, 1)
