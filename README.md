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

Ingest takes under a minute per species. The genome-feature tables
(`annotation.gene`, `annotation.transcript`, `annotation.exon`,
`reference.genome`) are stacked multi-writer tables: every row names its
asserting provider in a `source` column — `'ENSEMBL'` today — so RefSeq or
GENCODE later land as new rows, not new tables.

NCBI Gene adds the attributes a GTF cannot carry — descriptions, aliases,
cytogenetic bands, Entrez cross-references. Its raw tables are landed **whole**,
every organism NCBI knows, because raw is an audit trail and a resource in its
own right rather than a function of what we currently derive:

| Table | Rows |
| --- | --- |
| `raw.ncbi_gene_info` | 71,471,729 |
| `raw.ncbi_gene_history` | 27,079,420 |
| `raw.ncbi_gene2ensembl` | 17,859,274 |

| Derived (per species) | Human | Mouse |
| --- | --- | --- |
| `annotation.ncbi_gene` | 193,809 | 112,257 |
| `annotation.identifier_mapping` (NCBI-asserted) | 455,272 | 391,608 |

Landing all three dumps and deriving both species takes 2m56s into a local
warehouse and 9m52s into R2, peaking at 2.7 GB of memory — the files are streamed
in record batches rather than built as one Arrow table, so ingest does not need a
large machine.

Both sources coexist in `annotation.identifier_mapping` without retiring each
other: 121,255 Ensembl-asserted rows and 846,880 NCBI-asserted rows, live
simultaneously. That is the merge scope naming its writer, verified at scale.

**BugSigDB** is landed but not yet transformed: `raw.bugsigdb_full_dump`, 7,425
curated microbial signatures at release tag `v1.3.1`, CC BY 4.0. Landed from a
tag rather than the hourly `devel` export, so it is immutable and citable — each
release carries a Zenodo DOI. Turning the two nested member-list columns into a
signature↔taxon table is the next step and wants NCBI Taxonomy (#18) first.

The release manifest now carries both version axes at once, which is the point of
[ADR-0007](docs/adr/0007-release-manifest.md): `bugsigdb` resolves to `v1.3.1` by
`release_number`, `ncbi_gene` to a date by `retrieval_date`.

Serving is live behind icegate at
`https://icegate-bioconice.seandavi.workers.dev` with **anonymous public
read** — no token needed. Vended credentials are contained to this catalog's
bucket, read-only, by a bucket-scoped backend token
([ADR-0011](docs/adr/0011-bucket-scoped-vending-tokens.md)). Write access
remains key-only.

Not yet: the `ensembl` row of the release manifest (see below), sequence lengths,
and everything in Milestone 2.

> **Known gap.** `provenance.release` currently holds only the `ncbi_gene` row.
> The manifest is written by each source's `land_raw`, and R2's Ensembl data was
> landed before [ADR-0007](docs/adr/0007-release-manifest.md) existed, so release
> 2026.08 is not yet reproducible from the manifest alone. Re-landing the GTFs
> fixes it; making the manifest writable without a re-land would be better.

## Query it

No account, no token — anonymous read is public:

```sql
INSTALL iceberg; LOAD iceberg;
ATTACH 'bioconice' AS bioc (
    TYPE ICEBERG,
    ENDPOINT 'https://icegate-bioconice.seandavi.workers.dev',
    AUTHORIZATION_TYPE 'none'
);
```

The TxDb query — a canonical transcript's exons in biological order, with
coding bounds:

```sql
SELECT e.rank, e.start, e.end, e.strand, e.cds_start, e.cds_end, e.cds_phase
FROM bioc.annotation.gene g
JOIN bioc.annotation.transcript t USING (gene_id, taxon_id, source)
JOIN bioc.annotation.exon e USING (transcript_id, taxon_id, source)
WHERE g.symbol = 'TP53' AND g.taxon_id = 9606 AND g.source = 'ENSEMBL'
  AND t.canonical
ORDER BY e.rank;
```

Order by `rank`, never by coordinate: on the minus strand the two disagree and
coordinate order reverses the transcript.

## Ingest it

```sh
uv run bioconice ingest-ensembl homo_sapiens --release 2026.08 --ensembl-release 116
uv run bioconice ingest-ensembl homo_sapiens --release 2026.08 --transform-only
uv run bioconice ingest-ncbi --release 2026.08 --taxa 9606,10090
```

`--taxa` says which species to *derive* annotation for; the NCBI dumps are
always landed whole, so adding a species later is a `transform` and needs no
re-download.

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
and Python clients as Arrow field metadata. Plain-named tables in a derived
namespace (`annotation.gene`, `annotation.identifier_mapping`) are
multi-writer: rows are discriminated — and merge-scoped — by `source`, the
asserting provider; single-source views carry a `source__` prefix
(`annotation.ncbi__gene`).

A biocOnIce release is a point-in-time claim across the whole catalog,
expressed by the `first_seen` / `retired_in` columns rather than by Iceberg
time travel — see [SPEC.md](SPEC.md#versioning-model) for why.

## Develop

```sh
uv run pytest       # raw ingest -> transform -> query, on a fixture, offline
```
