"""BEDbase -> Iceberg: the resource-layer catalog of referenced BED files and bedsets.

BEDbase (bedbase.org) catalogs ~664k BED files across ~22k bedsets, mostly GEO/ENCODE
peak and region calls processed through its bedboss pipeline. The SPEC's `resource`
namespace exists for exactly this: catalog entries with a licence and provenance,
objects **referenced, never ingested** — the interval data itself (~20 billion rows)
is out of scope.

**Landing is BEDbase's own monthly Parquet snapshot, never an API crawl.** The index at
`/v1/exports` lists each snapshot's files with sha256 and record count; the files sit
on data2.bedbase.org, so pulling them costs the API and its database nothing. Three
files: `metadata` (the `bed` table joined with `bed_metadata`), `bedsets`, and
`bedset_membership`. The first production landing (2026-09-18) predates our knowing
the snapshot existed: it paged `/v1/bed/list`, whose offset listing stops answering
past ~70k, reached 81% of the files and took the API down for ~95 minutes on the way
(databio/bedhost#287). Do not bring the crawl back.

version = the snapshot's date, the label BEDbase itself publishes it under.

**Raw column names predate the snapshot.** The listing API nested the sample-level
fields under `annotation`, landed flattened as `annotation_*`; the snapshot prints them
bare (and `organism` as `species_name`). The raw tables keep the names they were
created with rather than being rebuilt; `_read_bed` is the whole mapping. Timestamps
are landed as the ISO 8601 text the API printed, so a file's row is byte-identical
whichever route landed it.

Still not here (issue #115): per-file size, checksum, http/s3/bigbed URIs and the
bedstat statistics — not in the snapshot, and one `/v1/bed/{id}/metadata?full=true`
request per record otherwise.

**Key genomes on `genome_digest`, never `genome_alias`.** The alias is free text —
verified live 2026-09-17, one Arabidopsis (taxon 3702) record carries the alias
'hg18', a human assembly name — so it is landed as a display label only, never
joined on. `annotation_species_id` has the same shape of problem: usually a single
NCBI taxon id as text but occasionally a comma-separated pair for a co-infection
study ('9606, 11676', also verified live), so the taxon_id join is a TRY_CAST that
becomes NULL rather than an error on the exceptions.
"""

import hashlib
import json
import tempfile
import urllib.request
from pathlib import Path

import duckdb
from pyiceberg.expressions import AlwaysTrue, EqualTo

from . import merge
from .ncbi import _land

EXPORTS = "https://api.bedbase.org/v1/exports"
# file_type in the exports index -> raw table. The file_type is also the manifest artifact.
FILES = {
    "metadata": "raw.bedbase__bed",
    "bedsets": "raw.bedbase__bedset",
    "bedset_membership": "raw.bedbase__bedset_membership",
}


def _get(url):
    # bedbase.org sits behind Cloudflare, which 403s urllib's default UA.
    req = urllib.request.Request(url, headers={"User-Agent": "bioc-on-ice"})
    return urllib.request.urlopen(req, timeout=300)


def latest_snapshot():
    """The newest snapshot's index entries, keyed by file_type."""
    with _get(EXPORTS) as r:
        entries = [e for e in json.load(r)["results"] if e["file_type"] in FILES]
    newest = max(e["creation_date"] for e in entries)
    snapshot = {e["file_type"]: e for e in entries if e["creation_date"] == newest}
    if set(snapshot) != set(FILES):
        raise ValueError(f"bedbase: snapshot {newest} lists {sorted(snapshot)}, expected {sorted(FILES)}")
    return snapshot


def _download(entry, directory):
    """One snapshot file to disk, refused unless it matches the index's sha256."""
    path = Path(directory) / entry["file_path"].rsplit("/", 1)[1]
    digest = hashlib.sha256()
    with _get(entry["file_path"]) as r, open(path, "wb") as out:
        while chunk := r.read(1 << 20):
            digest.update(chunk)
            out.write(chunk)
    if digest.hexdigest() != entry["checksum"]:
        raise ValueError(f"bedbase: {entry['file_path']} sha256 {digest.hexdigest()} != "
                         f"index {entry['checksum']}")
    return str(path)


