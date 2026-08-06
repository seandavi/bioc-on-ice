Yes. I think this is the right scope, and I would frame it carefully: **biocOnIce is not an "Iceberg replacement for every biological file format."** It is a **Bioconductor-native data catalog and access layer**, where Iceberg is used for relational/tabular scientific data and metadata, while large scientific objects remain in their native formats (FASTA, FASTQ, BAM/CRAM, Zarr, TileDB, BigWig, etc.).

The architectural boundary is:

```text
                         biocOnIce Catalog
                                |
              +-----------------+----------------+
              |                                  |
       Structured biological              Resource metadata
             tables                           catalog
              |                                  |
        Apache Iceberg                  Apache Iceberg
              |                                  |
     genes, taxa, ontology             FASTA, FASTQ, Zarr,
     transcripts, variants             TileDB, HDF5, etc.
                                             |
                                             |
                                      Object storage
                                     (R2/GCS/S3/etc.)
```

Below is a draft engineering specification.

---

# biocOnIce Specification

## Title

**biocOnIce: A Cloud-Native Biological Annotation and Data Resource Catalog for Bioconductor**

## Vision

biocOnIce provides a versioned, language-independent, cloud-native data layer for biological annotation and experimental resources.

It replaces package-bound distribution of annotation resources with:

* Apache Iceberg tables for structured biological knowledge
* object-store references for large scientific data objects
* provenance-aware resource metadata
* reproducible snapshots
* access from R, Python, SQL, and other Iceberg-compatible clients

---

# Design Goals

## Primary goals

1. Replace package-based annotation data distribution
2. Preserve Bioconductor user workflows
3. Enable access outside R
4. Provide reproducible versioning
5. Support cloud-native data access
6. Provide machine-readable provenance
7. Support AI/ML-ready biological knowledge access

---

## Non-goals

biocOnIce will not:

* replace FASTA/FASTQ formats
* replace BAM/CRAM
* replace Zarr/TileDB
* become a compute engine
* become a workflow orchestrator
* become a universal biological database

It is a **catalog and data access layer**.

---

# System Architecture

```text
                 Data Sources

 NCBI    Ensembl    UniProt    GEO    SRA
   |        |          |        |      |
   +--------+----------+--------+------+
                    |
              ingestion pipelines
                    |
                    v

              biocOnIce warehouse

          +-----------------------+
          | Iceberg tables        |
          |                       |
          | genes                 |
          | transcripts           |
          | taxa                  |
          | ontology              |
          | resources             |
          | provenance            |
          +-----------------------+

                    |
                    |
              IceGate REST Catalog

                    |
        +-----------+------------+
        |           |            |
       R         Python       DuckDB
```

---

# Catalog Organization

The initial public catalog:

```
biocOnIce
```

Namespaces:

```
reference
annotation
taxonomy
ontology
variant
experiment
resource
provenance
```

---

# Core Data Model

## 1. Taxonomy

Namespace:

```
taxonomy
```

Tables:

## taxon

Represents biological organisms.

```sql
taxon
-----
taxon_id
parent_taxon_id
scientific_name
common_name
rank
ncbi_taxonomy_id
```

Examples:

```
9606 Homo sapiens
10090 Mus musculus
```

---

## taxon_synonym

```sql
taxon_synonym
--------------
taxon_id
synonym
source
```

---

# 2. Genome Reference

Namespace:

```
reference
```

## genome

```sql
genome
------
genome_id
taxon_id
provider
assembly_name
assembly_accession
release
checksum
```

Examples:

```
GRCh38
GRCm39
```

---

## sequence

Metadata only.

The sequence itself remains external.

```sql
sequence
--------
sequence_id
genome_id
name
length
is_circular
md5
uri
format
```

`length` and `is_circular` are what a client needs to reconstruct `seqinfo()`
and to clamp `promoters()` at chromosome ends; without them, flanking queries
run off the end of a sequence silently.

Example:

```
chr1
248956422
md5
r2://genomes/hg38.fa
```

---

# 3. Gene Annotation

Namespace:

```
annotation
```

## gene

```sql
gene
----
gene_id        -- stable, unversioned: ENSG00000141510   [identifier]
taxon_id                                                 [identifier]
version        -- upstream record version, changes over releases
symbol
description
gene_type
source
first_seen     -- biocOnIce release
retired_in     -- biocOnIce release, NULL while current
```

