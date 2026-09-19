"""Ensembl GTF -> Iceberg, in two phases.

`land_raw` writes the GTF verbatim into `raw.ensembl__gtf`. `transform` reads it
back out and derives the annotation tables from it. Keeping the two apart means
that changing how we *interpret* a GTF is a re-run of transform rather than a
re-download, and that the attributes nobody has needed yet are already here when
somebody does.

The phases want different write semantics, which is why they are separate
functions rather than one pipeline. An Ensembl release is immutable, so raw is
replaced wholesale per (taxon_id, genome_id, ensembl_release) and is idempotent
under re-ingest. The derived tables have real keys and a maintained current state, so
they are the ones that carry valid_from/valid_to and merge.

The derived genome-feature tables are stacked multi-writer tables discriminated
by a `source` column; this module is the SOURCE = 'ENSEMBL' writer, stamping
that into every derived row and into every merge scope. See ncbi.py's module
docstring for why a scope that fails to name its writer flip-flops.

The same goes one level down for the assembly (issue #94). A taxon id is not an
assembly: release 116 lists 359 species entries over 276 taxa — 13 share 10090
(GRCm39 and 12 MGP strains), 28 share 9823 (pig), 12 share 9940 (sheep) — and
each has its own GTF with its own gene ids. So `genome_id` is in the business
key and the merge scope of every table carrying coordinates, and in raw's
replace scope; under (taxon_id, source) alone, loading a second assembly
retired the first one's rows (2026-09-18, stopped at the first mouse strain).

annotation.identifier_mapping has no assembly column, so its ENSEMBL rows are
written for the taxon's canonical assembly only, and an alternate assembly's
ingest leaves the table alone: MGP_129S1SvImJ_G… ids get no rows there, and
their symbols are on annotation.gene.
"""

import re
import urllib.request

import duckdb
from pyiceberg.expressions import And, EqualTo

from . import merge

FTP = "https://ftp.ensembl.org/pub/release-{release}"

# This writer's name in the stacked tables' controlled vocabulary ('ENSEMBL',
# 'REFSEQ', 'GENCODE', ...): the value of every derived row's `source` column
# and of the writer leg of every merge scope.
SOURCE = "ENSEMBL"

GTF_COLUMNS = (
    "{'seqname':'VARCHAR','source':'VARCHAR','feature':'VARCHAR',"
    "'start':'BIGINT','end':'BIGINT','score':'VARCHAR','strand':'VARCHAR',"
    "'frame':'VARCHAR','attribute':'VARCHAR'}"
)


def _attr(key):
    return f"""nullif(regexp_extract(attribute, '{key} "([^"]*)"', 1), '')"""


def _canonical(names):
    """The one species entry, of those sharing a taxon id, that stands for the taxon.

    The unsuffixed name where there is one — the entry every other is an
    extension of (mus_musculus, canis_lupus_familiaris beside
    canis_lupus_familiarisboxer) — else the first alphabetically
    (cricetulus_griseus_chok1gshd). Release 116: 20 of 276 taxa have more than
    one entry, 14 of them with an unsuffixed name.
    """
    return next((n for n in names if all(m.startswith(n) for m in names)), min(names))


def species_info(ensembl_release, species):
    """taxonomy_id / assembly / accession / canonical for a species, from Ensembl itself."""
    url = f"{FTP.format(release=ensembl_release)}/species_EnsemblVertebrates.txt"
    with urllib.request.urlopen(url) as r:
        rows = [f for f in (line.split("\t") for line in r.read().decode().splitlines())
                if len(f) > 5]
    for f in rows:
        if f[1] == species:
            # 13 old assemblies (turTru1, TETRAODON 8.0, ...) have no INSDC
            # accession; the assembly name is their genome_id, since '' names
            # nothing and is no partition value.
            # ponytail: is_canonical is refreshed only when an assembly is
            # (re-)ingested, so load all of a taxon's assemblies in one release.
            return {"taxon_id": int(f[3]), "assembly": f[4], "accession": f[5] or f[4],
                    "canonical": _canonical([g[1] for g in rows if g[3] == f[3]]) == species}
    raise SystemExit(f"{species} not in Ensembl release {ensembl_release} vertebrates")


def _pick_gtf(names, ensembl_release, species):
    """The primary GTF among a directory's files: not .chr, .abinitio or a patch build.

    Prefer the file stamped with this Ensembl release. Three species in the
    vertebrates tree — C. elegans, D. melanogaster, S. cerevisiae — are imported
    from Ensembl Metazoa/Fungi and keep that division's release number
    (`WBcel235.63.gtf.gz` under release 116), so when no file carries the
    release, a single remaining primary GTF is taken as it is.
    """
    primary = [n for n in names if n.endswith(".gtf.gz")
               and not re.search(r"\.(chr|abinitio|chr_patch_hapl_scaff)\.", n)]
    stamped = [n for n in primary if n.endswith(f".{ensembl_release}.gtf.gz")]
    if len(stamped) == 1:
        return stamped[0]
    if not stamped and len(primary) == 1:
        return primary[0]
    raise SystemExit(f"no unambiguous primary GTF for {species} in Ensembl release "
                     f"{ensembl_release}: {primary or names}")


