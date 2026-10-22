# 0006. Country routing is config-driven; unknown or NULL country goes to members_unassigned

## Context
Country codes in the data are mixed: `USA`, `IND`, `CAN`, `PHIL`, `AU`. Country is not mandatory but
drives routing, so a blank or unrecognised value would otherwise lose the member.

## Decision
- A `country_code_map` lookup table maps every raw value to a canonical ISO-3 code and its target
  table. Adding a country is a data change, not a code change.
- A NULL, blank or unmapped country routes the member to `members_unassigned` with a
  `country_unknown` flag.
- The member is never dropped.

## Consequences
- `PHIL` and `AU` are handled by two config rows, not hard-coded branches.
- A new country or spelling lands in `members_unassigned` until someone adds a mapping row.
- The map contents need confirming with the data owner.
- `members_unassigned` needs monitoring, since a growing count means missing mappings.
