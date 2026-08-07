"""Ensembl GTF -> Iceberg, in two phases.

`land_raw` writes the GTF verbatim into `raw.ensembl__gtf`. `transform` reads it
back out and derives the annotation tables from it. Keeping the two apart means
that changing how we *interpret* a GTF is a re-run of transform rather than a
re-download, and that the attributes nobody has needed yet are already here when
somebody does.

The phases want different write semantics, which is why they are separate
functions rather than one pipeline. An Ensembl release is immutable, so raw is
replaced wholesale per (taxon_id, ensembl_release) and is idempotent under
re-ingest. The derived tables have real keys and a maintained current state, so
they are the ones that carry valid_from/valid_to and merge.
"""

import re
import urllib.request
from datetime import datetime, timezone

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
    """Phase 1: the GTF, verbatim, into raw.ensembl__gtf."""
    info = info or species_info(ensembl_release, species)
    con = duckdb.connect()
    arrow = con.sql(f"""
        SELECT seqname, source, feature, "start", "end", score, strand, frame, attribute,
               {info['taxon_id']}::INTEGER AS taxon_id,
               '{ensembl_release}' AS ensembl_release,
               '{release}' AS landed_in
        FROM read_csv('{url or gtf_url(ensembl_release, species)}', sep='\t', header=false,
                      comment='#', auto_detect=false, columns={GTF_COLUMNS})
    """).to_arrow_table()
    n = _write(cat, "raw.ensembl__gtf", arrow,
               And(EqualTo("taxon_id", info["taxon_id"]),
                   EqualTo("ensembl_release", str(ensembl_release))))
    _manifest(cat, release, ensembl_release, url or gtf_url(ensembl_release, species), n)
    return info, n


def _manifest(cat, release, ensembl_release, url, rows):
    """Record what this release was built from — ADR-0007."""
    con = duckdb.connect()
    arrow = con.sql(f"""
        SELECT '{release}' AS release, 'ensembl' AS source,
               '{ensembl_release}' AS source_version,
               'release_number' AS version_method,
               '{datetime.now(timezone.utc).isoformat(timespec="seconds")}' AS retrieved_at,
               '{url}' AS url, NULL::VARCHAR AS checksum, {rows}::BIGINT AS row_count
    """).to_arrow_table()
    # A manifest row states what a completed ingest used; it is not versioned,
    # so it is replaced wholesale for its (release, source) rather than merged.
    _write(cat, "provenance.release", arrow,
           And(EqualTo("release", release), EqualTo("source", "ensembl")))


# Cross-source tables: a merge must only be able to retire rows it wrote, so the
# scope names the writer as well as the species. Without this, Ensembl and NCBI
# retire each other's cross-references on alternating ingests — silently, and
# forever, because the "current" view is never empty.
WRITER = {"reference.ensembl__genome": EqualTo("provider", "Ensembl"),
          "annotation.identifier_mapping": EqualTo("source", "Ensembl")}


def _scope(identifier, taxon):
    taxon_only = EqualTo("taxon_id", taxon)
    writer = WRITER.get(identifier)
    return And(taxon_only, writer) if writer else taxon_only


def transform(cat, release, info, ensembl_release):
    """Phase 2: derive the annotation tables from landed raw rows."""
    taxon = info["taxon_id"]
    raw = cat.load_table("raw.ensembl__gtf").scan(
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
        "reference.ensembl__genome": q(f"""
            SELECT '{info['accession']}' AS genome_id, {taxon}::INTEGER AS taxon_id,
                   'Ensembl' AS provider, '{info['assembly']}' AS assembly_name
        """),
        "annotation.ensembl__gene": q(f"""
            SELECT gene_id, {taxon}::INTEGER AS taxon_id, gene_version AS version,
                   gene_name AS symbol, gene_biotype AS gene_type, gene_source AS source
            FROM feat WHERE feature = 'gene'
        """),
        "annotation.ensembl__transcript": q(f"""
            SELECT transcript_id, {taxon}::INTEGER AS taxon_id, gene_id,
                   transcript_version AS version, transcript_biotype AS biotype, canonical
            FROM feat WHERE feature = 'transcript'
        """),
        # A CDS line carries the same transcript_id and exon_number as the exon it
        # lies in, which is what lets coding bounds and phase ride on the exon row.
        "annotation.ensembl__exon": q(f"""
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
    return {k: merge.merge(cat, k, a, release, _scope(k, taxon)) for k, a in out.items()}


def ingest(cat, release, species, ensembl_release):
    info, n = land_raw(cat, release, species, ensembl_release)
    return {"raw.ensembl__gtf": n, **transform(cat, release, info, ensembl_release)}
