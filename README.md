# biocOnIce

**Bioconductor annotation without the packages.** Gene, transcript and exon
annotation as Apache Iceberg tables, readable from R, Python, DuckDB or
anything else that speaks Iceberg — instead of a versioned R package per
organism per release.

[SPEC.md](SPEC.md) is the design source of truth, and
[the wayfinder map](https://github.com/seandavi/bioc-on-ice/issues/1) tracks the
work. [icegate](https://github.com/seandavi/icegate) is the gateway that will
front the catalog.

## Status

Ensembl 116 for human and mouse is live on Cloudflare R2:

| Table | Human | Mouse |
| --- | --- | --- |
| `raw.ensembl_gtf` | 11,248,794 | 8,417,898 |
| `annotation.gene` | 78,941 | 78,348 |
| `annotation.transcript` | 646,577 | 481,956 |
| `annotation.exon` | 5,087,789 | 3,763,037 |
| `annotation.identifier_mapping` | 43,458 | 77,797 |

Ingest takes under a minute per species. Not yet: the merge that makes
`first_seen` meaningful, provenance rows, sequence lengths, the icegate
deployment, and everything in Milestone 2.

## Query it

```sql
INSTALL iceberg; LOAD iceberg;
CREATE SECRET r2cat (TYPE ICEBERG, TOKEN getenv('CF_API_TOKEN'));
ATTACH '55bf7202fe14474e57a300f56a652f64_bioconice' AS bioc (
    TYPE ICEBERG,
    ENDPOINT 'https://catalog.cloudflarestorage.com/55bf7202fe14474e57a300f56a652f64/bioconice'
);
```

The TxDb query — a canonical transcript's exons in biological order, with
coding bounds:

```sql
SELECT e.rank, e.start, e.end, e.strand, e.cds_start, e.cds_end, e.cds_phase
FROM bioc.annotation.gene g
JOIN bioc.annotation.transcript t USING (gene_id, taxon_id)
JOIN bioc.annotation.exon e USING (transcript_id, taxon_id)
WHERE g.symbol = 'TP53' AND g.taxon_id = 9606 AND t.canonical
ORDER BY e.rank;
```

Order by `rank`, never by coordinate: on the minus strand the two disagree and
coordinate order reverses the transcript.

## Ingest it

```sh
uv run bioconice ingest-ensembl homo_sapiens --release 2026.08 --ensembl-release 116
uv run bioconice ingest-ensembl homo_sapiens --release 2026.08 --transform-only
```

`--release` is the biocOnIce release; `--ensembl-release` is the upstream
version. With no `BIOCONICE_URI` set this writes a local sqlite warehouse in
`./warehouse`, so nothing needs cloud credentials. Set `BIOCONICE_URI`,
`BIOCONICE_WAREHOUSE` and `BIOCONICE_TOKEN` to write to a REST catalog instead
— the ingest code is identical either way.

Ingest is ELT in two phases. **Land** writes the GTF verbatim into
`raw.ensembl_gtf`, attribute blob and all. **Transform** derives the annotation
tables from it, so reinterpreting a source — a parsing fix, an attribute nobody
needed before — is a re-run rather than a re-download. That is what
`--transform-only` does.

## Conventions

`gene_id` is the bare Ensembl stable id (`ENSG00000141510`) with the upstream
`version` as its own column, so a version bump updates a row rather than
creating one. Coordinates are 1-based and end-inclusive, following Ensembl and
GTF, **not** the 0-based half-open convention of BED and UCSC. UTRs are derived
from CDS bounds rather than stored, so a transcript with no CDS has no UTRs
instead of empty ones. Every column carries an Iceberg `doc`, which reaches R
and Python clients as Arrow field metadata.

A biocOnIce release is a point-in-time claim across the whole catalog,
expressed by the `first_seen` / `retired_in` columns rather than by Iceberg
time travel — see [SPEC.md](SPEC.md#versioning-model) for why.

## Develop

```sh
uv run pytest       # raw ingest -> transform -> query, on a fixture, offline
```
