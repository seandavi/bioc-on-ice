"""BEDbase -> Iceberg: the resource-layer catalog of referenced BED files and bedsets.

BEDbase (api.bedbase.org) catalogs 663,721 BED files across 22,189 bedsets, mostly
GEO/ENCODE peak and region calls processed through its bedboss pipeline. The SPEC's
`resource` namespace exists for exactly this: catalog entries with a URI, size,
checksum and licence, objects **referenced, never ingested** — the interval data
itself (~20 billion rows) is out of scope.

There is no bulk dump, so landing is a paging crawl of `/v1/bed/list` and
`/v1/bedset/list` (`count`/`limit`/`offset`, up to 100 records a page). DuckDB's
`read_json` fetches the pages directly over HTTP — no urllib download loop needed —
given the list of page URLs; `_count` makes the one request that determines how many
pages that list has to be. version = retrieval date: BEDbase publishes no release
number, same unversioned shape as NCBI (ncbi.py).

**v1 lands the listings only.** Per-record detail (`/v1/bed/{id}/metadata?full=true`
for size/checksum/http_uri/s3_uri/bigbed_uri/stats, and `/v1/bedset/{id}?full=true`
for bedset membership) is one HTTP request per record — 663,721 and 22,189 of them.
The listing endpoints already carry everything else the issue's shape asks for
(genome_digest, taxon, assay-level annotation, the DUO licence), so v1 pays ~6,637
+ ~222 requests total rather than ~686,000, and the URIs/stats/membership are left
as columns to add by schema evolution in a follow-up. See issue #79 for the
tradeoff; #80 is the companion genomic-partition enrichment, not implemented here.

**Key genomes on `genome_digest`, never `genome_alias`.** The alias is free text —
verified live 2026-09-17, one Arabidopsis (taxon 3702) record carries the alias
'hg18', a human assembly name — so it is landed as a display label only, never
joined on. `annotation_species_id` has the same shape of problem: usually a single
NCBI taxon id as text but occasionally a comma-separated pair for a co-infection
study ('9606, 11676', also verified live), so the taxon_id join is a TRY_CAST that
becomes NULL rather than an error on the exceptions.

`--limit N` bounds a crawl to (approximately) N records per endpoint, for a partial
run. A bounded crawl is not a claim of completeness, so it MUST NOT retire records
outside what it fetched: its merge scope is the fetched ids themselves rather than
AlwaysTrue(), the same shape as `ncbi._where(taxon)` scoping a single-species run
versus the full-dump default.
"""

import json
import time
import urllib.request

import duckdb
from pyiceberg.expressions import AlwaysTrue, In

from . import merge
from .ncbi import _land, _manifest

BASE = "https://api.bedbase.org/v1"
PAGE = 100

# Explicit column specs for read_json, in the spirit of ncbi.tsv()'s auto_detect=false:
# a renamed or removed upstream field then fails loudly in DuckDB's binder rather than
# silently vanishing, and dates stay VARCHAR (verbatim text) instead of being parsed
# and reformatted by DuckDB's own type inference.
BED_COLUMNS = (
    "STRUCT(name VARCHAR, genome_alias VARCHAR, genome_digest VARCHAR, "
    "bed_compliance VARCHAR, data_format VARCHAR, compliant_columns BIGINT, "
    "non_compliant_columns BIGINT, id VARCHAR, description VARCHAR, "
    "submission_date VARCHAR, last_update_date VARCHAR, is_universe BOOLEAN, "
    "license_id VARCHAR, processed BOOLEAN, "
    "annotation STRUCT(organism VARCHAR, species_id VARCHAR, genotype VARCHAR, "
    "phenotype VARCHAR, description VARCHAR, cell_type VARCHAR, cell_line VARCHAR, "
    "tissue VARCHAR, library_source VARCHAR, assay VARCHAR, antibody VARCHAR, "
    "target VARCHAR, treatment VARCHAR, global_sample_id VARCHAR[], "
    "global_experiment_id VARCHAR[], original_file_name VARCHAR))[]"
)
BEDSET_COLUMNS = (
    "STRUCT(id VARCHAR, name VARCHAR, md5sum VARCHAR, submission_date VARCHAR, "
    "last_update_date VARCHAR, description VARCHAR, bedfile_count BIGINT, "
    "author VARCHAR, source VARCHAR)[]"
)
ENVELOPE = "{{'count':'BIGINT','limit':'BIGINT','offset':'BIGINT','results':'{results}'}}"


