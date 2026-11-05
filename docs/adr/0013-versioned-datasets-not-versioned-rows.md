# 0013 — Versioned datasets, not versioned rows

**Status**: Accepted; its release layout (per-dataset `cdsci.lake.publish` releases, the first `bioconice-bugsigdb`) is superseded by [ADR-0014](0014-public-releases-are-parquet-plus-duckdb-views.md)'s whole-product bundle, and `release.py` was removed once the bundle's gate, upload and Hugging Face push moved to `cdsci.publish` (cdsci-lake ADR-0027). The principle, versioned datasets and not versioned rows, stands.
**Supersedes**: [ADR-0001](0001-point-in-time-lives-in-the-rows.md) and [ADR-0006](0006-full-type-2-history.md), for dataset releases.
**Amends**: [ADR-0012](0012-cdsci-lake-is-the-source-not-raw-upstream.md)

## Context

ADR-0001 and ADR-0006 put point-in-time access in `valid_from` / `valid_to` on
every row. The platform (cdsci-lake ADR-0025) has dropped row history for
versioned datasets: immutable full-snapshot releases, each with its own cadence
and retention. Row history forces complete-scope retirement, release-ordering
guards and a dependency on lake time travel, while every client only ever
queries current rows and a pinned release reproduces better than a predicate.

## Decision

biocOnIce publishes **dataset releases** through `cdsci.lake.publish`
(cdsci-lake ADR-0025). A release is an immutable full snapshot of one source
family, identified `YYYY-MM-DD` (UTC build date; a second release the same day is
`YYYY-MM-DD.2`, `.3`, ...). Upstream versions go in provenance, never in the id.
The first dataset is `bioconice-bugsigdb`, projected from the lake table
`lake.bugsigdb.signature_taxon`. ADR-0001 and ADR-0006 are superseded for
dataset releases.

**Archive rule.** Iceberg tables keep their `valid_from` / `valid_to` rows only
until their source moves to dataset releases; after that they receive no further
writes and stay as a read-only archive. No backfill into releases.
The `bioconice-bugsigdb` pilot does not yet make that move: the legacy
`ingest-bugsigdb` command and its Iceberg tables stay active until the remaining
BugSigDB consumers are repointed (follow-on work), and the archive rule applies
from then on.

**ADR-0012.** Its acceptance condition becomes "the first dataset release built
from cdsci-lake verifies against real lake data".

## Why not row history

- Clients query current rows; a pinned release is a better reproducibility
  handle than a validity predicate.
- SCD2 needs complete-scope retirement and release-ordering guards on every
  merge.
- Releases are verified end to end (hashes, contract, frozen DuckLake) as a unit.

## Consequence: releases carry no row-history columns (#222, 2026-11)

The public bundle (ADR-0014) drops `valid_from`, `valid_to` and `landed_in` from
every table, Iceberg-read ones included. In a release `valid_to` was always NULL,
and the values of the other two were frozen once a source moved to cdsci-lake.
Current rows are still selected with `valid_to IS NULL` when the bundle reads
Iceberg; the column is just not published. The one release that applies the change
passes the gate with the owner-approved `bioconice gate --allow-schema-change`.
