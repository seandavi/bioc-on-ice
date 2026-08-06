"""Ensembl GTF -> Iceberg tables.

One pass over the GTF with DuckDB pulls out the attributes we care about; each
target table is then a plain SELECT over that. Ingest is per-species and
idempotent: rows for the species' taxon are replaced, other species untouched.
"""

import re
import urllib.request

import duckdb
from pyiceberg.expressions import EqualTo

FTP = "https://ftp.ensembl.org/pub/release-{release}"

# GTF is a headerless 9-column TSV with '#!' comment lines.
GTF_COLUMNS = (
    "{'seqname':'VARCHAR','source':'VARCHAR','feature':'VARCHAR',"
    "'start':'BIGINT','end':'BIGINT','score':'VARCHAR','strand':'VARCHAR',"
    "'frame':'VARCHAR','attribute':'VARCHAR'}"
)

# Ensembl keeps the id and its version in separate attributes; the versioned
# form is the key that is unique across releases, the bare one is the stable id.
def _versioned(name):
    return f"{name}_stable_id || coalesce('.' || nullif({name}_version, ''), '')"


def _attr(key):
    return f"""regexp_extract(attribute, '{key} "([^"]*)"', 1)"""


def species_info(release, species):
    """taxonomy_id / assembly / accession for a species, from Ensembl itself."""
    url = f"{FTP.format(release=release)}/species_EnsemblVertebrates.txt"
    with urllib.request.urlopen(url) as r:
        for line in r.read().decode().splitlines():
            f = line.split("\t")
            if len(f) > 5 and f[1] == species:
                return {"taxon_id": int(f[3]), "assembly": f[4], "accession": f[5]}
    raise SystemExit(f"{species} not found in Ensembl release {release} vertebrates")


def gtf_url(release, species):
    """The primary GTF for a species (not .chr, .abinitio or the patch build)."""
    listing = f"{FTP.format(release=release)}/gtf/{species}/"
    with urllib.request.urlopen(listing) as r:
        m = re.search(rf'>([^<>"]+\.{release}\.gtf\.gz)<', r.read().decode())
    if not m:
        raise SystemExit(f"no GTF for {species} in Ensembl release {release}")
    return listing + m.group(1)


def parse(con, url):
    """Materialize the attributes we need, once, so the GTF is read once."""
    con.execute(f"""
        CREATE OR REPLACE TABLE gtf AS SELECT
            feature, seqname, "start", "end", strand,
            {_attr('gene_id')} AS gene_stable_id,
            {_attr('gene_version')} AS gene_version,
            {_attr('gene_name')} AS gene_name,
            {_attr('gene_biotype')} AS gene_biotype,
            {_attr('gene_source')} AS gene_source,
            {_attr('transcript_id')} AS transcript_stable_id,
            {_attr('transcript_version')} AS transcript_version,
            {_attr('transcript_biotype')} AS transcript_biotype,
            {_attr('exon_id')} AS exon_stable_id,
            {_attr('exon_version')} AS exon_version,
            attribute LIKE '%tag "Ensembl_canonical"%' AS canonical
        FROM read_csv('{url}', sep='\t', header=false, comment='#',
                      auto_detect=false, columns={GTF_COLUMNS})
    """)


def tables(con, release, info):
    """{table identifier: arrow table} for one species of one Ensembl release."""
    taxon, rel = info["taxon_id"], str(release)
    gene_id, tx_id, exon_id = _versioned("gene"), _versioned("transcript"), _versioned("exon")
    q = lambda sql: con.sql(sql).to_arrow_table()
    return {
        "reference.genome": q(f"""
            SELECT '{info["accession"]}' AS genome_id, {taxon} AS taxon_id,
                   'Ensembl' AS provider, '{info["assembly"]}' AS assembly_name,
                   '{info["accession"]}' AS assembly_accession, '{rel}' AS release,
                   NULL::VARCHAR AS checksum
        """),
        "annotation.gene": q(f"""
            SELECT {gene_id} AS gene_id, {taxon} AS taxon_id,
                   gene_stable_id AS stable_id, nullif(gene_name, '') AS symbol,
                   NULL::VARCHAR AS description, nullif(gene_biotype, '') AS gene_type,
                   nullif(gene_source, '') AS source, '{rel}' AS release
            FROM gtf WHERE feature = 'gene'
        """),
        "annotation.transcript": q(f"""
            SELECT {tx_id} AS transcript_id, {gene_id} AS gene_id, {taxon} AS taxon_id,
                   transcript_stable_id AS stable_id,
                   nullif(transcript_biotype, '') AS biotype, canonical
            FROM gtf WHERE feature = 'transcript'
        """),
        "annotation.exon": q(f"""
            SELECT {exon_id} AS exon_id, {tx_id} AS transcript_id, {taxon} AS taxon_id,
                   seqname AS sequence_name, "start", "end", strand
            FROM gtf WHERE feature = 'exon'
        """),
        # ponytail: Ensembl gives us SYMBOL only; ENTREZ needs NCBI gene2ensembl.
        "annotation.identifier_mapping": q(f"""
            SELECT 'ENSEMBL' AS source_namespace, gene_stable_id AS source_id,
                   'SYMBOL' AS target_namespace, gene_name AS target_id,
                   {taxon} AS taxon_id, 'Ensembl' AS source, '{rel}' AS release,
                   NULL::DOUBLE AS confidence
            FROM gtf WHERE feature = 'gene' AND gene_name <> ''
        """),
    }


def write(cat, identifier, arrow, taxon_id):
    cat.create_namespace_if_not_exists(identifier.split(".")[0])
    table = cat.create_table_if_not_exists(identifier, schema=arrow.schema)
    table.overwrite(arrow.cast(table.schema().as_arrow()),
                    overwrite_filter=EqualTo("taxon_id", taxon_id))
    return table


def ingest(cat, release, species):
    info = species_info(release, species)
    con = duckdb.connect()
    parse(con, gtf_url(release, species))
    out = {}
    for identifier, arrow in tables(con, release, info).items():
        write(cat, identifier, arrow, info["taxon_id"])
        out[identifier] = arrow.num_rows
    return out
