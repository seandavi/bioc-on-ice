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

from datetime import datetime, timezone

import duckdb
import pyarrow as pa
from pyiceberg.expressions import And, EqualTo

from . import merge, schemas
from .ensembl import _write
from .ncbi import BATCH, DATA

# DuckDB column spec in file order, auto_detect off, same contract as
# ncbi.COLUMNS: a column NCBI inserts or reorders fails loudly instead of
# quietly shifting every value one to the left. Not registered in ncbi.COLUMNS,
# because ncbi.land_raw lands everything in that dict and gene2go ingests on
# its own schedule under its own manifest row.
COLUMNS = (
    "{'tax_id':'INTEGER','gene_id':'VARCHAR','go_id':'VARCHAR','evidence':'VARCHAR',"
    "'qualifier':'VARCHAR','go_term':'VARCHAR','pubmed':'VARCHAR','category':'VARCHAR'}"
)


def land_raw(cat, release, url=None):
    """Phase 1: stream gene2go verbatim and whole into raw.ncbi__gene2go.

    Replace-with-the-first-batch then append, exactly as ncbi._land: the file
    does not fit in memory as one Arrow table.

    ponytail: a crash between batches leaves the table partly landed; re-running
    repairs it, same exposure and same upgrade path as ncbi._land.
    """
    identifier = "raw.ncbi__gene2go"
    table = schemas.create(cat, identifier)
    arrow_schema = table.schema().as_arrow()
    con = duckdb.connect()
    reader = con.sql(f"""
        SELECT * RENAME (tax_id AS taxon_id), '{release}' AS landed_in
        FROM read_csv('{url or f"{DATA}gene2go.gz"}', sep='\t', header=true,
                      auto_detect=false, columns={COLUMNS}, nullstr='-')
    """).to_arrow_reader(BATCH)

    n = 0
    for batch in reader:
        # Casting to the declared schema is the check: a null gene or GO id
        # fails here rather than landing quietly.
        arrow = pa.Table.from_batches([batch]).cast(arrow_schema)
        if n:
            table.append(arrow)
        else:
            table.overwrite(arrow)
        n += arrow.num_rows
    if not n:
        # Otherwise a bad URL silently leaves the previous landing in place and
        # reports success.
        raise SystemExit(f"{identifier}: {url or 'gene2go'} yielded no rows")
    _manifest(cat, release, n)
    return n


def _manifest(cat, release, rows):
    """Record what this release was built from — ADR-0007."""
    now = datetime.now(timezone.utc)
    con = duckdb.connect()
    arrow = con.sql(f"""
        SELECT '{release}' AS release, 'ncbi_gene2go' AS source,
               '{now.date()}' AS source_version,
               'retrieval_date' AS version_method,
               '{now.isoformat(timespec="seconds")}' AS retrieved_at,
               '{DATA}gene2go.gz' AS url, NULL::VARCHAR AS checksum,
               {rows}::BIGINT AS row_count
    """).to_arrow_table()
    _write(cat, "provenance.release", arrow,
           And(EqualTo("release", release), EqualTo("source", "ncbi_gene2go")))


def transform(cat, release, taxon):
    """Phase 2: direct GO annotations for one species.

    gene2go arrives sorted by tax_id upstream, so the filter prunes on Parquet
    row-group min/max stats without the table being partitioned.
    """
    con = duckdb.connect()
    con.register("gene2go", cat.load_table("raw.ncbi__gene2go").scan(
        row_filter=EqualTo("taxon_id", taxon)).to_arrow())

    # evidence and qualifier are part of the business key, and a NULL key never
    # joins to itself, which would make the same row retire and reappear on
    # every merge. NCBI's '-' therefore becomes '' here, not NULL — documented
    # on the columns. PubMed is dropped, not exploded: it is an attribute of
    # the citation, still whole in raw for whoever needs it. DISTINCT because
    # rows identical but for PubMed collapse once it is gone.
    go = con.sql(f"""
        SELECT DISTINCT gene_id, {taxon}::INTEGER AS taxon_id, go_id,
               COALESCE(evidence, '') AS evidence,
               COALESCE(qualifier, '') AS qualifier,
               go_term, category
        FROM gene2go
    """).to_arrow_table()

    # gene_go has a single writer — this module — so the merge scope is the
    # taxon alone; no source column in the key, unlike identifier_mapping.
    return {"annotation.ncbi__gene_go": merge.merge(
        cat, "annotation.ncbi__gene_go", go, release, EqualTo("taxon_id", taxon))}


def ingest(cat, release, taxa):
    out = {"raw.ncbi__gene2go": land_raw(cat, release)}
    for taxon in taxa:
        for k, v in transform(cat, release, taxon).items():
            out[f"{k} [{taxon}]"] = v
    return out
