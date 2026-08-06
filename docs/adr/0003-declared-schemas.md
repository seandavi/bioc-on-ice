# 0003 — Declare Iceberg schemas; never infer from Arrow

**Status**: Accepted

## Context

Tables were originally created with `create_table_if_not_exists(schema=arrow.schema)`,
which is the shortest path from a DuckDB query to a table.

## Decision

Every table is declared in `src/bioconice/schemas.py` as a pyiceberg `Schema`
with `identifier_field_ids`, a per-field `doc`, a table `comment`, and semantic
properties. Arrow tables are cast to the declared schema at write time.

## Why not inference

Two things required by SPEC.md cannot survive inference. **Identifier fields**
are the merge key and must be non-nullable, which an Arrow schema does not
express. **Column docs** are what make the catalog self-describing; they reach
clients as Arrow field metadata, and there is nowhere to put them if the schema
is derived. A third benefit was not the motivation but matters as much: the
cast is a check, so a column the SQL failed to produce fails loudly instead of
landing as a silent NULL.

A YAML file plus a loader was considered. The `Schema` objects *are* the
declaration, so YAML would add a format to invent and a parser to write.

## Consequences

Adding a column means editing two places, the schema and the SQL that fills it;
the cast catches disagreement. Merge reads keys and attributes from the
declared schema, so it is generic over tables without being told about them —
the main payoff.
