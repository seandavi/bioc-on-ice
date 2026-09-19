"""ENCODE portal metadata -> Iceberg: the experiment and file inventory, files referenced.

The ENCODE portal (encodeproject.org) catalogs 28,642 experiments and 1,659,000
files visible anonymously on 2026-09-18 (released, archived and revoked alike).
This is a `resource`-layer source, like CELLxGENE and BEDbase: catalog entries
with typed URIs, size and checksum, the bytes **referenced, never ingested**.
Its first use is making BEDbase's `resource.resource_relationship` targets
resolvable: BEDbase writes ENCODE accessions as 'encode:ENCSR…' / 'encode:ENCFF…',
and `resource_id` here is spelled exactly that way so the two join with `=`.

**Landing: `report.tsv` with an explicit `field=` list, one request per type.**
The alternative, `search/?type=File&format=json&limit=all`, returns one JSON
object holding every record — for 1.66M files a multi-GB object DuckDB cannot
stream — and `frame=object` leaves biosample, target, lab and award as links
to other objects rather than values, so it is not more complete where it
matters. `report.tsv` streams from a server-side scan (no offset paging, so none
of BEDbase's page drift), takes dotted paths into embedded objects
(`biosample_ontology.term_id`, `target.genes.geneid`), and always returns the
whole inventory: it rewrites any `limit` to `limit=all`. Measured 2026-09-18:
experiments 16 MB in 45 s, files ~1.1 GB in ~7 min, one GET each — far under the
portal's 10 GET/s limit.

What that choice costs, stated rather than hidden:
  * Without `field=` the report carries only the portal's default display
    columns, so the columns are an explicit, declared list (EXPERIMENT_FIELDS,
    FILE_FIELDS). Raw is whole in ROWS — never filtered by assay, organism,
    status or file type — but it is a declared subset of each object's
    properties. Adding a field is a schema addition and a re-land.
  * The header row is display titles ('Biosample term name'), which the portal
    can reword; the first line is a comment carrying a timestamp and the request
    URL. So the read is positional with declared names, and `_verify` checks that
    the comment line echoes our fields in our order before anything lands.
  * Cells are written with no quoting at all: upstream collapses whitespace runs
    (tabs, newlines) inside a value to one space and joins lists with ','. Quote
    characters are literal data, so the read disables quoting. A list whose
    values contain commas cannot be split back; the lists transform splits
    (gene ids, accessions, object paths, assembly labels, dbxrefs) cannot.

The portal publishes no release number and the inventory mutates in place, so
version = retrieval date and raw holds the **latest crawl only**, as
raw.cellxgene__dataset does. A short download must never replace a good
landing: the report is downloaded to scratch first (User-Agent, timeout, whole-
request retries), and its row count is checked against the search API's `total`
before `_land` touches the table.

Licence (checked 2026-09-18, issue #137): "External data users may freely
download, analyze and publish results based on any ENCODE data without
restrictions"; citation of the ENCODE Consortium is requested
(https://www.encodeproject.org/help/citing-encode/). Carried in the table
comments.

Out of scope here: SCREEN cCREs (`annotation.encode__ccre`) need an assembly key
and wait on issue #94. Also not landed: the 620,734 Annotation datasets (and
Reference, Series, ...) that share the ENCSR accession space with experiments —
a file's `dataset` can be any of them, so `dataset_type` says which, and only
'experiments' resolve in resource.encode__experiment. That is also the ceiling on
BEDbase resolution, measured 2026-09-18 against the live catalog: all 405,210
'encode:ENCFF…' targets resolve in resource.encode__file, but only 55,537 of
401,520 'encode:ENCSR…' rows resolve in resource.encode__experiment — 345,668 of
the rest name Annotation datasets. Landing `type=Annotation` the same way is the
follow-up that closes it.
"""

import http.client
import json
import os
import shutil
import tempfile
import time
import urllib.parse
import urllib.request
from datetime import datetime
from pathlib import Path

import duckdb
from pyiceberg.expressions import AlwaysTrue, EqualTo

from . import merge
from .ncbi import _land

BASE = "https://www.encodeproject.org"
UA = {"User-Agent": "bioc-on-ice (https://github.com/seandavi/bioc-on-ice)"}

# report.tsv `field=` paths, in request (= column) order. Raw column names are
# these with '.' -> '_'.
EXPERIMENT_FIELDS = (
    "accession", "uuid", "status", "date_created", "date_submitted", "date_released",
    "description", "assay_term_id", "assay_term_name", "assay_title", "assay_slims",
    "biosample_ontology.term_id", "biosample_ontology.term_name",
    "biosample_ontology.classification", "biosample_ontology.organ_slims",
    "biosample_ontology.cell_slims", "biosample_summary",
    "replicates.library.biosample.organism.scientific_name",
    "replicates.library.biosample.organism.taxon_id",
    "target.name", "target.label", "target.investigated_as", "target.genes.geneid",
    "target.genes.symbol", "control_type", "perturbed", "lab.title", "award.name",
    "award.project", "award.rfa", "assembly", "replication_type", "bio_replicate_count",
    "tech_replicate_count", "dbxrefs", "doi", "internal_tags", "alternate_accessions",
    "supersedes", "superseded_by", "possible_controls",
)
FILE_FIELDS = (
    "accession", "external_accession", "title", "uuid", "status", "dataset", "file_format",
    "file_format_type", "file_type", "output_type", "output_category", "assembly",
    "genome_annotation", "file_size", "md5sum",
    "content_md5sum", "href", "cloud_metadata.url", "s3_uri", "no_file_available", "restricted",
    "derived_from", "biological_replicates", "technical_replicates", "preferred_default",
    "processed", "run_type", "read_length", "paired_end", "paired_with", "date_created",
    "lab.title", "award.project", "award.rfa", "alternate_accessions", "superseded_by",
)


