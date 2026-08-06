"""NCBI Gene -> Iceberg, in the same two phases as Ensembl.

NCBI is the *unversioned* case, and it is here partly to prove the model
handles it. There is no release number: the dumps are regenerated nightly, so
`Last-Modified` and any published checksum change daily on identical content.
The version recorded in `provenance.release` is therefore the retrieval date,
with `version_method = 'retrieval_date'` saying plainly how we know it — never
a fabricated release number.

It is also the **second writer** to `annotation.identifier_mapping`, which
Ensembl already writes. That is why its merge scope names the source: with a
taxon-only scope each ingest would retire the other's rows on alternating runs,
silently and forever. Data Vault calls that the flip-flop effect.
"""

from datetime import datetime, timezone

import duckdb
from pyiceberg.expressions import And, EqualTo, In

from . import merge, schemas
from .ensembl import _write

GENE2ENSEMBL = "https://ftp.ncbi.nlm.nih.gov/gene/DATA/gene2ensembl.gz"

COLUMNS = (
    "{'tax_id':'INTEGER','gene_id':'VARCHAR','ensembl_gene_id':'VARCHAR',"
    "'rna_accession':'VARCHAR','ensembl_rna_id':'VARCHAR',"
    "'protein_accession':'VARCHAR','ensembl_protein_id':'VARCHAR'}"
)


def land_raw(cat, release, taxa, url=None):
    """Phase 1: gene2ensembl, verbatim, for the taxa we care about.

    The file covers every organism NCBI knows, so it is filtered on the way in.
    That is a departure from landing a source whole, and a deliberate one: the
    unfiltered file is 261 MB of mostly-irrelevant rows, and the filter is on a
    column that is part of every downstream key.
    """
    con = duckdb.connect()
    arrow = con.sql(f"""
        SELECT tax_id AS taxon_id, gene_id, ensembl_gene_id, rna_accession,
               ensembl_rna_id, protein_accession, ensembl_protein_id,
               '{release}' AS landed_in
        FROM read_csv('{url or GENE2ENSEMBL}', sep='\t', header=true,
                      auto_detect=false, columns={COLUMNS}, nullstr='-')
        WHERE tax_id IN ({', '.join(str(t) for t in taxa)})
    """).to_arrow_table()
    n = _write(cat, "raw.ncbi_gene2ensembl", arrow, In("taxon_id", taxa))

    manifest = con.sql(f"""
        SELECT '{release}' AS release, 'ncbi_gene' AS source,
               '{datetime.now(timezone.utc).date()}' AS source_version,
               'retrieval_date' AS version_method,
               '{datetime.now(timezone.utc).isoformat(timespec="seconds")}' AS retrieved_at,
               '{url or GENE2ENSEMBL}' AS url, NULL::VARCHAR AS checksum,
               {n}::BIGINT AS row_count
    """).to_arrow_table()
    _write(cat, "provenance.release", manifest,
           And(EqualTo("release", release), EqualTo("source", "ncbi_gene")))
    return n


def transform(cat, release, taxon):
    """Phase 2: Entrez cross-references, scoped so we cannot retire Ensembl's rows."""
    raw = cat.load_table("raw.ncbi_gene2ensembl").scan(
        row_filter=EqualTo("taxon_id", taxon)).to_arrow()
    con = duckdb.connect()
    con.register("raw", raw)

    incoming = con.sql(f"""
        SELECT DISTINCT
               'ENSEMBL' AS source_namespace, ensembl_gene_id AS source_id,
               'ENTREZ' AS target_namespace, gene_id AS target_id,
               {taxon}::INTEGER AS taxon_id, 'NCBI' AS source,
               NULL::DOUBLE AS confidence
        FROM raw
        WHERE ensembl_gene_id IS NOT NULL AND gene_id IS NOT NULL
    """).to_arrow_table()

    # The scope names the source. Without it this merge would retire every
    # ENSEMBL->SYMBOL row Ensembl wrote for this taxon.
    scope = And(EqualTo("taxon_id", taxon), EqualTo("source", "NCBI"))
    return {"annotation.identifier_mapping":
            merge.merge(cat, "annotation.identifier_mapping", incoming, release, scope)}


def ingest(cat, release, taxa):
    n = land_raw(cat, release, taxa)
    out = {"raw.ncbi_gene2ensembl": n}
    for taxon in taxa:
        for k, v in transform(cat, release, taxon).items():
            out[f"{k} [{taxon}]"] = v
    return out
