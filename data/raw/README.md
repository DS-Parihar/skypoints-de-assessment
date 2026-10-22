# data/raw

Input files for the pipeline. **Do not edit, rename or reformat any file here.** The pipeline
reads them as they are, and a test fails if a client file changes.

## Provided by Incubyte (byte-identical)

| File | SHA-256 |
|---|---|
| `IND.csv` | `0b68c29592dfc89d54edcdaf7293252f66001e832f12113b63a9cce6e1c46eed` |
| `USA.csv` | `4570a978554081429d9ef3fb5b11c75b5dac8da357334183f8b5bdda42c4d337` |
| `AUS.xlsx` | `ea3c7c3502b3398eebc34337f0f33c1f16cbb29d56349219fa5af987ca4adbf4` |

`tests/test_raw_files.py` recomputes these checksums and compares them with this table, so an
accidental edit fails CI. `.gitattributes` turns off line-ending conversion for `data/raw/`,
otherwise `core.autocrlf` on Windows would change the bytes of the CSV files on checkout.
These checksums are of the files as they stand in this repository.

## Created from the PDF (not client files)

| File | What it is |
|---|---|
| `SKYPOINTS_MEMBERS_20240115_00000000.dat` | The member feed example of the brief: the 10-field header (no `Post_Code`) and its five detail lines (Elena, Ravi, Mateo, Nora, Jacob), nothing else. A test checks it line by line. |
| `redemptions.json` | The redemption example of the brief: member `223457`, transactions `RX10091` and `RX10092`, nothing else. A test checks it. |

## Assumed member feed file name

`SKYPOINTS_MEMBERS_YYYYMMDD_HHMMSSTT.dat`, where `TT` is hundredths of a second. The timestamp in
the name is the file's `file_ts`, the version that decides "latest record wins". The brief does
not give a file name, so this pattern is an assumption; the redemption feed uses
`SKYPOINTS_REDEMPTIONS_YYYYMMDD_HHMMSSTT.json` in the same way.

## How the pipeline reads each file

Files named like a feed are read by name. The other files are described in the source manifest,
`src/skypoints/config.py` (`MANIFEST`): feed type, source system, country code, date format and a
fixed `file_ts` / `feed_date`. A file in neither place is listed in the run report with the reason
`no matching feed rule` and is not loaded. This README is one of them.
