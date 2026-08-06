"""Ensembl GTF -> Iceberg, in two phases.

`land_raw` writes the GTF verbatim into `raw.ensembl_gtf`. `transform` reads it
back out and derives the annotation tables from it. Keeping the two apart means
that changing how we *interpret* a GTF is a re-run of transform rather than a
re-download, and that the attributes nobody has needed yet are already here when
somebody does.

The phases want different write semantics, which is why they are separate
functions rather than one pipeline. An Ensembl release is immutable, so raw is
replaced wholesale per (taxon_id, ensembl_release) and is idempotent under
re-ingest. The derived tables have real keys and a maintained current state, so
they are the ones that carry first_seen/retired_in and merge.
"""

import re
import urllib.request

import duckdb
from pyiceberg.expressions import And, EqualTo

from . import merge, schemas

FTP = "https://ftp.ensembl.org/pub/release-{release}"

GTF_COLUMNS = (
    "{'seqname':'VARCHAR','source':'VARCHAR','feature':'VARCHAR',"
    "'start':'BIGINT','end':'BIGINT','score':'VARCHAR','strand':'VARCHAR',"
    "'frame':'VARCHAR','attribute':'VARCHAR'}"
)


def _attr(key):
    return f"""nullif(regexp_extract(attribute, '{key} "([^"]*)"', 1), '')"""


def species_info(ensembl_release, species):
    """taxonomy_id / assembly / accession for a species, from Ensembl itself."""
    url = f"{FTP.format(release=ensembl_release)}/species_EnsemblVertebrates.txt"
    with urllib.request.urlopen(url) as r:
        for line in r.read().decode().splitlines():
            f = line.split("\t")
            if len(f) > 5 and f[1] == species:
                return {"taxon_id": int(f[3]), "assembly": f[4], "accession": f[5]}
    raise SystemExit(f"{species} not in Ensembl release {ensembl_release} vertebrates")


def gtf_url(ensembl_release, species):
    """The primary GTF for a species (not .chr, .abinitio or the patch build)."""
    listing = f"{FTP.format(release=ensembl_release)}/gtf/{species}/"
    with urllib.request.urlopen(listing) as r:
        m = re.search(rf'>([^<>"]+\.{ensembl_release}\.gtf\.gz)<', r.read().decode())
    if not m:
        raise SystemExit(f"no GTF for {species} in Ensembl release {ensembl_release}")
    return listing + m.group(1)


def _write(cat, identifier, arrow, overwrite_filter):
    table = schemas.create(cat, identifier)
    # Casting to the declared schema is the check: a column we failed to produce,
    # or a null in an identifier field, fails here rather than landing quietly.
    table.overwrite(arrow.cast(table.schema().as_arrow()), overwrite_filter=overwrite_filter)
    return arrow.num_rows


def land_raw(cat, release, species, ensembl_release, url=None, info=None):
    """Phase 1: the GTF, verbatim, into raw.ensembl_gtf."""
    info = info or species_info(ensembl_release, species)
    con = duckdb.connect()
    arrow = con.sql(f"""
        SELECT seqname, source, feature, "start", "end", score, strand, frame, attribute,
               {info['taxon_id']}::INTEGER AS taxon_id,
               '{ensembl_release}' AS ensembl_release,
               '{release}' AS first_seen
        FROM read_csv('{url or gtf_url(ensembl_release, species)}', sep='\t', header=false,
                      comment='#', auto_detect=false, columns={GTF_COLUMNS})
    """).to_arrow_table()
    n = _write(cat, "raw.ensembl_gtf", arrow,
               And(EqualTo("taxon_id", info["taxon_id"]),
                   EqualTo("ensembl_release", str(ensembl_release))))
    return info, n


