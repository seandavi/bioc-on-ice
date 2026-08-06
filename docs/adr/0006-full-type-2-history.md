# 0006 — Attribute changes close a row and open a new one

**Status**: Accepted
**Amends**: [ADR-0001](0001-point-in-time-lives-in-the-rows.md) — its core claim stands; its guarantee was overstated.

## Context

The merge implemented under ADR-0001 updates changed attributes **in place**,
preserving `first_seen`. That is Type 2 for existence and Type 1 for
attributes, and Kimball's definition of Type 1 is explicit: *"this technique
destroys history."*

It was caught empirically. Reconstructing Ensembl 115 from the catalog returned
509,644 transcripts where Ensembl 115 had 509,650. All six missing were
transcripts reassigned to a different gene in 116: because `gene_id` is an
attribute, the merge overwrote it, and the point-in-time query returned rows
asserting they existed at a release while pointing at a parent that did not yet
exist. Ensembl's own `gene_archive` shows **10,144 human transcripts** have
moved between genes historically, so this is not a rounding error.

The guarantee was therefore narrower than ADR-0001 claimed: validity intervals
reconstructed *which records existed*, not *what they said*.

## Decision

On any attribute difference, **close the existing row and insert a new one**.
Nothing is updated in place.

The interval columns are renamed `valid_from` / `valid_to`, because
`first_seen` / `retired_in` describe existence and a row now represents a
*version* of a record. The names were the trap.

This makes attribute change and resurrection **the same rule**, removing a
special case rather than adding one — a record that reappears is simply a
version whose predecessor is closed.

Intervals are expressed in **biocOnIce release coordinates**, not wall-clock,
following UniProt's UniSave (`firstRelease` / `lastRelease`). Release
identifiers are `YYYY.MM` with zero-padded corrections `YYYY.MM.NN`, because
`'2026.10.10' < '2026.10.2'` lexicographically and release ordering would
otherwise invert at the tenth correction.

## Why not the alternatives

**In-place updates with a targeted fix** — promoting `gene_id` into the
business key — patches the one foreign key we noticed and leaves every other
attribute lossy. Full Type 2 fixes the class.

**Full copies per release** (partition by source release) is also exact, and
storage is not the objection at this scale. It loses the ability to ask "when
did this change" as a filter rather than an N-way join, and it cannot express a
source that has no releases at all.

**Storage** was measured, not assumed. Between Ensembl 115 and 116, 17,007 of
78,941 genes and 1,923,488 of 5,087,789 exons changed. Type 2 costs those as
new rows rather than updates — roughly 5× over ten releases, against 20× for
full copies, on tables where ten releases of full copies would still be about
two cents a month. Cost is not what decides this; exactness is.

## Consequences

Referential integrity in the past is restored for free: a historical transcript
row keeps the `gene_id` it had, so the six-transcript defect disappears without
`gene_id` joining the business key. Ensembl solves this with a dedicated
`gene_archive` table; Type 2 makes it fall out of the general mechanism.

Row counts grow with churn. `valid_to IS NULL` still yields exactly one current
row per business key, and that invariant remains enforced in code because
Iceberg declares identifier fields without checking them.

The row key stays business key + `valid_from`. SQL:2011 would express the
constraint as `PRIMARY KEY (bk, period WITHOUT OVERLAPS)` — non-overlap rather
than distinctness — which no lakehouse engine implements, so the intent is
recorded here and the check lives in the merge.

Data Vault has deprecated end-dating in favour of insert-only with the interval
end computed as `LEAD(...)` in a view. Worth revisiting if write cost ever
matters; not now, while a merge takes 23 seconds.
