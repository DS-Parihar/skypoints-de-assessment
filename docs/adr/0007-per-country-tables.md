# 0007. Per-country tables as requested; alternative noted

## Context
The brief asks for one table per country. At billions of rows per day, many separate tables mean
more DDL and objects to maintain, repeated `MERGE` jobs, and unevenly sized tables; a new country
means a new table and a pipeline change.

## Decision
Build one table per country as requested (`member_ind`, `member_usa`, ..., plus
`members_unassigned`), derived from `member_current` (ADR 0004).

## Alternative
One `member_current` table clustered by `country`, with a secure view per country
(`CREATE SECURE VIEW member_ind AS SELECT * FROM member_current WHERE country = 'IND'`).

It is cheaper at scale because:
- one `MERGE` and one pipeline serve every country, not one per country;
- clustering by `country` prunes partitions, so a per-country query reads only its slice;
- a country move is an update of one row, not a delete from one table and an insert into another;
- a new country needs a view (or a config row), not new tables and jobs;
- storage and warehouse sizing are managed once, with no skew between large and small countries;
- per-country access control still works through the secure views.

## Consequences
- The requested per-country tables are delivered. Swapping them for views later changes nothing for
  consumers, because the names and columns stay the same.
- Until then, there is one set of load jobs and checks per country.
- The recommendation is to move to the clustered table plus views if the number of countries or the
  volume grows.