def _iso(col):
    # The snapshot types these TIMESTAMPTZ; the raw columns are the text the API printed —
    # Python's isoformat(), which drops the fraction when the microseconds are zero.
    return (f"replace(strftime({col} AT TIME ZONE 'UTC', '%Y-%m-%dT%H:%M:%S.%fZ'), "
            f"'.000000Z', 'Z') AS {col}")


def _read_bed(path):
    return f"""(
        SELECT id, name, description, genome_alias, genome_digest, bed_compliance, data_format,
               compliant_columns::INTEGER AS compliant_columns,
               non_compliant_columns::INTEGER AS non_compliant_columns,
               is_universe, license_id, processed,
               {_iso('submission_date')}, {_iso('last_update_date')},
               species_name AS annotation_organism, species_id AS annotation_species_id,
               genotype AS annotation_genotype, phenotype AS annotation_phenotype,
               cell_type AS annotation_cell_type, cell_line AS annotation_cell_line,
               tissue AS annotation_tissue, library_source AS annotation_library_source,
               assay AS annotation_assay, antibody AS annotation_antibody,
               target AS annotation_target, treatment AS annotation_treatment,
               array_to_string(global_sample_id, '|') AS annotation_global_sample_id,
               array_to_string(global_experiment_id, '|') AS annotation_global_experiment_id,
               original_file_name AS annotation_original_file_name,
               NULL::VARCHAR AS annotation_description,  -- listing-API only, see schemas.py
               header, indexed, file_indexed
        FROM read_parquet({path!r})
    )"""


def _read_bedset(path):
    return f"""(
        SELECT id, name, md5sum, {_iso('submission_date')}, {_iso('last_update_date')},
               description, bedfile_count::INTEGER AS bedfile_count, author,
               source AS bedset_source, summary, bedset_means, bedset_standard_deviation,
               bedset_stats, processed
        FROM read_parquet({path!r})
    )"""


def _read_membership(path):
    return f"(SELECT bedset_id, bedfile_id FROM read_parquet({path!r}))"


READERS = {"metadata": _read_bed, "bedsets": _read_bedset, "bedset_membership": _read_membership}


def land_raw(cat, release, paths=None):
    """Phase 1: the snapshot's three files, whole, replacing what was there.

    `paths` ({file_type: local parquet}) stands in for the download, for offline tests;
    it skips the index, so nothing is verified and the version is the retrieval date.
    """
    with tempfile.TemporaryDirectory() as tmp:
        snapshot = {} if paths else latest_snapshot()
        counts = {}
        for kind, identifier in FILES.items():
            entry = snapshot.get(kind)
            path = paths[kind] if paths else _download(entry, tmp)
            # SET TimeZone: _iso's AT TIME ZONE reads the session zone, not the host's.
            n = counts[identifier] = _land(cat, release, identifier, READERS[kind](path),
                                           config={"TimeZone": "UTC"})
            if entry and n != entry["record_count"]:
                raise ValueError(f"bedbase: {identifier} landed {n:,} rows, index says "
                                 f"{entry['record_count']:,}")
            if entry:
                merge.manifest(cat, release, "bedbase", kind, entry["file_path"], n,
                               version=entry["creation_date"][:10], method="release_number",
                               checksum=entry["checksum"])
            else:
                merge.manifest(cat, release, "bedbase", kind, path, n)
    return counts


# Assertions on the derived rows, in icite.py's style: each SQL counts violations,
# and any violation fails the ingest before either merge runs.
CHECKS = {
    "bedfile resource_id is unique": "SELECT count(*) - count(DISTINCT resource_id) FROM bedfile",
    "bedset resource_id is unique":  "SELECT count(*) - count(DISTINCT resource_id) FROM bedset",
}