def report_url(type_, fields):
    return f"{BASE}/report.tsv?type={type_}&" + "&".join(f"field={f}" for f in fields)


def _total(type_):
    """The portal's own count of `type_` objects, from one zero-row search."""
    req = urllib.request.Request(f"{BASE}/search/?type={type_}&limit=0&format=json", headers=UA)
    with urllib.request.urlopen(req, timeout=120) as r:
        return json.load(r)["total"]


def _download(url, path):
    """One streaming GET to `path`, retried whole: the report cannot be resumed.

    A dropped chunked stream raises (IncompleteRead) rather than ending quietly,
    and `_verify`'s count is the backstop for one that does not.
    """
    for attempt in range(4):
        try:
            req = urllib.request.Request(url, headers=UA)
            with urllib.request.urlopen(req, timeout=300) as r, open(path, "wb") as f:
                shutil.copyfileobj(r, f, 1 << 20)
            return
        except (OSError, http.client.HTTPException):
            if attempt == 3:
                raise
            time.sleep(30 * (attempt + 1))


def _source(path, fields, retrieval_date):
    """The DuckDB read: positional, every column VARCHAR, no quoting (module docstring).

    skip=2 drops the comment line and the display-title header. A row with the
    wrong number of cells fails in DuckDB's reader rather than shifting columns.
    """
    columns = "{" + ", ".join(f"'{f.replace('.', '_')}': 'VARCHAR'" for f in fields) + "}"
    return (f"(SELECT *, '{retrieval_date}' AS retrieval_date FROM read_csv('{path}', "
            f"delim='\\t', quote='', escape='', header=false, skip=2, auto_detect=false, "
            f"nullstr='', columns={columns}))")


def _verify(path, fields, total=None):
    """The report's own date — after failing if it is not the report asked for, or is short.

    The comment line is '<UTC timestamp>\\t<request URL>': the URL's `field=` list,
    in order, is what makes the positional read safe, and the timestamp's date is
    the retrieval date, true even for a report downloaded earlier. `total` is the
    search API's count; fewer rows than that is a truncated download. (More is a
    record released mid-download.)
    """
    with open(path, encoding="utf-8") as f:
        stamp, _, url = f.readline().rstrip("\r\n").partition("\t")
    echoed = tuple(urllib.parse.parse_qs(urllib.parse.urlparse(url).query).get("field", ()))
    if echoed != tuple(fields):
        raise SystemExit(f"encode: {path} does not echo the requested fields; differs in "
                         f"{sorted(set(echoed) ^ set(fields)) or 'order'}")
    n = duckdb.sql(f"SELECT count(*) FROM {_source(path, fields, '')}").fetchone()[0]
    if total is not None and n < total:
        raise SystemExit(f"encode: {path} has {n:,} rows but the portal reports {total:,}; "
                         "short download, nothing landed")
    return str(datetime.strptime(stamp[:10], "%Y-%m-%d").date())


def land_raw(cat, release, experiments=None, files=None):
    """Phase 1: both reports, verbatim and whole, latest crawl only.

    `experiments` / `files` are already-downloaded report.tsv files: they skip
    the download and the `total` check (how offline tests stay offline).
    """
    scratch = Path(os.environ.get("BIOCONICE_SCRATCH", tempfile.gettempdir())) / "encode"
    counts = {}
    for type_, fields, local in (("Experiment", EXPERIMENT_FIELDS, experiments),
                                 ("File", FILE_FIELDS, files)):
        name = type_.lower()
        url = report_url(type_, fields)
        total = None
        facts = merge.reading(release, "encode", name, local or url)
        if not local:
            scratch.mkdir(parents=True, exist_ok=True)
            local = scratch / f"{name}.tsv"
            total = _total(type_)
            _download(url, local)
        retrieval_date = _verify(local, fields, total)
        n = _land(cat, release, f"raw.encode__{name}", _source(local, fields, retrieval_date))
        merge.manifest(cat, release, "encode", name, url, n, version=retrieval_date,
                       checksum=merge.sha256(local), **facts)
        counts[f"raw.encode__{name}"] = n
    return counts


def _ids(col, prefix=""):
    # Sorted and distinct, so the merge's attribute comparison is order-independent.
    return (f"list_sort(list_distinct(list_transform(str_split({col}, ','), "
            f"x -> '{prefix}' || x)))")


