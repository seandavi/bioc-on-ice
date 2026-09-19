# 0007 — A release manifest, with the provenance of the version string

**Status**: Accepted

## Context

A biocOnIce release is a claim across sources that version themselves
differently, or not at all: Ensembl publishes citable immutable releases, NCBI
Gene regenerates nightly and publishes none. Stamping an invented release
number onto NCBI rows asserts something NCBI never said.

Surveying what aggregators actually do: four of ten publish an upstream-version
manifest, and none has both a manifest and a replacement record. InterPro
embeds `<dbinfo version dbname entry_count file_date>` in the data file itself,
so a 2010 SSF release and a 2026 UniProt release sit side by side, each honest
about its own age. Monarch's `metadata.yaml` adds the field that makes a
manifest survive reality: `version_method`, recording *how* the version was
learned, and a willingness to record `version: unknown`.

## Decision

A `provenance.release` table, one row per (biocOnIce release, source, artifact):

```
release            -- 2026.10
source             -- the provider: ensembl, ncbi_gene, obo
artifact           -- what of it was read: homo_sapiens, gene_info, cl
source_version     -- '116', or '2026-08-06' where the source has no version
version_method     -- how we learned it: release_number, http_last_modified,
                   --   ftp_index_probe, retrieval_date, unavailable
retrieved_at
url
checksum
row_count          -- free integrity check, per InterPro's entry_count
```

`artifact` was added on 2026-09-19 (issue #96). Keyed by source alone, 276
Ensembl species overwrote one another's row, the three NCBI Gene dumps shared
one row with a summed count, and the ontologies escaped only by minting a
source per ontology (`obo_cl`). The source is the provider; the artifact is the
file, species or ontology an ingest actually read, and each ingest replaces
only its own row. Rows written before then that summarised several files keep a
NULL artifact.

`source_version` is recorded **in the source's own vocabulary**, never
normalised. `version_method` is mandatory, and `unavailable` is a legitimate
value — a source that publishes no version must be recorded as such rather than
given a fabricated one.

This is what makes "reproduce biocOnIce 2026.10" answerable: resolve the
release through the manifest to each source's own version, then query each
table at that release.

## Why a table rather than snapshot properties

Snapshot summaries are per-table and die with expiry. The manifest is
catalog-wide and must outlive any snapshot, for the same reason ADR-0001 puts
history in the rows. Per-table fetch facts still ride on snapshot properties;
the manifest is the durable cross-source record, and the two do not duplicate:
one describes a write, the other describes a release.

## Consequences

The manifest is where citation metadata lands, which is what the RDA Data
Citation recommendations (R10/R11) ask for and what makes "biocOnIce 2026.10"
citable rather than merely named.

It also gives the mixed-cadence problem somewhere to live without contaminating
the data: rows carry release coordinates (ADR-0006), and the translation to
"Ensembl 116" or "NCBI retrieved 2026-08-06" happens here, once.

Deliberately **not** included: a successor column for merged identifiers. When
a source needs it — dbSNP rsIDs merge, UniProt accessions become secondary —
it belongs beside the record, modelled on NCBI Taxonomy's split of `delnodes`
from `merged`, never as a sentinel value and never as free text inside a status
string, which is how RefSeq and ChEMBL get it wrong.