def _count(kind):
    """The endpoint's total record count, from one un-paged request.

    A User-Agent is required: bedbase.org sits behind Cloudflare, which 403s
    urllib's default 'Python-urllib/x.y' UA. DuckDB's own HTTP client (used for
    the paged reads themselves) is unaffected, so only this one request needs it.
    """
    req = urllib.request.Request(f"{BASE}/{kind}/list?limit=1&offset=0",
                                  headers={"User-Agent": "bioc-on-ice"})
    with urllib.request.urlopen(req, timeout=60) as r:
        return json.load(r)["count"]


def _page_urls(kind, limit=None):
    """Every page URL for `kind` ('bed' or 'bedset'), bounded to `limit` records."""
    count = _count(kind)
    total = min(count, limit) if limit else count
    return [f"{BASE}/{kind}/list?limit={PAGE}&offset={o}" for o in range(0, max(total, 1), PAGE)]


def _read_json(urls, columns):
    return f"read_json({urls!r}, columns={ENVELOPE.format(results=columns)})"


def _read_bed(urls, limit=None):
    lim = f" LIMIT {limit}" if limit else ""
    return f"""(
        SELECT
            r.id AS id, r.name AS name, r.description AS description,
            r.genome_alias AS genome_alias, r.genome_digest AS genome_digest,
            r.bed_compliance AS bed_compliance, r.data_format AS data_format,
            r.compliant_columns::INTEGER AS compliant_columns,
            r.non_compliant_columns::INTEGER AS non_compliant_columns,
            r.is_universe AS is_universe, r.license_id AS license_id,
            r.processed AS processed,
            r.submission_date AS submission_date, r.last_update_date AS last_update_date,
            r.annotation.organism AS annotation_organism,
            r.annotation.species_id AS annotation_species_id,
            r.annotation.genotype AS annotation_genotype,
            r.annotation.phenotype AS annotation_phenotype,
            r.annotation.description AS annotation_description,
            r.annotation.cell_type AS annotation_cell_type,
            r.annotation.cell_line AS annotation_cell_line,
            r.annotation.tissue AS annotation_tissue,
            r.annotation.library_source AS annotation_library_source,
            r.annotation.assay AS annotation_assay,
            r.annotation.antibody AS annotation_antibody,
            r.annotation.target AS annotation_target,
            r.annotation.treatment AS annotation_treatment,
            array_to_string(r.annotation.global_sample_id, '|') AS annotation_global_sample_id,
            array_to_string(r.annotation.global_experiment_id, '|') AS annotation_global_experiment_id,
            r.annotation.original_file_name AS annotation_original_file_name
        FROM (SELECT UNNEST(results) AS r FROM {_read_json(urls, BED_COLUMNS)})
        {lim}
    )"""


def _read_bedset(urls, limit=None):
    lim = f" LIMIT {limit}" if limit else ""
    return f"""(
        SELECT
            r.id AS id, r.name AS name, r.md5sum AS md5sum,
            r.submission_date AS submission_date, r.last_update_date AS last_update_date,
            r.description AS description, r.bedfile_count::INTEGER AS bedfile_count,
            r.author AS author, r.source AS bedset_source
        FROM (SELECT UNNEST(results) AS r FROM {_read_json(urls, BEDSET_COLUMNS)})
        {lim}
    )"""


def _land_retrying(cat, release, identifier, source):
    """`_land`, riding out the transient 5xx a several-thousand-page crawl hits.

    A full crawl is thousands of individual page fetches multiplexed inside one
    DuckDB read_json call; unlike ncbi._commit's 429 (one rate limit, waited out
    once), a mid-crawl 503 from bedbase.org's own edge means restarting the whole
    read — DuckDB does not expose a per-page retry — so this retries `_land`
    itself rather than the fetch beneath it.
    """
    for attempt in range(5):
        try:
            return _land(cat, release, identifier, source)
        except duckdb.HTTPException:
            if attempt == 4:
                raise
            time.sleep(10 * (attempt + 1))