def _check(con):
    failed = {name: con.sql(sql).fetchone()[0] for name, sql in CHECKS.items()}
    failed = {k: v for k, v in failed.items() if v}
    if failed:
        raise ValueError("bedbase: derived rows violate "
                          + "; ".join(f"{k} ({v:,} rows)" for k, v in failed.items()))


def transform(cat, release):
    """Phase 2: the bedfile and bedset resource entries and their relationships, from raw.

    A snapshot is the whole catalog, so every merge scope is everything BEDbase
    asserts: a record absent from the snapshot is retired.
    """
    con = duckdb.connect()
    for name in ("bed", "bedset", "bedset_membership"):
        con.register(f"raw_{name}", cat.load_table(f"raw.bedbase__{name}").scan().to_arrow())

    bedfile = con.sql("""
        SELECT id AS resource_id, name AS title, description,
               genome_digest, genome_alias,
               TRY_CAST(NULLIF(annotation_species_id, '') AS INTEGER) AS taxon_id,
               NULLIF(annotation_organism, '') AS organism,
               NULLIF(annotation_assay, '') AS assay,
               NULLIF(annotation_target, '') AS target,
               NULLIF(annotation_antibody, '') AS antibody,
               NULLIF(annotation_cell_type, '') AS cell_type,
               NULLIF(annotation_cell_line, '') AS cell_line,
               NULLIF(annotation_tissue, '') AS tissue,
               NULLIF(annotation_treatment, '') AS treatment,
               list_sort(str_split(NULLIF(annotation_global_sample_id, ''), '|')) AS sample_id,
               list_sort(str_split(NULLIF(annotation_global_experiment_id, ''), '|')) AS experiment_id,
               bed_compliance AS compliance, data_format AS format,
               license_id, 'BEDbase' AS provider,
               submission_date AS submitted, last_update_date AS updated
        FROM raw_bed
    """).to_arrow_table()

    bedset = con.sql("""
        SELECT id AS resource_id, name AS title, description,
               bedfile_count, author, bedset_source, 'BEDbase' AS provider,
               submission_date AS submitted, last_update_date AS updated
        FROM raw_bedset
    """).to_arrow_table()

    con.register("bedfile", bedfile)
    con.register("bedset", bedset)
    _check(con)

    # The joinable form of the accession lists and of bedset membership: one row per
    # (bed file, relationship, target). An accession target must be 'prefix:accession':
    # upstream's lists also carry bare tags and blanks that name nothing — 'encode' beside
    # the real 'encode:ENCSR…' on 15,749 files, 'excluderanges', 'GLOBAL_EXP', and a
    # trailing '' on 41,969 sample lists (2026-09-01 snapshot, issue #158). They stay in
    # raw and in the list columns, as published; they are not relationships.
    rel = con.sql("""
        SELECT DISTINCT * FROM (
            SELECT resource_id, 'derived_from_sample' AS relationship, unnest(sample_id) AS target_id,
                   'bedbase' AS source FROM bedfile
            UNION ALL
            SELECT resource_id, 'derived_from_experiment', unnest(experiment_id), 'bedbase' FROM bedfile)
        WHERE regexp_matches(target_id, '^[^:]+:.+')
        UNION ALL
        SELECT DISTINCT bedfile_id, 'member_of_bedset', bedset_id, 'bedbase' FROM raw_bedset_membership
    """).to_arrow_table()
    return {
        "resource.bedbase__bedfile": merge.merge(
            cat, "resource.bedbase__bedfile", bedfile, release, AlwaysTrue()),
        "resource.bedbase__bedset": merge.merge(
            cat, "resource.bedbase__bedset", bedset, release, AlwaysTrue()),
        "resource.resource_relationship": merge.merge(
            cat, "resource.resource_relationship", rel, release, EqualTo("source", "bedbase")),
    }


def ingest(cat, release, paths=None):
    return {**land_raw(cat, release, paths), **transform(cat, release)}
