# 0004 — Merge recomputes the scope rather than using PyIceberg upsert

**Status**: Accepted

## Context

Maintaining `first_seen`/`retired_in` against a full upstream dump means
classifying every record as new, changed, unchanged or retired. PyIceberg ships
`Table.upsert(df, join_cols=...)`, which covers new and changed.

## Decision

`merge.merge` computes the scope's complete post-merge state in DuckDB and
writes it with `overwrite(final, overwrite_filter=scope)`.

## What a scope must name

The scope is what the merge is entitled to retire, so it must name everything
that makes the incoming state *complete*: a record in scope and absent from
`incoming` is closed. Two levels have been learned the hard way. The writer: a
`taxon_id` scope let NCBI and Ensembl retire each other's identifier_mapping
rows on alternate ingests, so every scope names its `source`. The assembly
(issue #94, 2026-09-18): Ensembl ships 359 assemblies over 276 taxa, each with
its own gene ids, and under `(taxon_id, source)` the first mouse strain retired
GRCm39's genes. The genome-feature tables (`reference.genome`,
`annotation.gene`, `transcript`, `exon`) are therefore scoped
`And(taxon_id, source, genome_id)`, with `genome_id` in the business key, and
`raw.ensembl__gtf` is replaced per `(taxon_id, genome_id, ensembl_release)`.
`annotation.identifier_mapping` has no assembly and keeps `(taxon_id, source)`;
only a taxon's canonical assembly writes Ensembl's rows there, and an alternate
assembly's ingest does not merge into it at all — an empty merge would retire
them.

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
