"""NCBI gene2go -> Iceberg: GO annotations per Entrez gene.

Same two phases and the same unversioned-source treatment as ncbi.py: gene2go
is regenerated nightly, so the version is the retrieval date, recorded in the
manifest under its own source key `ncbi_gene2go` — a separate row from
`ncbi_gene`, because the two ingests run independently and one overwriting the
other's manifest row would misreport what either was built from.

Raw is landed **whole** — every organism, ~48M rows — with per-taxon scoping in
`transform`, for the same reason as ncbi.py: raw must not be a function of what
we happen to derive. The file's quirks: `-` is the null string; `PubMed` is a
pipe-separated PMID list kept unsplit, splitting is interpretation; `Qualifier`
carries the GO relation (involved_in, located_in, NOT|contributes_to, ...) and
is itself pipe-separated where several apply.

The derived table is DIRECT annotations only. The GOALL-style closure over
ancestor terms needs the GO DAG, which is its own source and its own issue —
computing it here would bake one ontology snapshot invisibly into every row.
"""

import duckdb

from . import merge
from .ncbi import DATA, _derive, _land, _manifest, _where, tsv

URL = f"{DATA}gene2go.gz"

# DuckDB column spec in file order, same contract as ncbi.COLUMNS: auto_detect
# off, names from the spec rather than the header. Not registered in
# ncbi.COLUMNS, because ncbi.land_raw lands everything in that dict and
# gene2go ingests on its own schedule under its own manifest row.
COLUMNS = (
    "{'taxon_id':'INTEGER','gene_id':'VARCHAR','go_id':'VARCHAR','evidence':'VARCHAR',"
    "'qualifier':'VARCHAR','go_term':'VARCHAR','pubmed':'VARCHAR','category':'VARCHAR'}"
)


def land_raw(cat, release, url=None):
    """Phase 1: stream gene2go verbatim and whole into raw.ncbi__gene2go."""
    n = _land(cat, release, "raw.ncbi__gene2go", tsv(url or URL, COLUMNS))
    _manifest(cat, release, "ncbi_gene2go", URL, n)
    return n


def transform(cat, release, taxon=None):
    """Phase 2: direct GO annotations, for one species or (default) all.

    gene2go arrives sorted by tax_id upstream, so a single-taxon filter prunes
    on Parquet row-group min/max stats without the table being partitioned.
    """
    con = duckdb.connect()
    con.register("gene2go", cat.load_table("raw.ncbi__gene2go").scan(
        row_filter=_where(taxon)).to_arrow())

    # evidence and qualifier are part of the business key, and a NULL key never
    # joins to itself, which would make the same row retire and reappear on
    # every merge. NCBI's '-' therefore becomes '' here, not NULL — documented
    # on the columns. PubMed is dropped, not exploded: it is an attribute of
    # the citation, still whole in raw for whoever needs it. DISTINCT because
    # rows identical but for PubMed collapse once it is gone.
    go = con.sql("""
        SELECT DISTINCT gene_id, taxon_id, go_id,
               COALESCE(evidence, '') AS evidence,
               COALESCE(qualifier, '') AS qualifier,
               go_term, category
        FROM gene2go
    """).to_arrow_table()

    # gene_go has a single writer — this module — so the merge scope is the
    # taxon alone; no source column in the key, unlike identifier_mapping.
    return {"annotation.ncbi__gene_go": merge.merge(
        cat, "annotation.ncbi__gene_go", go, release, _where(taxon))}


def ingest(cat, release, taxa=None):
    return {"raw.ncbi__gene2go": land_raw(cat, release),
            **_derive(transform, cat, release, taxa)}