CHECKS = {
    "experiment resource_id is unique": "SELECT count(*) - count(DISTINCT resource_id) FROM experiment",
    "file resource_id is unique":       "SELECT count(*) - count(DISTINCT resource_id) FROM file",
}


def _check(con):
    failed = {name: con.sql(sql).fetchone()[0] for name, sql in CHECKS.items()}
    failed = {k: v for k, v in failed.items() if v}
    if failed:
        raise ValueError("encode: derived rows violate "
                         + "; ".join(f"{k} ({v:,} rows)" for k, v in failed.items()))


def transform(cat, release):
    """Phase 2: one resource row per experiment and per file, plus their relationships."""
    con = duckdb.connect()
    for name in ("experiment", "file"):  # raw holds one crawl, so the whole table is the input
        con.register(f"raw_{name}", cat.load_table(f"raw.encode__{name}").scan().to_arrow())

    # taxon_id is a TRY_CAST: a mixed-species experiment lists two ids ('9606,10090')
    # and becomes NULL rather than a wrong single taxon — bedbase.py's rule.
    con.execute(f"""
        CREATE TABLE experiment AS
        SELECT 'encode:' || accession AS resource_id, accession, status, description,
               assay_term_id, assay_term_name, assay_title,
               biosample_ontology_term_id AS biosample_term_id,
               biosample_ontology_term_name AS biosample_term_name,
               biosample_ontology_classification AS biosample_classification,
               biosample_summary,
               TRY_CAST(replicates_library_biosample_organism_taxon_id AS INTEGER) AS taxon_id,
               replicates_library_biosample_organism_scientific_name AS organism,
               target_label, target_investigated_as,
               {_ids('target_genes_geneid')} AS target_gene_ids,
               control_type, lab_title AS lab, award_name AS award, award_project AS project,
               award_rfa AS rfa,
               {_ids('assembly')} AS assemblies,
               {_ids('dbxrefs')} AS dbxrefs,
               doi, date_submitted, date_released,
               '{BASE}/experiments/' || accession || '/' AS portal_uri,
               'ENCODE' AS provider
        FROM raw_experiment
    """)

    # dataset is an object path, '/experiments/ENCSR…/' or '/annotations/ENCSR…/':
    # its two segments are the dataset's type and accession. A file is keyed on
    # `title`, not `accession`: 1,283 files (2026-09-18: SRA runs, named reference
    # files) have only an external accession, and title is what the portal itself
    # keys them by — the segment their own path and others' derived_from carry.
    con.execute(f"""
        CREATE TABLE file AS
        SELECT 'encode:' || title AS resource_id, title AS accession,
               'encode:' || split_part(dataset, '/', 3) AS dataset_id,
               split_part(dataset, '/', 2) AS dataset_type,
               status, file_format, file_format_type, file_type, output_type, output_category,
               assembly, genome_annotation,
               file_size::BIGINT AS size, md5sum, content_md5sum,
               -- href is set even where ENCODE hosts no object; a URI that cannot resolve is not one
               CASE WHEN no_file_available = 'True' THEN NULL ELSE '{BASE}' || href END AS https_uri,
               cloud_metadata_url AS cloud_uri,
               s3_uri,
               no_file_available = 'True' AS no_file_available,
               restricted = 'True' AS restricted,
               preferred_default = 'True' AS preferred_default,
               list_sort(list_distinct(list_transform(str_split(derived_from, ','),
                   x -> 'encode:' || split_part(x, '/', 3)))) AS derived_from,
               lab_title AS lab, date_created, 'ENCODE' AS provider
        FROM raw_file
    """)
    _check(con)

    # The joinable form. Targets keep the spelling their own table uses: ontology
    # CURIEs as ontology.term.term_id writes them, 'encode:' accessions as BEDbase's
    # rows and resource_id here write them, Entrez ids under Bioregistry's 'ncbigene'.
    rel = con.sql("""
        SELECT DISTINCT * FROM (
            SELECT resource_id, 'part_of_dataset' AS relationship, dataset_id AS target_id,
                   'encode' AS source FROM file WHERE dataset_id IS NOT NULL
            UNION ALL
            SELECT resource_id, 'has_biosample', biosample_term_id, 'encode'
            FROM experiment WHERE biosample_term_id IS NOT NULL
            UNION ALL
            SELECT resource_id, 'has_target_gene', 'ncbigene:' || unnest(target_gene_ids), 'encode'
            FROM experiment)
    """).to_arrow_table()
    return {
        "resource.encode__experiment": merge.merge(
            cat, "resource.encode__experiment", con.sql("SELECT * FROM experiment").to_arrow_table(),
            release, AlwaysTrue()),
        "resource.encode__file": merge.merge(
            cat, "resource.encode__file", con.sql("SELECT * FROM file").to_arrow_table(),
            release, AlwaysTrue()),
        "resource.resource_relationship": merge.merge(
            cat, "resource.resource_relationship", rel, release, EqualTo("source", "encode")),
    }


def ingest(cat, release, experiments=None, files=None):
    return {**land_raw(cat, release, experiments, files), **transform(cat, release)}