def land_raw(cat, release, limit=None, bed_urls=None, bedset_urls=None):
    """Phase 1: both listings, verbatim, replacing what was there.

    `bed_urls`/`bedset_urls` override the live paging crawl with explicit page
    URLs (or local fixture paths) for offline tests.
    """
    bed_urls = bed_urls if bed_urls is not None else _page_urls("bed", limit)
    bedset_urls = bedset_urls if bedset_urls is not None else _page_urls("bedset", limit)

    n_bed = _land_retrying(cat, release, "raw.bedbase__bed", _read_bed(bed_urls, limit))
    _manifest(cat, release, "bedbase_bed", f"{BASE}/bed/list", n_bed)

    n_bedset = _land_retrying(cat, release, "raw.bedbase__bedset", _read_bedset(bedset_urls, limit))
    _manifest(cat, release, "bedbase_bedset", f"{BASE}/bedset/list", n_bedset)

    return n_bed, n_bedset


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


def transform(cat, release, limit=None):
    """Phase 2: the bedfile and bedset resource entries, read back from raw.

    Scope is AlwaysTrue() for a full crawl (limit=None), reproducing the retire
    leg of merge.merge for records that vanished upstream. For a bounded --limit
    crawl, scope narrows to the ids just fetched, so a partial run can never
    retire records outside the slice it saw — see the module docstring.
    """
    con = duckdb.connect()
    con.register("raw_bed", cat.load_table("raw.bedbase__bed").scan().to_arrow())
    con.register("raw_bedset", cat.load_table("raw.bedbase__bedset").scan().to_arrow())

    bedfile = con.sql("""
        SELECT id AS resource_id, name AS title, description, genome_digest, genome_alias,
               TRY_CAST(NULLIF(annotation_species_id, '') AS INTEGER) AS taxon_id,
               NULLIF(annotation_organism, '') AS organism,
               NULLIF(annotation_assay, '') AS assay,
               NULLIF(annotation_target, '') AS target,
               NULLIF(annotation_antibody, '') AS antibody,
               NULLIF(annotation_cell_type, '') AS cell_type,
               NULLIF(annotation_cell_line, '') AS cell_line,
               NULLIF(annotation_tissue, '') AS tissue,
               NULLIF(annotation_treatment, '') AS treatment,
               NULLIF(annotation_global_sample_id, '') AS sample_id,
               NULLIF(annotation_global_experiment_id, '') AS experiment_id,
               bed_compliance AS compliance, data_format AS format,
               license_id, 'BEDbase' AS provider,
               submission_date AS submitted, last_update_date AS updated
        FROM raw_bed
    """).to_arrow_table()

    bedset = con.sql("""
        SELECT id AS resource_id, name AS title, description, bedfile_count, author,
               bedset_source, 'BEDbase' AS provider,
               submission_date AS submitted, last_update_date AS updated
        FROM raw_bedset
    """).to_arrow_table()

    con.register("bedfile", bedfile)
    con.register("bedset", bedset)
    _check(con)

    def scope(table):
        # A bounded crawl only saw these ids, so it is only authoritative over
        # them: scoping to In(...) rather than AlwaysTrue() means nothing outside
        # the slice can be retired by a partial run (module docstring).
        if not limit:
            return AlwaysTrue()
        ids = [r[0] for r in con.sql(f"SELECT resource_id FROM {table}").fetchall()]
        return In("resource_id", ids)

    return {
        "resource.bedbase__bedfile": merge.merge(
            cat, "resource.bedbase__bedfile", bedfile, release, scope("bedfile")),
        "resource.bedbase__bedset": merge.merge(
            cat, "resource.bedbase__bedset", bedset, release, scope("bedset")),
    }


def ingest(cat, release, limit=None, bed_urls=None, bedset_urls=None):
    n_bed, n_bedset = land_raw(cat, release, limit, bed_urls, bedset_urls)
    return {"raw.bedbase__bed": n_bed, "raw.bedbase__bedset": n_bedset,
            **transform(cat, release, limit)}
