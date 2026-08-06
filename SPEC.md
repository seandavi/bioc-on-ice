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
gene_id
taxon_id
stable_id
symbol
description
gene_type
source
release
```

---

## transcript

```sql
transcript
-----------
transcript_id
gene_id
taxon_id
stable_id
biotype
canonical
```

---

## exon

```sql
exon
----
exon_id
transcript_id
taxon_id
sequence_name
start
end
strand
```

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
release
confidence
```

`taxon_id` is carried on `transcript`, `exon` and `identifier_mapping` (beyond
what the entity itself needs) so that ingest is per-species: a species'
rows can be replaced with an Iceberg overwrite filtered on `taxon_id` without
disturbing any other species in the same table.

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

Every release corresponds to:

```
biocOnIce release
        |
        |
 Iceberg snapshot
```

Example:

```
biocOnIce 2026.10
  |
  +-- Ensembl 114
  +-- NCBI taxonomy 2026-08
  +-- GO 2026-09
```

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