Columns marked `[identifier]` form the Iceberg identifier fields — the merge
key. They are the *unversioned* stable id: an upstream version bump is an
update to an existing row, not a new one. `first_seen` / `retired_in` are
explained under [Versioning Model](#versioning-model).

---

## transcript

```sql
transcript
-----------
transcript_id  -- stable, unversioned                      [identifier]
taxon_id                                                   [identifier]
gene_id
version
biotype
canonical
first_seen
retired_in
```

---

## exon

```sql
exon
----
exon_id        -- stable, unversioned                      [identifier]
transcript_id  -- an exon is shared across transcripts     [identifier]
taxon_id                                                   [identifier]
sequence_name
start
end
strand
rank           -- ordinal within the transcript, 5' to 3'
cds_start      -- NULL where this exon is not translated
cds_end
cds_phase      -- reading frame; not recoverable from coordinates
first_seen
retired_in
```

This one table stands in for three of TxDb's. TxDb separates `exon`, `cds` and
a `splicing` junction carrying `(tx_id, exon_rank, exon_id, cds_id,
cds_phase)`; because a row here is already keyed by exon *and* transcript, it
is that junction, and a coding segment always falls within an exon of the same
transcript, so the CDS bounds ride along.

`rank` is load-bearing, not a convenience: it is the only thing that orders a
transcript's exons biologically rather than by coordinate, which is the
difference between correct and reversed on the minus strand. Without it
`exonsBy(by="tx")`, `intronsByTranscript()`, `transcriptLengths()`,
`extractTranscriptSeqs()` and the whole `mapToTranscripts()` family cannot be
served. A GTF supplies it as `exon_number`, so omitting it is a modelling
error rather than a data limitation.

`cds_phase` is likewise unrecoverable downstream — coordinates do not imply
frame — and without it `proteinToGenome()` and any correct-frame translation
are impossible. UTRs are NOT stored: they are derived from CDS bounds, and a
transcript with no CDS correctly has no UTRs rather than empty ones.

---

## protein

```sql
protein
-------
protein_id
gene_id
transcript_id
uniprot_id
```

---

# 4. Identifier Mapping

This replaces much of OrgDb.

```sql
identifier_mapping
------------------
source_namespace
source_id

target_namespace
target_id

taxon_id
source
confidence
first_seen
retired_in
```

Here the whole tuple `(source_namespace, source_id, target_namespace,
target_id, taxon_id)` is the identifier — a mapping has no attributes that can
change, so it is only ever asserted or withdrawn, never updated. That makes
`retired_in` the only signal that a cross-reference went away, and the reason
retirement has to be modelled rather than left implicit.

`taxon_id` appears on `transcript`, `exon` and `identifier_mapping` beyond what
the entity itself strictly needs, because it is part of every merge key: it
keeps ingest scoped to one species without a scan of the whole table.

Examples:

```
ENSEMBL ENSG00000141510
ENTREZ 7157
SYMBOL TP53
```

---

# 5. Ontologies

Namespace:

```
ontology
```

## term

```sql
term
----
ontology
term_id
name
definition
```

---

## relationship

```sql
relationship
------------
ontology
subject
predicate
object
```

---

## annotation

```sql
annotation
----------
entity_id
term_id
evidence
source
```

Supports:

* GO
* HPO
* MONDO
* Cell Ontology
* Uberon

---

# Resource Metadata Layer

This is the key expansion beyond OrgDb/TxDb.

Namespace:

```
resource
```

## resource

A universal catalog entry.

```sql
resource
--------
resource_id
title
description
type
format
uri
size
checksum
license
provider
created
```

Types:

```
fasta
fastq
bam
cram
zarr
tiledb
parquet
iceberg
bigwig
hdf5
```

---

## resource_relationship

Connects resources.

```sql
resource_relationship
---------------------
resource_id
relationship
target_id
```

Examples:

```
FASTQ
 derived_from
 BioProject
```

---

# Experimental Metadata

Namespace:

```
experiment
```

## dataset

```sql
dataset
-------
dataset_id
title
description
organism
assay
publication
```

---

## sample

```sql
sample
------
sample_id
dataset_id
taxon_id
attributes
```

---

## assay

```sql
assay
-----
assay_id
technology
platform
```

---

# Large Data Integration

biocOnIce does not ingest:

```
sample.fastq.gz
```

Instead:

```sql
resource
--------
resource_id
type = fastq
uri =
r2://sra/SRR123.fastq.gz
```

Similarly:

## Zarr

```sql
resource
--------
type=zarr
uri=r2://atlas/cellxgene.zarr
```

## TileDB

```sql
resource
--------
type=tiledb
uri=r2://spatial/sample1
```

---

# Variant Resources

Namespace:

```
variant
```

These are the part of the showcase that is **not** a replacement. They are
selected for being things a Bioconductor annotation package cannot carry, so
their presence argues for the architecture rather than merely matching it.

## Selection rule

A resource qualifies only if it is (a) redistributable by a third party, (b)
published as a complete dump per release, since retirement is computed by set
difference, and (c) not already served well by a maintained Bioconductor
package. Licence is a hard gate assessed **before** any ingest work.

## Selected for the first release

| Resource | Why | Licence |
| --- | --- | --- |
| **Open Targets Platform** | already native Parquet on EBI's FTP, complete versioned re-release per cycle, and it now absorbs Open Targets Genetics | CC0 1.0 |
| **ClinVar** | the canonical fast-moving resource — comprehensive TSV weekly, archived monthly — and Bioconductor ships none of it | NCBI open, attribution requested |
| **GWAS Catalog** | cheap flat TSV, complete per release, and joins to Open Targets on study accessions | EMBL-EBI terms |

ClinVar MUST be ingested from the tab-delimited comprehensive release, not the
VCF, which NCBI marks partial. Reproducible pins use the archived monthly
release; the weekly serves currency.

## Ruled out, and why

Recording these so they are not revisited casually:

* **COSMIC** — downloads are registration-gated and the academic grant runs to
  the registered user, not to a republisher. Also blocks using Open Targets'
  Cancer Gene Census evidence as an indirect route.
* **DisGeNET** — non-commercial and share-alike; the NC term alone is
  disqualifying for a public catalog.
* **gnomAD** — ODbL share-alike would propagate to every derived table we
  publish, and the bundled SpliceAI columns are separately non-commercial.
  Highest profile of any candidate, so this is a decision to revisit
  deliberately with a licensing call, not to drift into.
* **dbSNP** — 28 GB of VCF for a resource that moves every year or two; the
  worst effort-to-value ratio on the list, notwithstanding that Bioconductor's
  SNPlocs packages are frozen at build 155.
* **HPO** — a custom licence requiring that the logical relationships not be
  altered, which normalising into `ontology.relationship` arguably does. Needs
  review before ingest.
* **KEGG** (the OrgDb `PATH` column) — redistribution is restricted, so this
  column is knowingly out of scope for OrgDb parity.

## Candidates for later releases

Verified as having **no** R or Bioconductor coverage of any kind — no package,
no CRAN client, no AnnotationHub or ExperimentHub record: **eQTL Catalogue**
(migrating to Parquet, which suits us), **NCBI ALFA**, **MaveDB**, and **NCBI
dbVar**. GTEx eQTL/sQTL is reachable only through a rate-limited CRAN REST
wrapper unsuited to bulk work, with nothing at all in AnnotationHub. PharmGKB
appears only as a pathway subset inside `graphite`.

These are recorded because the gap is the argument; none is in scope before
the delivery path and the parity layer exist.

The ontology packages tell the same story more sharply, and bear directly on
the `ontology` namespace: `DO.db` still ships a 2015 snapshot, `HPO.db` a 2023
one while two newer snapshots sit unused in AnnotationHub, and `ontoProc`'s
whole ontology set froze in April 2023 — including MONDO at the 2022-12-01
release, against a monthly upstream cadence. Meanwhile `DOSE` solved its own
currency problem by dropping those packages and downloading current data from a
personal GitHub Pages branch, with no Bioconductor versioning, no
AnnotationHub record, and no provenance trail at all. That is precisely the
failure mode biocOnIce exists to remove: the choice should not be between
stale-but-tracked and current-but-untraceable.

---

# Provenance Model

Namespace:

```
provenance
```

## source

```sql
source
------
source_id
provider
url
retrieved
version
checksum
```

---

## transformation

```sql
transformation
--------------
input
output
software
version
parameters
timestamp
```

---

# Versioning Model

Three things are versioned, and conflating any two of them causes trouble:

| Concept | Created by | Scope | Identified as |
| --- | --- | --- | --- |
| **biocOnIce release** | us, editorially | the whole catalog | `2026.10` |
| **Iceberg snapshot** | every write, automatically | one table | opaque int64 |
| **upstream version** | the data provider | one source | Ensembl 116, GO 2026-06-26 |

A **release** is a curatorial claim: one coherent state across every table and
every source, of the kind a methods section cites.

```
biocOnIce 2026.10
  |
  +-- Ensembl 116
  +-- NCBI taxonomy retrieved 2026-08-06
  +-- GO 2026-06-26
```

Releases are `YYYY.MM`, ordered lexicographically, with `YYYY.MM.N` for
corrections. A published release MUST NOT be redefined; a mistake is corrected
by publishing a successor.

A **snapshot** is a mechanical record of a single write to a single table. It
is per-table, so no snapshot describes the catalog; it is per-write, so several
compose one release and some correspond to nothing meaningful (a re-run after a
bug fix). Snapshot ids are opaque and unordered across tables.

## History lives in the rows, not in the files

Point-in-time access MUST be served by the `first_seen` / `retired_in` columns
on every row-bearing table, not by Iceberg time travel:

```sql
-- the catalog as it stood at release 2026.10
WHERE first_seen <= '2026.10'
  AND (retired_in IS NULL OR retired_in > '2026.10')

-- current state
WHERE retired_in IS NULL
```

This follows from what the two mechanisms can express. A release spans tables
and snapshots do not, so a snapshot-per-release scheme is only as coherent as
our discipline in tagging every table alike, with nothing enforcing it. It also
pins a complete generation of every table's files for as long as the release is
published, where validity columns store an unchanged row once no matter how
many releases it survives — for exons, which barely change between Ensembl
releases, that is the difference between linear growth and near-flat growth.
And a predicate is something an R, Python or SQL client can write, where
resolving a release to a per-table snapshot id is not.

The corollary is that snapshot expiry becomes a pure storage optimization
rather than a destructive act, since no published release depends on snapshot
retention. Cutting a release SHOULD still tag the resulting snapshot with the
release name as a rollback anchor, but nothing about serving that release may
depend on the tag surviving.

The cost of this choice is that "current" is a filter, and a client that omits
it silently sees retired records. Clients MUST therefore default to
`retired_in IS NULL` and require an explicit opt-in to see history.

## Raw and derived are separate layers

Ingest is ELT, in two phases with different semantics.

**Land**: the source file is written verbatim into the `raw` namespace, before
interpretation — for a GTF, all nine columns including the unparsed attribute
blob. **Transform**: derived tables are computed by reading `raw` back out.

The split buys two things. Changing how a source is *interpreted* becomes a
re-run of transform rather than a re-download, so a parsing fix or a
newly-needed attribute costs nothing upstream. And the raw layer is the audit
trail: what we were given, as we were given it, separable from what we made of
it.

The two layers MUST NOT share write semantics:

| | `raw` | derived |
| --- | --- | --- |
| identity | none — a source line has no natural key | declared identifier fields |
| write | replace wholesale per source version | merge |
| history | accumulated source versions | `first_seen` / `retired_in` |

A source release that is immutable — an Ensembl release, an archived ClinVar
month — makes raw idempotent under re-ingest without any merge machinery:
replacing everything for that version is both correct and cheap. Validity
intervals belong on the derived tables, which are the ones with keys and a
maintained current state.

Raw therefore grows with the number of source versions retained, which is the
price of being able to re-derive without re-fetching. How many are kept is a
retention policy, not a correctness question.

## Columns exist when something fills them

A column is added when a source populates it, by schema evolution, rather than
shipped early as a permanent NULL. A column that is always NULL advertises a
capability the catalog does not have, and is worse than its absence — a client
cannot distinguish "not loaded yet" from "not applicable". Gene descriptions
and assembly checksums are absent for this reason until NCBI ingest lands.

## Ingest is release-scoped

A release is cut by running every source against it, so ingest takes the
biocOnIce release as an argument and the upstream version as a source-specific
one. Each source's records are merged on the identifier fields:

- **matched, and some attribute differs** — update the row in place
- **not matched** — insert with `first_seen` set to this release
- **present with `retired_in IS NULL` but absent upstream** — set
  `retired_in` to this release

Only those three sets are written; a record that survives a release unchanged
is not rewritten. Retirement is computed by set difference against the previous
state, which requires each source to publish a **complete** dump per release.
Sources that publish deltas are out of scope for this model.

If releases are ingested out of order or one is skipped, a record retired
upstream during the gap is attributed to the release that noticed it. That is
accepted, and MUST be documented rather than papered over.

## Retirement is not the same as merging

Set difference cannot tell a record that *disappeared* from one that was
**merged into another identifier**. dbSNP is the canonical case: rsIDs are
routinely merged between builds, so a naive diff reports the absorbed id as
retired when it in fact still resolves, and a client following the old id gets
a false negative rather than a redirect.

A source with merge semantics MUST therefore supply its merge history, which
is ingested alongside the data so that a superseded identifier records what
replaced it. A source that merges identifiers and publishes no merge table
cannot be retired correctly and MUST NOT be ingested under this model.

## Deciding whether a source changed

For unversioned sources, no HTTP-layer signal answers this. NCBI serves no
`ETag` and regenerates its dumps nightly, so `Last-Modified` and any published
checksum both change daily on identical content; Ensembl serves a size-and-
mtime `ETag`, but its release number already answers the question. The merge
diff is therefore the authority on whether anything changed: if nothing did,
it writes nothing.

Conditional requests are a bandwidth optimization, not a correctness
mechanism. `ETag`, `Last-Modified` and retrieval time MUST be recorded as
provenance regardless of whether they were used to skip a fetch.

---

# Semantic Layer

The catalog MUST be self-describing. An agent — or a person — that can reach
the catalog and nothing else must be able to determine what a table holds, what
each column means, and how to join it to another table, without access to this
document or to any biocOnIce client library.

This is a requirement on every table from the first release, not a later
enrichment. Documentation that ships separately from the data drifts from it;
documentation carried *by* the table cannot.

## Where the descriptions live

Iceberg carries this natively, so biocOnIce MUST NOT introduce a sidecar
metadata store:

* **Column descriptions** use the Iceberg schema's per-field `doc`. These
  survive conversion to Arrow as field metadata, so they reach R, Python and
  DuckDB clients without biocOnIce being involved, and `update_column` can
  revise them without rewriting data.
* **Table descriptions** use the table property `comment`.
* **Namespace descriptions** use namespace properties.

Because `doc` and identifier fields both require a declared Iceberg schema,
tables MUST NOT be created from an inferred Arrow schema.

## What a description must say

A column's `doc` states what the value *is*, not what it is called. It MUST
resolve the things a reader cannot infer from the name and type:

* the authority a value belongs to — `gene_id` holds Ensembl stable ids, not
  arbitrary strings
* cardinality, where a join would otherwise be assumed unique — an Ensembl
  exon id recurs across every transcript containing it
* the convention behind a number — coordinates are 1-based and
  end-inclusive, following Ensembl and GTF, and NOT the 0-based half-open
  convention of BED and UCSC
* what a null means, where null is meaningful — `retired_in IS NULL` means
  current, not unknown

## Machine-actionable properties

Prose serves agents well and programs badly, so two facts that software must
act on are ALSO carried as structured table properties, keyed by column:

* `bioc.column.<name>.prefix` — the [Bioregistry](https://bioregistry.io)
  prefix for an identifier column (`ensembl`, `ncbigene`, `ncbitaxon`, `hgnc`,
  `go`), which is what lets a client resolve a bare id to a URI without
  hard-coding a mapping per column.
* `bioc.column.<name>.coordinate_system` — `1-based-inclusive` on any
  coordinate column.

These two are structured because getting them wrong is silent: an off-by-one
from a coordinate convention and a mis-resolved identifier both produce
plausible, wrong answers rather than errors. Everything else stays prose. This
is deliberately not an ontology, and MUST NOT grow into one without a decision
recorded against this spec.

## One declarative source

Descriptions and semantic properties MUST be declared in a single file in the
repository and applied at table creation and on schema evolution — never
written inline at the call site that happens to create a table, which is how
they rot. A table whose columns lack `doc` is incomplete, and the acceptance
suite checks this.

---

# API Expectations

## R

Future:

```r
library(biocOnIce)

gene <- bioc_table(
    "annotation.gene"
)

gene |>
    filter(symbol=="TP53") |>
    collect()
```

---

## Python

```python
catalog.load_table(
    "annotation.gene"
)
```

---

## SQL

```sql
SELECT *
FROM annotation.gene
WHERE symbol='TP53'
```

---

# Acceptance Criteria

What "it works" means. Each criterion is stated so that it can fail: a claim
that cannot be checked against a live catalog is not a criterion.

Criteria are verified by an acceptance suite run against a real deployment
with real clients, following icegate's convention — not by unit tests with
mocks.

## A. Versioning and point-in-time

1. Two releases are cut from two different Ensembl releases. A point-in-time
   query at the earlier release returns exactly the row set that release
   returned when it was current.
2. A gene retired upstream between those releases is absent from the current
   view, carries `retired_in` equal to the later release, and is still present
   in the point-in-time view of the earlier one.
3. **All snapshots except the current one are expired, and both point-in-time
   queries still return identical results.** This is the criterion that proves
   history lives in the rows; if it fails, the versioning model is wrong.
4. Re-ingesting a source whose content has not changed adds no data files and
   changes no row.
5. Warehouse size after N releases grows with upstream churn, not with N times
   the size of the catalog.
6. Every ingest writes a `provenance.source` row carrying URL, retrieval
   timestamp, upstream version where one exists, and `ETag` / `Last-Modified`
   where the server supplies them.

## B. Self-description

1. Every column of every published table has a non-empty `doc`; every table has
   a `comment`; every namespace has a description. A table failing this does
   not ship.
2. Those descriptions are visible to a client that has only the catalog
   endpoint — verified in both R and Python, through the Arrow field metadata,
   with no biocOnIce library installed.
3. Every identifier column declares a Bioregistry prefix that resolves, and
   every coordinate column declares `1-based-inclusive`.
4. An LLM agent given catalog access, a fixed set of biological questions, and
   **no access to this spec or to biocOnIce documentation** writes SQL that
   returns the correct answers. This is the operational definition of
   agent-aware, and the question set is version-controlled alongside the suite.

## C. Access through icegate

1. The catalog is served through icegate and every criterion in this document
   is verified through that endpoint, not against the backend catalog directly.
2. PyIceberg, DuckDB, and R read the catalog using only stock Iceberg client
   configuration — an endpoint, a warehouse name, and a token. No
   biocOnIce-specific client code is required to read any table.
3. Browser-based DuckDB-WASM reads the catalog, exercising icegate's CORS
   handling.
4. Anonymous read access works for the public namespaces, and the public
   catalog cannot be written through, whatever key is presented.
5. Table data is read directly from object storage via vended credentials;
   only metadata traffic crosses the gateway.

## D. OrgDb parity

`org.Hs.eg.db` exposes 26 columns off a central ENTREZID key, and `select()`
is explicitly many-to-many.

1. These columns are served: ENTREZID, ENSEMBL, ENSEMBLTRANS, ENSEMBLPROT,
   SYMBOL, ALIAS, GENENAME, GENETYPE, REFSEQ, UNIPROT, ACCNUM, UCSCKG, MAP,
   OMIM, PMID, GO, GOALL, ONTOLOGY, ONTOLOGYALL, EVIDENCE, EVIDENCEALL.
2. Knowingly **not** served, and documented as such: PATH (KEGG, redistribution
   restricted) and the legacy protein-domain columns PFAM, PROSITE, IPI,
   ENZYME. A criterion that is out of scope must be stated, not silently
   dropped.
3. A key with several matches returns **every** match — the full multiset, one
   row per match, matching `select()`'s row-multiplication rule. Returning a
   deduplicated or first-match-only result is a failure, since `mapIds()`'s
   `multiVals` behaviour is a client-side collapse of exactly this.
4. `GOALL` / `ONTOLOGYALL` / `EVIDENCEALL` include ancestor terms. This is a
   transitive closure over the GO graph, not a join, and is the form GO
   enrichment consumes.
5. For a fixed gene set, results are compared against a real `org.Hs.eg.db`
   and every difference is attributable to a stated source-release difference.

## E. TxDb parity

Compared against a TxDb built by `txdbmaker` **from the same Ensembl GTF**, so
that any difference is ours rather than the source's.

1. `exonsBy(by="tx")` returns each transcript's exons ordered by rank, verified
   on minus-strand transcripts where rank and coordinate order disagree.
2. `intronsByTranscript()`, `fiveUTRsByTranscript()` and
   `threeUTRsByTranscript()` reproduce. A transcript with no CDS yields no
   UTRs rather than empty ones.
3. `cds()` and `cdsBy(by="tx")` reproduce, including `cds_phase`.
4. `transcriptLengths(with.cds_len=TRUE, with.utr5_len=TRUE,
   with.utr3_len=TRUE)` reproduces.
5. `seqinfo()` carries sequence lengths and circularity, so `promoters()`
   clamps correctly at chromosome ends.
6. A range-overlap query returns the same features as the equivalent
   `subsetByOverlaps` against the TxDb.

## F. AnnotationHub parity

1. An AnnotationHub record is representable with all of its metadata fields —
   title, dataprovider, species, taxonomyid, genome, description,
   coordinate_1_based, maintainer, rdatadateadded, preparerclass, tags,
   rdatapath, sourceurl, sourcetype, rdataclass — plus its accession.
2. Free-text search across that metadata returns the records `query()` would.
3. A record resolves to a fetchable URI for the underlying object, which is
   referenced and not ingested.
4. Coverage is reported honestly as a fraction of the **117,671** records the
   hub actually holds, with the unrepresented classes named.

The hub publishes its entire catalog as a SQLite database at
`https://annotationhub.bioconductor.org/metadata/annotationhub.sqlite3`
(~131 MB), with ExperimentHub alongside it. Ingest reads that file directly —
there is no scraping and no API pagination. Note that the record count printed
in the AnnotationHub vignette is stale; the database is authoritative.

## G. Variant resources

1. Open Targets Platform, ClinVar and GWAS Catalog are queryable through the
   same catalog, each carrying its licence and required attribution in table
   metadata.
2. A cross-resource join is demonstrated that no combination of current
   Bioconductor packages can perform — for example ClinVar clinical
   significance joined to GWAS Catalog associations and Open Targets evidence
   for one gene, in a single query.
3. Currency is demonstrated against the packaged alternatives, which are
   frozen at dbSNP 155 (2021) for SNPlocs, dbSNP 137 for SIFT and dbSNP 131
   for PolyPhen, with no ClinVar package at all and `ensemblVEP` removed after
   Bioconductor 3.20.
4. Every ingested resource's redistribution terms are recorded, and a resource
   whose licence has not been positively established is not ingested.

---

# Milestones

## Milestone 1 — Live catalog, DuckDB surface

Goal:

> A real catalog, on real infrastructure, that a stranger can query.

The riskiest part of biocOnIce is not the ETL — that is incremental — it is
the path from object storage to a client's query. Warehouse on Cloudflare R2,
icegate in front of it, DuckDB as the query surface. Everything else waits
until that path is proven end to end.

### Acceptance

Sections A, B and C of [Acceptance Criteria](#acceptance-criteria): point-in-
time and retirement, self-describing tables, and access through icegate
including vended credentials, anonymous read, and browser DuckDB-WASM.

---

## Milestone 2 — The parity layer

An R package whose objects answer to the real Bioconductor generics, backed by
the catalog, so existing scripts run unchanged:

1. **OrgDb** — `select()`, `mapIds()`, `keys()` over identifier space,
   replacing `org.Hs.eg.db` and `org.Mm.eg.db`
2. **TxDb** — `genes()`, `exonsBy()`, `cdsBy()`, the UTR pair,
   `intronsByTranscript()`, `transcriptLengths()`, replacing
   `TxDb.Hsapiens.*` and `TxDb.Mmusculus.*`
3. **AnnotationHub** — the resource catalog, read from the hub's published
   SQLite database, with external objects referenced rather than ingested

Scoped to the accessors named in acceptance sections D, E and F — not the whole
of GenomicFeatures.

This is deliberately **after** Milestone 1. Parity is the loudest claim
biocOnIce makes, and making it before the delivery path works would be
building the argument before the thing it argues about.

---

## Milestone 3 — ExperimentHub integration

Add:

* GEO metadata
* SRA metadata
* BioProject/BioSample
* processed matrices

---

## Milestone 4 — AI-ready biological knowledge layer

Add:

* embeddings
* entity resolution
* knowledge graph exports
* semantic search

---

## Why this scope is powerful

The first milestone is deceptively small. If you successfully replace:

* `org.Hs.eg.db`
* `TxDb.Hsapiens`
* `AnnotationHub` metadata

you have already proven the architecture.

Everything else becomes incremental ETL.

And this aligns almost perfectly with IceGate: biocOnIce becomes the first flagship public catalog behind IceGate, with the same principles you have been developing for OmicIDX:

* immutable snapshots
* open formats
* provenance
* language neutrality
* cloud-native access
* reproducibility

I would actually make Milestone 1 the formal "proof of concept": **"Bioconductor annotation packages without packages."** That is a very crisp story.

