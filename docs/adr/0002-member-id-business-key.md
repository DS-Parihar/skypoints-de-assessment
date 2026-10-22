# 0002. member_id is the business key, not Member Name

## Context
The spec names Member Name as the key column. Names repeat: "Mike" already appears in both the USA
and AUS files with different tiers. Matching on name would merge different people or split one.
The sample country files (IND, USA, AUS) also reuse IDs 1, 2 and 3 for different people.

## Decision
`member_id` is the business key for "latest record wins", `member_current` and the redemption join.
This contradicts the spec and is flagged to the spec owner as a question. The IND/USA/AUS files are
treated as extra sources of the same member feed and mapped to one common layout; their ID
collisions are recorded as a DQ finding, not modelled with a composite key.

## Consequences
- Records are matched reliably, and a member who moves country stays one member.
- If IDs are not actually global across source systems, different people will be merged. This
  stays an open risk until the spec owner confirms.
- Rows with no `member_id` cannot be ranked and go to quarantine as `MISSING_MEMBER_ID`.
- Redemptions join on `member_id`; `member_id` `1` in the JSON cannot be attributed to a single
  sample file.
