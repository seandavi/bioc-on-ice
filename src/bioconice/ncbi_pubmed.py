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

from . import merge
from .ncbi import DATA, _derive, _land, _where, tsv

URL = f"{DATA}gene2pubmed.gz"

# DuckDB column spec in file order, same contract as ncbi.COLUMNS: auto_detect
# off, names from the spec rather than the header.
COLUMNS = "{'taxon_id':'INTEGER','gene_id':'VARCHAR','pubmed_id':'VARCHAR'}"


def land_raw(cat, release, url=None):
    """Phase 1: stream gene2pubmed verbatim and whole into raw.ncbi__gene2pubmed."""
    facts = merge.reading(release, "ncbi_gene", "gene2pubmed", url or URL)
    n = _land(cat, release, "raw.ncbi__gene2pubmed", tsv(url or URL, COLUMNS))
    merge.manifest(cat, release, "ncbi_gene", "gene2pubmed", URL, n, **facts)
    return n


def transform(cat, release, taxon=None):
    """Phase 2: the gene-to-publication links, for one species or (default) all.

    The dump arrives sorted by tax_id upstream, so a single-taxon filter prunes
    nearly every Parquet row group on min/max stats without partitioning.
    """
    con = duckdb.connect()
    con.register("g2p", cat.load_table("raw.ncbi__gene2pubmed").scan(
        row_filter=_where(taxon)).to_arrow())
    links = con.sql("SELECT DISTINCT gene_id, taxon_id, pubmed_id FROM g2p").to_arrow_table()

    # Single writer to this table, so a taxon-only scope suffices: no other
    # ingest can retire these rows on alternating runs.
    return {"annotation.ncbi__gene_pubmed": merge.merge(
        cat, "annotation.ncbi__gene_pubmed", links, release, _where(taxon))}


def ingest(cat, release, taxa=None, url=None):
    return {"raw.ncbi__gene2pubmed": land_raw(cat, release, url),
            **_derive(transform, cat, release, taxa)}
