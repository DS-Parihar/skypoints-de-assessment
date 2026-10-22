"""Unit tests for skypoints.ingest.country_extracts.

IND.csv, USA.csv and AUS.xlsx each have their own column names. The adapters map them to one
canonical layout, keep every value as the raw string, and add source_system plus a namespaced
member_key ("<source_system>:<member_id>") because IDs 1, 2 and 3 repeat across the files.
A column a source does not have is None (absent), as opposed to '' (present but blank).
"""

from pathlib import Path

import pytest

from skypoints.ingest.country_extracts import read_aus_xlsx, read_ind_csv, read_usa_csv

FIXTURES = Path(__file__).resolve().parents[1] / "fixtures" / "country"
IND_FILE = FIXTURES / "IND.csv"
USA_FILE = FIXTURES / "USA.csv"
AUS_FILE = FIXTURES / "AUS.xlsx"

CANONICAL_KEYS = {
    "source_system",
    "member_key",
    "member_id",
    "member_name",
    "tier_code",
    "date_of_birth",
    "enrollment_date",
    "last_flight_date",
    "individual_or_corporate",
}

ADAPTERS = [
    pytest.param(read_ind_csv, IND_FILE, "IND", id="IND"),
    pytest.param(read_usa_csv, USA_FILE, "USA", id="USA"),
    pytest.param(read_aus_xlsx, AUS_FILE, "AUS", id="AUS"),
]


# ---- common contract -------------------------------------------------------------------------


@pytest.mark.parametrize(("reader", "path", "system"), ADAPTERS)
def test_every_adapter_returns_the_same_canonical_keys(reader, path, system) -> None:
    records = reader(path)

    assert len(records) == 3
    for record in records:
        assert set(record) == CANONICAL_KEYS


@pytest.mark.parametrize(("reader", "path", "system"), ADAPTERS)
def test_source_system_is_added(reader, path, system) -> None:
    assert {r["source_system"] for r in reader(path)} == {system}


@pytest.mark.parametrize(("reader", "path", "system"), ADAPTERS)
def test_member_key_is_the_source_system_and_member_id(reader, path, system) -> None:
    for record in reader(path):
        assert record["member_key"] == f"{system}:{record['member_id']}"


@pytest.mark.parametrize(("reader", "path", "system"), ADAPTERS)
def test_every_present_value_is_a_string(reader, path, system) -> None:
    for record in reader(path):
        for key, value in record.items():
            assert value is None or isinstance(value, str), key


@pytest.mark.parametrize(("reader", "path", "system"), ADAPTERS)
def test_member_ids_are_plain_digit_strings(reader, path, system) -> None:
    assert [r["member_id"] for r in reader(path)] == ["1", "2", "3"]


def test_member_keys_do_not_collide_across_countries() -> None:
    keys = [r["member_key"] for reader, path, _ in (p.values for p in ADAPTERS) for r in reader(path)]

    assert len(keys) == 9
    assert len(set(keys)) == 9
    assert {"IND:1", "USA:1", "AUS:1"} <= set(keys)


def test_the_bare_member_ids_do_collide_which_is_why_the_key_is_namespaced() -> None:
    ids = [r["member_id"] for reader, path, _ in (p.values for p in ADAPTERS) for r in reader(path)]

    assert len(ids) == 9
    assert len(set(ids)) == 3


# ---- IND.csv ---------------------------------------------------------------------------------


def test_ind_columns_are_mapped_to_canonical_names() -> None:
    first = read_ind_csv(IND_FILE)[0]

    assert first == {
        "source_system": "IND",
        "member_key": "IND:1",
        "member_id": "1",
        "member_name": "Vikas",
        "tier_code": "SLV",
        "date_of_birth": "12/1/1998",
        "enrollment_date": "1/1/2022",
        "last_flight_date": "6/15/2022",
        "individual_or_corporate": "I",
    }


def test_ind_dates_are_not_reformatted() -> None:
    records = read_ind_csv(IND_FILE)

    assert [r["date_of_birth"] for r in records] == ["12/1/1998", "8/13/1982", "8/13/1952"]


# ---- USA.csv ---------------------------------------------------------------------------------


def test_usa_columns_are_mapped_to_canonical_names() -> None:
    first = read_usa_csv(USA_FILE)[0]

    assert first == {
        "source_system": "USA",
        "member_key": "USA:1",
        "member_id": "1",
        "member_name": "Sam",
        "tier_code": "PLT",
        "date_of_birth": None,  # USA has no DOB column
        "enrollment_date": "6152022",
        "last_flight_date": "8202022",
        "individual_or_corporate": None,  # only IND has this column
    }


def test_usa_unpadded_dates_stay_as_the_raw_digits() -> None:
    records = read_usa_csv(USA_FILE)

    assert [r["enrollment_date"] for r in records] == ["6152022", "1052022", "12282021"]
    assert [r["last_flight_date"] for r in records] == ["8202022", "1152022", "12302021"]


# ---- AUS.xlsx --------------------------------------------------------------------------------


def test_aus_columns_are_mapped_to_canonical_names() -> None:
    second = read_aus_xlsx(AUS_FILE)[1]

    assert second == {
        "source_system": "AUS",
        "member_key": "AUS:2",
        "member_id": "2",
        "member_name": "Jonnathan",
        "tier_code": "GLD",
        "date_of_birth": "1997-12-13",
        "enrollment_date": "2021-13-13",  # invalid on purpose: the adapter does not judge it
        "last_flight_date": "2022-01-05",
        "individual_or_corporate": None,
    }


def test_aus_text_null_is_kept_as_the_string_null() -> None:
    first = read_aus_xlsx(AUS_FILE)[0]

    assert first["date_of_birth"] == "NULL"


def test_aus_numeric_excel_ids_become_plain_strings_without_a_decimal_point() -> None:
    ids = [r["member_id"] for r in read_aus_xlsx(AUS_FILE)]

    assert ids == ["1", "2", "3"]
    assert "1.0" not in ids


# ---- bad input -------------------------------------------------------------------------------


@pytest.mark.parametrize(
    ("reader", "path"),
    [
        pytest.param(read_ind_csv, FIXTURES / "missing.csv", id="IND"),
        pytest.param(read_usa_csv, FIXTURES / "missing.csv", id="USA"),
        pytest.param(read_aus_xlsx, FIXTURES / "missing.xlsx", id="AUS"),
    ],
)
def test_missing_file_raises_file_not_found(reader, path) -> None:
    with pytest.raises(FileNotFoundError):
        reader(path)


def test_ind_file_with_an_unexpected_header_is_rejected(tmp_path: Path) -> None:
    path = tmp_path / "IND.csv"
    path.write_text("Foo,Bar\n1,2\n", encoding="utf-8")

    with pytest.raises(ValueError, match="ID"):
        read_ind_csv(path)