def transform(cat, release, info, ensembl_release):
    """Phase 2: derive the annotation tables from landed raw rows."""
    taxon = info["taxon_id"]
    raw = cat.load_table("raw.ensembl_gtf").scan(
        row_filter=And(EqualTo("taxon_id", taxon),
                       EqualTo("ensembl_release", str(ensembl_release)))).to_arrow()

    con = duckdb.connect()
    con.register("raw", raw)
    # ponytail: attributes are extracted per transform run rather than stored
    # parsed. Fine at one GTF; if this becomes the bottleneck, materialize the
    # extraction as its own derived table instead of widening raw.
    con.execute(f"""
        CREATE OR REPLACE TABLE feat AS SELECT
            feature, seqname, "start", "end", strand, frame,
            {_attr('gene_id')} AS gene_id,
            {_attr('gene_version')} AS gene_version,
            {_attr('gene_name')} AS gene_name,
            {_attr('gene_biotype')} AS gene_biotype,
            {_attr('gene_source')} AS gene_source,
            {_attr('transcript_id')} AS transcript_id,
            {_attr('transcript_version')} AS transcript_version,
            {_attr('transcript_biotype')} AS transcript_biotype,
            {_attr('exon_id')} AS exon_id,
            {_attr('exon_number')} AS exon_number,
            attribute LIKE '%tag "Ensembl_canonical"%' AS canonical
        FROM raw
    """)

    q = lambda sql: con.sql(sql).to_arrow_table()
    out = {
        "reference.genome": q(f"""
            SELECT '{info['accession']}' AS genome_id, {taxon}::INTEGER AS taxon_id,
                   'Ensembl' AS provider, '{info['assembly']}' AS assembly_name
        """),
        "annotation.gene": q(f"""
            SELECT gene_id, {taxon}::INTEGER AS taxon_id, gene_version AS version,
                   gene_name AS symbol, gene_biotype AS gene_type, gene_source AS source
            FROM feat WHERE feature = 'gene'
        """),
        "annotation.transcript": q(f"""
            SELECT transcript_id, {taxon}::INTEGER AS taxon_id, gene_id,
                   transcript_version AS version, transcript_biotype AS biotype, canonical
            FROM feat WHERE feature = 'transcript'
        """),
        # A CDS line carries the same transcript_id and exon_number as the exon it
        # lies in, which is what lets coding bounds and phase ride on the exon row.
        "annotation.exon": q(f"""
            SELECT e.exon_id, e.transcript_id, {taxon}::INTEGER AS taxon_id,
                   e.seqname AS sequence_name, e."start", e."end", e.strand,
                   e.exon_number::INTEGER AS rank,
                   c."start" AS cds_start, c."end" AS cds_end,
                   try_cast(c.frame AS INTEGER) AS cds_phase
            FROM feat e
            LEFT JOIN feat c
              ON c.feature = 'CDS'
             AND c.transcript_id = e.transcript_id
             AND c.exon_number = e.exon_number
            WHERE e.feature = 'exon'
        """),
        # ponytail: Ensembl gives us SYMBOL only; ENTREZ needs NCBI gene2ensembl.
        "annotation.identifier_mapping": q(f"""
            SELECT 'ENSEMBL' AS source_namespace, gene_id AS source_id,
                   'SYMBOL' AS target_namespace, gene_name AS target_id,
                   {taxon}::INTEGER AS taxon_id, 'Ensembl' AS source,
                   NULL::DOUBLE AS confidence
            FROM feat WHERE feature = 'gene' AND gene_name IS NOT NULL
        """),
    }
    # ponytail: taxon-only scope is correct while Ensembl is the sole writer.
    # reference.genome and annotation.identifier_mapping are cross-source tables;
    # the moment a second source writes them, this must become
    # And(EqualTo("taxon_id", t), EqualTo(<source column>, "Ensembl")) or the two
    # writers will retire each other's rows on alternating ingests. Do not copy
    # this line into a new source module unchanged.
    scope = EqualTo("taxon_id", taxon)
    return {k: merge.merge(cat, k, a, release, scope) for k, a in out.items()}


def ingest(cat, release, species, ensembl_release):
    info, n = land_raw(cat, release, species, ensembl_release)
    return {"raw.ensembl_gtf": n, **transform(cat, release, info, ensembl_release)}
