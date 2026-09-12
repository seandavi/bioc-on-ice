# AGENTS.md

Conventions for AI agents (and humans) working on biocOnIce.

## What this project is

Ingest pipelines that turn public biological annotation into Iceberg tables,
plus the thin client helper for reading them. **SPEC.md is the design source
of truth** — read it before writing code. If a change contradicts the SPEC,
edit SPEC.md in the same commit and say so in your report (that is how
`taxon_id` reached `transcript`/`exon`).

Decisions with a non-obvious rejected alternative are recorded as ADRs in
`docs/adr/`. Read them before proposing an architecture change — contradicting
one is allowed and sometimes right, but it should be argued rather than drifted
into. SPEC.md says what the system must do; an ADR says why a decision went the
way it did.

biocOnIce is the *data*; [icegate](https://github.com/seandavi/icegate) is the
gateway that serves it. Nothing here should grow gateway concerns (auth,
routing, credential vending) — that boundary is deliberate.

## Coding rules

- Lazy and minimal: smallest working diff, no speculative abstractions, no
  scaffolding "for later". Deletion beats addition.
- **New sources are SQL, not frameworks.** Each source is one module that
  parses with DuckDB and hands Arrow tables to `ensembl.write`. If a second
  source needs something from the first, move that one thing — don't build an
  ingestion framework for two pipelines.
- No new dependencies without a recorded reason. DuckDB does the parsing;
  PyIceberg does the writing; argparse does the CLI.
- Non-trivial logic lands with a test, and tests stay offline: fixtures over
  network calls (`tests/tiny.gtf`). Before pushing: `uv run pytest`.
- Ingest must stay idempotent and scoped — overwrite filtered on the merge
  scope (`taxon_id`, or every taxon plus the writer's `source`), never blind
  append.
- **PyIceberg is the only writer to the lake.** Never `DELETE`/`UPDATE`/`MERGE`
  a live table through DuckDB or any other engine, even to test. A DuckDB
  write on 2026-08-11 left position-delete files and manifest entries with
  null sequence numbers in `annotation.identifier_mapping`; PyIceberg then
  refused every overwrite (`Only entries with status ADDED can have null
  sequence number`) until the table was rebuilt. The pre-repair copy is
  `annotation.identifier_mapping__pre_repair`, safe to drop.
- Report honestly: if something wasn't verified against real data, say so.
  Row counts in docs come from an actual run.
