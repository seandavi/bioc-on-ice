# biocOnIce

**Bioconductor annotation without the packages.** Gene, transcript and exon
annotation as Apache Iceberg tables, readable from R, Python, DuckDB or
anything else that speaks Iceberg — instead of a versioned R package per
organism per release.

[SPEC.md](SPEC.md) is the design source of truth. This repo is the ingest side;
[icegate](https://github.com/seandavi/icegate) is the gateway that serves the
resulting catalog.

## Status

Milestone 1, partially: Ensembl GTF → `reference.genome`, `annotation.gene`,
`annotation.transcript`, `annotation.exon`, `annotation.identifier_mapping`.
Human release 116 is 78,941 genes / 646,577 transcripts / 5,087,789 exons,
ingested in ~20 s into a 35 MB warehouse.

Not yet: NCBI (Entrez ids, taxonomy), GO, and the resource/experiment/
provenance namespaces.

## Use it

```sh
uv run bioconice ingest-ensembl homo_sapiens --release 116
uv run bioconice tables
```

By default this writes a local sqlite-backed warehouse in `./warehouse`, so
nothing needs cloud credentials. Point it at a REST catalog instead with
`BIOCONICE_URI` (plus `BIOCONICE_WAREHOUSE` for the catalog name and
`BIOCONICE_TOKEN` for the key) — the ingest code doesn't change.

The OrgDb query — symbol to identifier:

```python
from bioconice import catalog

catalog().load_table("annotation.gene").scan(
    row_filter="symbol = 'TP53'").to_arrow()
# ENSG00000141510.21 | 9606 | TP53 | protein_coding | 116
```

The TxDb query — the exons of a gene's canonical transcript:

```python
cat = catalog()
tx = cat.load_table("annotation.transcript").scan(
    row_filter="gene_id = 'ENSG00000141510.21' and canonical = true").to_arrow()
cat.load_table("annotation.exon").scan(
    row_filter=f"transcript_id = '{tx['transcript_id'][0]}'").to_arrow()
# 11 exons on chr17, minus strand
```

## Conventions

`gene_id` is the versioned Ensembl id (`ENSG00000141510.21`), unique across
releases; `stable_id` is the bare one. Ingest is idempotent per species: it
replaces that taxon's rows and leaves other species alone. A biocOnIce release
is an Iceberg snapshot, so older releases stay reachable by time travel rather
than by a `release` filter.

## Develop

```sh
uv run pytest       # GTF -> Iceberg -> query round trip on a fixture
```
