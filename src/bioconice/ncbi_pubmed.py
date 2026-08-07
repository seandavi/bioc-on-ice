"""NCBI gene2pubmed -> Iceberg: which publications discuss which genes.

Same unversioned-source shape as ncbi.py: the dump is regenerated nightly, so
the version recorded in `provenance.release` is the retrieval date, with
`version_method = 'retrieval_date'`. Raw is landed **whole** — every organism,
~40M rows — and streamed in record batches because it does not fit in memory
as one Arrow table. Per-species scoping lives in `transform`.

Its own module rather than a fourth entry in ncbi.py, so the gene2* sources
can land and fail independently. Unlike ncbi.py it is the ONLY writer to its
annotation table, so the merge scope is taxon-only — the flip-flop trap that
forces identifier_mapping's scope to name the source does not arise here.
"""

from datetime import datetime, timezone

import duckdb
import pyarrow as pa
from pyiceberg.expressions import And, EqualTo

from . import merge, schemas
from .ensembl import _write

DATA = "https://ftp.ncbi.nlm.nih.gov/gene/DATA/"

# DuckDB column spec in file order, `auto_detect` off: a column NCBI inserts
# or reorders fails loudly instead of quietly shifting every value.
COLUMNS = "{'tax_id':'INTEGER','gene_id':'VARCHAR','pubmed_id':'VARCHAR'}"

BATCH = 1_000_000


def land_raw(cat, release, url=None):
    """Phase 1: gene2pubmed verbatim, whole and streaming — the ncbi._land shape.

    ponytail: same partial-landing exposure as ncbi._land — a crash between
    batches leaves a wrong row count until the next run repairs it.
    """
    identifier = "raw.ncbi_gene2pubmed"
    table = schemas.create(cat, identifier)
    arrow_schema = table.schema().as_arrow()
    con = duckdb.connect()
    reader = con.sql(f"""
        SELECT * RENAME (tax_id AS taxon_id), '{release}' AS landed_in
        FROM read_csv('{url or f"{DATA}gene2pubmed.gz"}', sep='\t', header=true,
                      auto_detect=false, columns={COLUMNS}, nullstr='-')
    """).to_arrow_reader(BATCH)

    n = 0
    for batch in reader:
        # Casting to the declared schema is the check: a null in any of the
        # three identifier fields fails here rather than landing quietly.
        arrow = pa.Table.from_batches([batch]).cast(arrow_schema)
        if n:
            table.append(arrow)
        else:
            table.overwrite(arrow)
        n += arrow.num_rows
    if not n:
        # Otherwise a bad URL silently leaves the previous landing in place and
        # reports success.
        raise SystemExit(f"{identifier}: {url or 'gene2pubmed'} yielded no rows")
    _manifest(cat, release, n)
    return n


def _manifest(cat, release, rows):
    """Record what this release was built from — ADR-0007."""
    now = datetime.now(timezone.utc)
    con = duckdb.connect()
    arrow = con.sql(f"""
        SELECT '{release}' AS release, 'ncbi_gene2pubmed' AS source,
               '{now.date()}' AS source_version,
               'retrieval_date' AS version_method,
               '{now.isoformat(timespec="seconds")}' AS retrieved_at,
               '{DATA}gene2pubmed.gz' AS url, NULL::VARCHAR AS checksum,
               {rows}::BIGINT AS row_count
    """).to_arrow_table()
    _write(cat, "provenance.release", arrow,
           And(EqualTo("release", release), EqualTo("source", "ncbi_gene2pubmed")))


def transform(cat, release, taxon):
    """Phase 2: the gene-to-publication links for one species.

    The dump arrives sorted by tax_id upstream, so this filter prunes nearly
    every Parquet row group on min/max stats without the table being partitioned.
    """
    con = duckdb.connect()
    con.register("g2p", cat.load_table("raw.ncbi_gene2pubmed").scan(
        row_filter=EqualTo("taxon_id", taxon)).to_arrow())
    links = con.sql(f"""
        SELECT DISTINCT gene_id, {taxon}::INTEGER AS taxon_id, pubmed_id FROM g2p
    """).to_arrow_table()

    # Single writer to this table, so a taxon-only scope suffices: no other
    # ingest can retire these rows on alternating runs.
    return {"annotation.gene_pubmed": merge.merge(
        cat, "annotation.gene_pubmed", links, release, EqualTo("taxon_id", taxon))}


def ingest(cat, release, taxa, url=None):
    out = {"raw.ncbi_gene2pubmed": land_raw(cat, release, url)}
    for taxon in taxa:
        for k, v in transform(cat, release, taxon).items():
            out[f"{k} [{taxon}]"] = v
    return out