def gtf_url(ensembl_release, species):
    listing = f"{FTP.format(release=ensembl_release)}/gtf/{species}/"
    with urllib.request.urlopen(listing) as r:
        names = re.findall(r'>([^<>"]+\.gtf\.gz)<', r.read().decode())
    return listing + _pick_gtf(names, ensembl_release, species)


def land_raw(cat, release, species, ensembl_release, url=None, info=None):
    """Phase 1: the GTF, verbatim, into raw.ensembl__gtf."""
    info = info or species_info(ensembl_release, species)
    con = duckdb.connect()
    arrow = con.sql(f"""
        SELECT seqname, source, feature, "start", "end", score, strand, frame, attribute,
               {info['taxon_id']}::INTEGER AS taxon_id,
               '{ensembl_release}' AS ensembl_release,
               '{release}' AS landed_in,
               '{info['accession']}' AS genome_id
        FROM read_csv('{url or gtf_url(ensembl_release, species)}', sep='\t', header=false,
                      comment='#', auto_detect=false, columns={GTF_COLUMNS})
    """).to_arrow_table()
    n = merge.write(cat, "raw.ensembl__gtf", arrow, _raw_scope(info, ensembl_release))
    merge.manifest(cat, release, "ensembl", species, url or gtf_url(ensembl_release, species), n,
                   version=ensembl_release, method="release_number")
    return info, n


# Every derived table is multi-writer: a merge must only be able to retire rows
# it wrote, so the scope names the writer as well as the species. Without this,
# a second writer into the same taxon — NCBI in identifier_mapping today, RefSeq
# or GENCODE in the genome-feature tables tomorrow — and Ensembl retire each
# other's rows on alternating ingests: silently, and forever, because the
# "current" view is never empty. (identifier_mapping said 'Ensembl' until
# `bioconice migrate-assembly-scope` respelled its 4,232,653 rows.)
WRITER = EqualTo("source", SOURCE)


def _raw_scope(info, ensembl_release):
    return And(EqualTo("taxon_id", info["taxon_id"]), EqualTo("genome_id", info["accession"]),
               EqualTo("ensembl_release", str(ensembl_release)))


def _scope(identifier, info):
    """(taxon, writer), and the assembly on every table that has one — all but
    identifier_mapping, which only the canonical assembly writes."""
    scope = And(EqualTo("taxon_id", info["taxon_id"]), WRITER)
    if identifier == "annotation.identifier_mapping":
        return scope
    return And(scope, EqualTo("genome_id", info["accession"]))


def transform(cat, release, info, ensembl_release):
    """Phase 2: derive the annotation tables from landed raw rows."""
    taxon, genome = info["taxon_id"], info["accession"]
    # An info dict without the flag is a taxon's only assembly.
    canonical = info.get("canonical", True)
    raw = cat.load_table("raw.ensembl__gtf").scan(
        row_filter=_raw_scope(info, ensembl_release)).to_arrow()

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
            SELECT '{genome}' AS genome_id, {taxon}::INTEGER AS taxon_id,
                   '{SOURCE}' AS source, '{info['assembly']}' AS assembly_name,
                   {canonical} AS is_canonical
        """),
        "annotation.gene": q(f"""
            SELECT gene_id, {taxon}::INTEGER AS taxon_id, '{SOURCE}' AS source,
                   '{genome}' AS genome_id, gene_version AS version, gene_name AS symbol,
                   gene_biotype AS gene_type, gene_source AS curation_source
            FROM feat WHERE feature = 'gene'
        """),
        "annotation.transcript": q(f"""
            SELECT transcript_id, {taxon}::INTEGER AS taxon_id, '{SOURCE}' AS source,
                   '{genome}' AS genome_id, gene_id, transcript_version AS version,
                   transcript_biotype AS biotype, canonical
            FROM feat WHERE feature = 'transcript'
        """),
        # A CDS line carries the same transcript_id and exon_number as the exon it
        # lies in, which is what lets coding bounds and phase ride on the exon row.
        "annotation.exon": q(f"""
            SELECT e.exon_id, e.transcript_id, {taxon}::INTEGER AS taxon_id,
                   '{SOURCE}' AS source, '{genome}' AS genome_id,
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
                   {taxon}::INTEGER AS taxon_id, '{SOURCE}' AS source,
                   NULL::DOUBLE AS confidence
            FROM feat WHERE feature = 'gene' AND gene_name IS NOT NULL
        """),
    }
    if not canonical:
        # Not merged empty: that would retire the canonical assembly's rows.
        del out["annotation.identifier_mapping"]
    return {k: merge.merge(cat, k, a, release, _scope(k, info)) for k, a in out.items()}


def ingest(cat, release, species, ensembl_release):
    info, n = land_raw(cat, release, species, ensembl_release)
    return {"raw.ensembl__gtf": n, **transform(cat, release, info, ensembl_release)}
