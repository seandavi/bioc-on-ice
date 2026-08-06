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
md5
uri
format
```

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
first_seen
retired_in
```

`rank` is what lets a transcript's exons be reassembled in biological order
rather than coordinate order, which matters on the minus strand.

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

# Milestones

## Milestone 1 — OrgDb/TxDb replacement

Goal:

> Demonstrate that major annotation packages can be recreated.

Resources:

### Organisms

Start:

* Homo sapiens
* Mus musculus

### Tables

Implement:

* taxon
* genome
* gene
* transcript
* exon
* identifier_mapping
* ontology
* annotation

Sources:

* NCBI Gene
* Ensembl
* GO

Acceptance criteria:

* reproduce common OrgDb queries
* reproduce TxDb range queries
* accessible through R and Python
* Iceberg snapshots reproducible

---

## Milestone 2 — AnnotationHub catalog

Add:

* resource metadata
* providers
* licenses
* checksums
* external object references

Acceptance:

Existing AnnotationHub resources can be represented.

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

