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

import duckdb
from pyiceberg.expressions import EqualTo

from . import merge
from .ncbi import DATA, _land, _manifest

URL = f"{DATA}gene2pubmed.gz"

# DuckDB column spec in file order, same contract as ncbi.COLUMNS: auto_detect
# off, names from the spec rather than the header.
COLUMNS = "{'taxon_id':'INTEGER','gene_id':'VARCHAR','pubmed_id':'VARCHAR'}"


def land_raw(cat, release, url=None):
    """Phase 1: stream gene2pubmed verbatim and whole into raw.ncbi__gene2pubmed."""
    n = _land(cat, release, "raw.ncbi__gene2pubmed", url or URL, COLUMNS)
    _manifest(cat, release, "ncbi_gene2pubmed", URL, n)
    return n


def transform(cat, release, taxon):
    """Phase 2: the gene-to-publication links for one species.

    The dump arrives sorted by tax_id upstream, so this filter prunes nearly
    every Parquet row group on min/max stats without the table being partitioned.
    """
    con = duckdb.connect()
    con.register("g2p", cat.load_table("raw.ncbi__gene2pubmed").scan(
        row_filter=EqualTo("taxon_id", taxon)).to_arrow())
    links = con.sql(f"""
        SELECT DISTINCT gene_id, {taxon}::INTEGER AS taxon_id, pubmed_id FROM g2p
    """).to_arrow_table()

    # Single writer to this table, so a taxon-only scope suffices: no other
    # ingest can retire these rows on alternating runs.
    return {"annotation.ncbi__gene_pubmed": merge.merge(
        cat, "annotation.ncbi__gene_pubmed", links, release, EqualTo("taxon_id", taxon))}


def ingest(cat, release, taxa, url=None):
    out = {"raw.ncbi__gene2pubmed": land_raw(cat, release, url)}
    for taxon in taxa:
        for k, v in transform(cat, release, taxon).items():
            out[f"{k} [{taxon}]"] = v
    return out
