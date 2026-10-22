"""Guards for data/raw: the client files stay byte-identical, and the files created from the PDF
keep exactly the brief's content. See data/raw/README.md."""

import hashlib
import json
import re
from pathlib import Path

import pytest

RAW = Path(__file__).resolve().parents[1] / "data" / "raw"
README = RAW / "README.md"
CLIENT_FILES = ["IND.csv", "USA.csv", "AUS.xlsx"]
MEMBER_FEED = RAW / "SKYPOINTS_MEMBERS_20240115_00000000.dat"


def readme_checksums() -> dict[str, str]:
    """{file name: sha256} from the first table of the README (the client files)."""
    rows = re.findall(
        r"^\|\s*`([^`]+)`\s*\|\s*`([0-9a-f]{64})`\s*\|\s*$",
        README.read_text(encoding="utf-8"),
        re.MULTILINE,
    )
    return dict(rows)


def sha256(path: Path) -> str:
    return hashlib.sha256(path.read_bytes()).hexdigest()


def test_the_readme_lists_exactly_the_three_client_files() -> None:
    assert sorted(readme_checksums()) == sorted(CLIENT_FILES)


@pytest.mark.parametrize("name", CLIENT_FILES)
def test_a_client_file_matches_the_checksum_in_the_readme(name: str) -> None:
    assert sha256(RAW / name) == readme_checksums()[name], (
        f"{name} changed. The client files must stay byte-identical (data/raw/README.md)."
    )


def test_line_endings_of_data_raw_are_not_converted_by_git() -> None:
    attributes = (RAW.parents[1] / ".gitattributes").read_text(encoding="utf-8")

    assert "data/raw/** -text" in attributes


def test_the_member_feed_has_exactly_the_briefs_lines() -> None:
    assert MEMBER_FEED.read_text(encoding="utf-8").splitlines() == [
        "|H|Member_Name|Member_Id|Enrollment_Date|Last_Flight_Date|Tier_Code|Agent_Name|State|Country|DOB|Is_Active",
        "|D|Elena|223457|20101012|20121013|GLD|Sam|CA|USA|03051985|A",
        "|D|Ravi|223458|20101012|20121013|SLV|Sam|TN|IND|03051985|A",
        "|D|Mateo|223459|20101012|20121013|GLD|Sam|NCR|PHIL|03051985|A",
        "|D|Nora|22345|20101012|20121013|PLT|Sam|ONT|CAN|03051985|A",
        "|D|Jacob|2256|20101012|20121013|SLV|Sam|VIC|AU|03051985|A",
    ]


def test_the_member_feed_ends_with_one_newline_and_has_no_blank_lines() -> None:
    text = MEMBER_FEED.read_bytes().decode("utf-8")

    assert text.endswith("\n") and not text.endswith("\n\n")
    assert "\r" not in text and "" not in text.splitlines()


def test_redemptions_json_is_only_the_briefs_example() -> None:
    data = json.loads((RAW / "redemptions.json").read_text(encoding="utf-8"))

    assert [m["member_id"] for m in data] == ["223457"]
    assert [t["txn_id"] for t in data[0]["redemptions"]] == ["RX10091", "RX10092"]


def test_data_raw_holds_only_the_documented_files() -> None:
    assert sorted(p.name for p in RAW.iterdir()) == sorted(
        [*CLIENT_FILES, "README.md", "redemptions.json", MEMBER_FEED.name]
    )
