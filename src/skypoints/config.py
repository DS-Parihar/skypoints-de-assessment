"""Source manifest: how the pipeline reads the files in data/raw that are NOT named like a feed.

The pipeline reads this instead of guessing from file names or file contents. A dict in code
(no YAML dependency); keys are file names. Files named SKYPOINTS_MEMBERS_*.dat or
SKYPOINTS_REDEMPTIONS_*.json need no entry: their timestamp is in the name. A file that is in
neither the manifest nor a feed pattern is listed in the run report with the reason
"no matching feed rule" and is not loaded.

PROPOSED VALUES TO CONFIRM: feed_type, source_system, country_code and date_format follow from
the files and from the earlier decisions (IND is month-first, USA has unpadded MDDYYYY dates, AUS
is ISO). The fixed file_ts and feed_date are NOT in the client files, so they are placeholders
set to the as-of date of the brief (2024-01-15); replace them with the real delivery times.
"""

from __future__ import annotations

from dataclasses import dataclass
from datetime import datetime

FEED_COUNTRY_EXTRACT = "country_extract"
FEED_REDEMPTIONS = "redemptions"
NO_RULE_REASON = "no matching feed rule"


@dataclass(frozen=True)
class SourceEntry:
    feed_type: str  # FEED_COUNTRY_EXTRACT or FEED_REDEMPTIONS
    source_system: str  # prefix of member_key: 'IND' -> 'IND:1'
    country_code: str | None  # the country of every member in a country extract
    date_format: str  # format of the date columns (a name known to skypoints.transform.derive)
    file_ts: datetime  # fixed file timestamp: the extract has none in its name; ranks versions
    feed_date: str | None = (
        None  # fixed feed date (YYYYMMDD), used when the feed does not carry one
    )


_DELIVERED = datetime(2024, 1, 15, 0, 0, 0)  # placeholder, see the module docstring

MANIFEST: dict[str, SourceEntry] = {
    "IND.csv": SourceEntry(FEED_COUNTRY_EXTRACT, "IND", "IND", "M/D/YYYY", _DELIVERED),
    "USA.csv": SourceEntry(FEED_COUNTRY_EXTRACT, "USA", "USA", "MDYYYY", _DELIVERED),
    "AUS.xlsx": SourceEntry(FEED_COUNTRY_EXTRACT, "AUS", "AUS", "YYYY-MM-DD", _DELIVERED),
    "redemptions.json": SourceEntry(
        FEED_REDEMPTIONS, "SKYPOINTS", None, "YYYYMMDD", _DELIVERED, feed_date="20240115"
    ),
}
