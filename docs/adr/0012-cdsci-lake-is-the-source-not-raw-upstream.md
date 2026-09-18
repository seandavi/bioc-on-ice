# 0012 — cdsci-lake is the source, not raw upstream, for platform-curated data

**Status**: Proposed — accepted once the first migrated lander is verified
live against the icegate write path.

## Context

Every current lander (`bugsigdb.py`, `ncbi.py`, `ncbi_accession.py`,
`ncbi_pubmed.py`, `ncbi_go.py`, `ensembl.py`) fetches its own copy straight
from raw upstream (GitHub, NCBI FTP, Ensembl) and implements its own
extract/parse/land logic, independent of anything else on the platform.

Meanwhile `cdsci-lake` (the shared DuckLake at `onclappc02`, Postgres catalog
+ R2 data) already does exactly this extract/curate/merge work for a growing
and overlapping set of sources — `bugsigdb` is being added there as this ADR
is written, and omicidx's SRA/GEO/PubMed/BioSample/BioProject tables already
live there. Two independent implementations of the same curation logic is
duplicated work today and a silent-divergence risk tomorrow — two different
views of "the current state of BugSigDB," reconciled by nobody.

This is the local instance of a platform-wide split: DuckLakes (cdsci-lake,
and any future ones) are the ETL and business-logic layer; icegates
(bioc-on-ice, and potentially siblings) are the interop and publication
layer for "finished" products — cross-language (R, Python, SQL — SPEC.md's
own goal), decoupled from any one source's ETL quirks or fetch cadence.
omicidx's own path to publication runs through a DuckLake first, not
straight into an icegate.

## Decision

A bioc-on-ice lander sources from a DuckLake's already-curated table, not raw
upstream, **whenever a DuckLake source already curates the domain it wants to
publish**. Where none exists yet, landing from raw upstream directly remains
fine — this doesn't retroactively invalidate `ensembl.py`/`ncbi.py`; it's a
default for new work and migrated work, not a mandate to route everything
through a DuckLake regardless of whether one already does the job.

Today that DuckLake is `cdsci-lake`; the pattern generalizes (see
`monode/infrastructure/PUBLISHING.md`) to any DuckLake, so a future source
publishing from a different one doesn't need a new ADR to justify the shape,
only to name the specific source.

## Mechanism

No file-level copying — both sides are DuckDB/Arrow-native, so it's a
same-process read/write hop:

```python
# read: cdsci-lake's own connection helper
from cdsci.lake.connect import lake_connect
con = lake_connect(read_only=True)
arrow = con.sql("SELECT ... FROM lake.<schema>.<table>").arrow()

# write: bioc-on-ice's own existing primitive, unchanged
from bioconice import catalog, merge
cat = catalog()
merge.write(cat, "raw.<source>__<table>", arrow, overwrite_filter)
```

The lander keeps its existing shape (`land_raw` + `merge.manifest`, ADR-0002,
ADR-0007) — only where the bytes come from changes.

## Consequences

- A migrated lander's availability and schema stability now depend on
  cdsci-lake, not the original upstream. cdsci-lake's own glossary already
  defines this relationship (`consumer`, `read_only=True`, "assumes the data
  exists") — bioc-on-ice becomes exactly that kind of consumer, same
  contract as any other.
- `bugsigdb.py` is the first concrete candidate, once cdsci-lake's `bugsigdb`
  source lands: swap `land_raw`'s `read_csv(url, ...)` for a `lake_connect()`
  read of `lake.bugsigdb.*`, keep everything downstream (`merge.write`,
  `merge.manifest`) as-is.
- This ADR exists so the default gets decided once, here, rather than
  re-litigated per source as each migration comes up.
