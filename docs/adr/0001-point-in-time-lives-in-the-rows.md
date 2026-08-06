# 0001 — Point-in-time lives in the rows, not in Iceberg snapshots

**Status**: Accepted

## Context

A biocOnIce release is a claim about the whole catalog at a moment: Ensembl 116
plus an NCBI dump of a given day plus a GO release. Users must be able to
reproduce a release years later. Iceberg offers time travel, and tags name a
snapshot so expiry will not reclaim it, so "one tag per table per release" is
the obvious mechanism.

## Decision

Every row-bearing derived table carries `first_seen` and `retired_in`. A
release is served by the predicate
`first_seen <= R AND (retired_in IS NULL OR retired_in > R)`.
Snapshots are treated as operational records of writes, not as releases, and
may be expired freely.

## Why not time travel

A snapshot is per-table and per-*write*. A release spans tables, so the
snapshot scheme is only as coherent as our discipline in tagging every table
alike, with nothing enforcing it. It also pins a full generation of every
table's files for as long as the release is published, so storage grows with
releases rather than with churn — measured at 5% extra rows for a full extra
Ensembl release against 72% for a second copy. And it forces clients to resolve
a release to a snapshot id per table, where a predicate is something any R,
Python or SQL client already knows how to write.

## Consequences

Snapshot expiry becomes a storage optimisation rather than a destructive act.
"Current" becomes a filter, and a client that forgets `retired_in IS NULL`
silently sees retired records — so clients must default to it. Retirement must
be computed, which requires each source to publish complete dumps, and cannot
distinguish a retired identifier from one merged into another (see SPEC.md).

Verified: ingesting Ensembl 115 then 116 reconstructs 78,899 genes at the
earlier release, exactly matching Ensembl 115, with no time travel.
