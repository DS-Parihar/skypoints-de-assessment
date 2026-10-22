# How I used AI

I used Claude Code for the boilerplate and for first drafts, and checked everything myself. This log records what I asked, what came back, and what I changed.

## Phase 1: profiling the source files

- **Asked for:** a script that reports columns, blanks, distinct values and date formats for every file in `data/raw`.
- **What came back:** a working script. It reported formats per column, not per value, and it didn't read the pipe-delimited member feed.
- **What I changed:** asked it to read the `.dat` file, check the header against the spec, and list the ambiguous date values by name (like `1052022`).
- **What I learned:** a column-level summary hides the ambiguous values. You only notice if you read the counts.
