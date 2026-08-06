# 0004 — Merge recomputes the scope rather than using PyIceberg upsert

**Status**: Accepted

## Context

Maintaining `first_seen`/`retired_in` against a full upstream dump means
classifying every record as new, changed, unchanged or retired. PyIceberg ships
`Table.upsert(df, join_cols=...)`, which covers new and changed.

## Decision

`merge.merge` computes the scope's complete post-merge state in DuckDB and
writes it with `overwrite(final, overwrite_filter=scope)`.

## Why not upsert

Two reasons, one fatal. Upsert has no not-matched-by-source leg, so retirement
has to be computed separately regardless — that part is merely inconvenient.
The fatal one is that upsert derives a filter predicate from every join key:
5,087,789 exons had not completed after ten minutes, where overwrite takes 23
seconds. This is a property of the library, not of our data shape.

## Consequences

Unchanged rows are rewritten, so write volume is proportional to the table
rather than to churn. Storage is unaffected once snapshots are expired, because
ADR-0001 puts history in the rows rather than in retained files — so the cost
is write time, not bytes.

Upgrade path if write time matters: partition by `taxon_id` and replace only
touched partitions. Do not reach for upsert again without checking whether the
predicate-per-key behaviour has changed upstream.
