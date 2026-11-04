"""NIH iCite -> Iceberg: bibliometrics for every PubMed record.

iCite publishes a complete database snapshot monthly on NIH Figshare
(collection 4586573, CC BY 4.0): one zipped CSV, ~40M rows, one per PMID, with
the paper's identifiers, the influence metrics (RCR, NIH percentile, citation
counts), the translation scores (human/animal/molecular-cellular, APT) and the
space-separated `cited_by` / `references` PMID lists. It is the versioned,
whole-dump case: the snapshot label ("2026-08") is the source version.

Raw is landed **whole** — every column, the citation lists included — but as
the *latest* snapshot only: a snapshot is 40M rows dominated by those lists,
older snapshots stay immutable and citable on Figshare, and every snapshot's
metrics survive in the derived table. That is a deliberate narrowing of the
raw layer's accumulate-per-version rule, stated in the table comment.

Two derived tables, because the columns change at two different rates:

  annotation.icite__publication  keyed by pmid — what the paper *is*
                                 (doi, title, authors, journal, year, flags).
                                 Type 2 like every other annotation table;
                                 these rarely change, so history stays small.
  annotation.icite__citation     keyed by (citing_pmid, cited_pmid) — the graph
                                 itself, exploded from references, merged in 16
                                 shards of cited_pmid so ~930M edges never sit in
                                 memory at once.
  annotation.icite__metrics      keyed by (pmid, snapshot) — how it is *cited*
                                 as of one snapshot. Citation counts move for
                                 nearly every paper every month; as Type 2
                                 attributes that would open 40M version rows
                                 per snapshot and rewrite them all each merge.
                                 Keyed by snapshot they are facts that never
                                 change, and a snapshot is a merge scope.

Flags arrive as 'True'/'False' text and are read by name, not position, with every
column as text first: a reordered or renamed upstream column then fails loudly
rather than shifting values.
"""

import os
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import duckdb
from pyiceberg.expressions import AlwaysTrue, EqualTo

from cdsci.lake.sources.icite.ingest import ADDED

from . import merge
from .ncbi import _land

FIGSHARE = "https://api.figshare.com/v2"
COLLECTION = 4586573

# Read by name from the CSV header. Text first; typed in the derivation.
COLUMNS = (
    "pmid", "doi", "year", "title", "authors", "journal", "is_research_article",
    "relative_citation_ratio", "nih_percentile", "human", "animal", "molecular_cellular",
    "apt", "is_clinical", "citation_count", "citations_per_year",
    "expected_citations_per_year", "field_citation_rate", "provisional",
    "x_coord", "y_coord", "cited_by_clin", "cited_by", "references", "last_modified",
)


def _json(url):
    import json
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def resolve(snapshot=None):
    """The (snapshot label, metadata-zip url) of one Figshare snapshot; latest by default."""
    arts = _json(f"{FIGSHARE}/collections/{COLLECTION}/articles"
                 "?page_size=100&order=published_date&order_direction=desc")
    if snapshot:
        arts = [a for a in arts if a["title"].endswith(snapshot)]
    if not arts:
        raise SystemExit(f"no iCite snapshot {snapshot!r} in Figshare collection {COLLECTION}")
    art = arts[0]
    label = art["title"].split()[-1]
    files = _json(f"{FIGSHARE}/articles/{art['id']}")["files"]
    zips = [f for f in files if f["name"] == "icite_metadata.zip"]
    if not zips:
        raise SystemExit(f"snapshot {label}: no icite_metadata.zip among {[f['name'] for f in files]}")
    return label, zips[0]["download_url"], zips[0]["size"]


def fetch(label, url, size):
    """Download and extract the snapshot CSV, once: re-running reuses what is on disk.

    Returns (the zip, the CSV): the zip is what was retrieved, so it is what is hashed.

    The zip is ~14 GB and the CSV ~40 GB, so they go under BIOCONICE_SCRATCH
    (default: the system temp dir) rather than through memory.
    """
    scratch = Path(os.environ.get("BIOCONICE_SCRATCH", tempfile.gettempdir())) / "icite" / label
    scratch.mkdir(parents=True, exist_ok=True)
    zpath = scratch / "icite_metadata.zip"
    if not (zpath.exists() and zpath.stat().st_size == size):
        urllib.request.urlretrieve(url, zpath)
    with zipfile.ZipFile(zpath) as z:
        names = [n for n in z.namelist() if n.endswith(".csv")]
        if len(names) != 1:
            raise SystemExit(f"{zpath}: expected one CSV, found {names}")
        csv = scratch / names[0]
        if not csv.exists():
            z.extract(names[0], scratch)
    return zpath, csv


def _read(csv):
    cols = ", ".join(f'"{c}"' for c in COLUMNS)  # "references" is a reserved word
    # all_varchar keeps raw unparsed and makes the read name-based. The dialect
    # is stated, not sniffed: titles carry commas, quotes and line breaks.
    # max_line_size because cited_by is the whole citing-PMID list on one line —
    # 2.3 MB for the most-cited paper in the 2026-08 snapshot, over DuckDB's 2 MB
    # default. Every row in that snapshot had all 25 columns, so no null_padding
    # (which would also force the single-threaded scanner alongside quoted newlines).
    return (f"(SELECT {cols} FROM read_csv('{csv}', header=true, all_varchar=true, "
            f"quote='\"', escape='\"', sample_size=-1, max_line_size=268435456))")


def land_raw(cat, release, snapshot=None, csv=None):
    """Phase 1: the snapshot CSV, verbatim and whole, replacing the previous snapshot."""
    checksum = None
    if csv:
        label, url = snapshot or "local", str(csv)
    else:
        label, url, size = resolve(snapshot)
    facts = merge.reading(release, "icite", "metadata", url)
    if not csv:
        zpath, csv = fetch(label, url, size)
        checksum = merge.sha256(zpath)   # 14 GB: about a minute, against an hours-long landing
    n = _land(cat, release, "raw.icite__metadata",
              f"(SELECT *, '{label}' AS snapshot FROM {_read(csv)})")
    merge.manifest(cat, release, "icite", "metadata", url, n, version=label, method="release_number",
                   checksum=checksum, **facts)
    return label, n


def _flag(col):
    # The snapshot CSV prints 'True'/'False' where the API says 'Yes'/'No'. Loud
    # on anything else: a third value would otherwise become NULL quietly.
    return (f"CASE WHEN {col} IS NULL THEN NULL WHEN {col} IN ('True', 'Yes') THEN true "
            f"WHEN {col} IN ('False', 'No') THEN false "
            f"ELSE error('{col}: unexpected ' || {col}) END")


# Normalisation rules, decided from the 2026-08 snapshot (41.0M rows):
#   doi    3.9M upper-cased, 443 with stray whitespace, one with an https://doi.org/
#          prefix, 6,414 values that are not DOIs at all ('0161901/AIM.003'). A DOI
#          is case-insensitive and always '10.<registrant>/<suffix>', so: trim,
#          lower-case, strip a resolver or 'doi:' prefix, and NULL anything that
#          still does not look like one. Raw keeps the original.
#   title  7,774 with leading/trailing whitespace; authors and journal had none.
#   year   1800-2028 are all real: PMC digitised 18th/19th-century journals, and
#          ahead-of-print records carry next year's date. No floor is imposed.
DOI = ("NULLIF(regexp_extract(lower(trim(doi)), "
       "'^(?:https?://(?:dx\\.)?doi\\.org/|doi:\\s*)?(10\\.[0-9]{4,9}/.+)$', 1), '')")

# Assertions on the derived rows: each SQL counts violations, and one violation
# fails the ingest before anything is written. These are the invariants the
# tables' docs promise; a snapshot that breaks one needs a person, not a merge.
CHECKS = {
    "pmid is numeric":            "SELECT count(*) FROM pub WHERE TRY_CAST(pmid AS BIGINT) IS NULL",
    "pmid is unique":             "SELECT count(*) - count(DISTINCT pmid) FROM pub",
    "year is plausible":          "SELECT count(*) FROM pub WHERE year NOT BETWEEN 1600 AND 2100",
    "doi is a normalised DOI":    "SELECT count(*) FROM pub WHERE doi IS NOT NULL AND (doi <> lower(doi) OR doi NOT LIKE '10.%/%')",
    "text has no edge whitespace": "SELECT count(*) FROM pub WHERE title <> trim(title) OR authors <> trim(authors) OR journal <> trim(journal)",
    "text has no empty strings":  "SELECT count(*) FROM pub WHERE '' IN (title, authors, journal, doi)",
}


def _check(con):
    failed = {name: con.sql(sql).fetchone()[0] for name, sql in CHECKS.items()}
    failed = {k: v for k, v in failed.items() if v}
    if failed:
        raise ValueError("icite: derived rows violate " + "; ".join(f"{k} ({v:,} rows)" for k, v in failed.items()))


# Read from cdsci-lake (#183 H1): lake.icite.metadata and lake.icite.citation, typed as the
# lake types them. The schema drifts from the Iceberg tables', owner-approved: pmid is
# BIGINT, raw is typed, relative_citation_ratio is rcr and snapshot is snapshot_version (the
# load in which a row's values last changed). icite__publication keeps the lander's DOI and
# whitespace normalisation, applied to the lake's lower-cased doi.
_LAKE = "lake.icite.metadata"
# raw.metadata: lake.icite.metadata's original 17 columns, without those added for #183 H1.
METADATA_17 = f"SELECT * EXCLUDE ({', '.join(f'\"{c}\"' for c in ADDED)}) FROM {_LAKE}"
RELEASE = {
    "raw.icite__metadata": f"SELECT * FROM {_LAKE}",
    "annotation.icite__publication": f"""
        SELECT pmid, {DOI} AS doi, NULLIF(trim(title), '') AS title,
               NULLIF(trim(authors), '') AS authors, NULLIF(trim(journal), '') AS journal,
               year, is_research_article, is_clinical
        FROM {_LAKE}""",
    "annotation.icite__metrics": f"""
        SELECT pmid, snapshot_version, rcr, nih_percentile, citation_count, citations_per_year,
               expected_citations_per_year, field_citation_rate, human, animal,
               molecular_cellular, apt, provisional
        FROM {_LAKE}""",
    "annotation.icite__citation": "SELECT citing_pmid, cited_pmid, shard FROM lake.icite.citation",
}
# The lander's current rows in RELEASE's shape, for the by-value equivalence (#183 H1): {}
# is the Iceberg table. snapshot_version equals the lander's snapshot only for a lake
# loaded once, at that snapshot.
_LIST = "TRY_CAST(string_split(NULLIF({0}, ''), ' ') AS BIGINT[]) AS {0}"
AS_PUBLISHED = {
    "raw.icite__metadata": (
        "SELECT pmid::BIGINT AS pmid, NULLIF(lower(doi), '') AS doi, year::INTEGER AS year, "
        "title, journal, relative_citation_ratio::DOUBLE AS rcr, "
        + "".join(f"{c}::{t} AS {c}, " for c, t in (
            ("nih_percentile", "DOUBLE"), ("citation_count", "BIGINT"),
            ("citations_per_year", "DOUBLE"), ("expected_citations_per_year", "DOUBLE"),
            ("field_citation_rate", "DOUBLE")))
        + f"{_flag('is_research_article')} AS is_research_article, "
          f"{_flag('is_clinical')} AS is_clinical, apt::DOUBLE AS apt, human::DOUBLE AS human, "
          "animal::DOUBLE AS animal, snapshot AS snapshot_version, NULLIF(authors, '') AS authors, "
          f"molecular_cellular::DOUBLE AS molecular_cellular, {_flag('provisional')} AS provisional, "
          "x_coord::DOUBLE AS x_coord, y_coord::DOUBLE AS y_coord, "
        + ", ".join(_LIST.format(c) for c in ("cited_by_clin", "cited_by", '"references"'))
        + ", regexp_replace(last_modified, '\\[.*\\]$', '')::TIMESTAMP AS last_modified FROM {}"),
    "annotation.icite__publication":
        "SELECT pmid::BIGINT AS pmid, doi, title, authors, journal, year, is_research_article, "
        "is_clinical FROM {}",
    "annotation.icite__metrics":
        "SELECT pmid::BIGINT AS pmid, snapshot AS snapshot_version, relative_citation_ratio AS rcr, "
        "nih_percentile, citation_count, citations_per_year, expected_citations_per_year, "
        "field_citation_rate, human, animal, molecular_cellular, apt, provisional FROM {}",
    "annotation.icite__citation":
        "SELECT citing_pmid::BIGINT AS citing_pmid, cited_pmid::BIGINT AS cited_pmid, shard FROM {}",
}
_SNAPSHOT_VERSION = ("iCite snapshot label, e.g. '2026-08', of the load in which this row's "
                     "values last changed.")
_PMIDS = "PMIDs, as a list; NULL when iCite lists none."
# The published columns, in order, as (name, arrow type, doc); None takes the declared doc
# of the same name. The comments replace declarations that no longer describe the release.
PUBLISHED = {
    "raw.icite__metadata": (
        "The NIH iCite database snapshot, every column typed, the citation lists included, "
        "from cdsci-lake's lake.icite.metadata (pruned to the latest snapshot). CC BY 4.0; "
        "cite iCite.", (
            ("pmid", "int64", "PubMed id."),
            ("doi", "string", "DOI as iCite prints it, lower-cased; NULL when blank. "
                              "annotation.icite__publication.doi is normalised."),
            ("year", "int32", None), ("title", "string", None), ("journal", "string", None),
            ("rcr", "double", "RCR: field- and time-normalised citation rate, NIH R01 "
                              "papers = 1.0."),
            ("nih_percentile", "double", None), ("citation_count", "int64", None),
            ("citations_per_year", "double", None),
            ("expected_citations_per_year", "double", None),
            ("field_citation_rate", "double", None),
            ("is_research_article", "bool", "Publication types consistent with primary "
                                            "research, per iCite."),
            ("is_clinical", "bool", "The article itself is clinical, per iCite."),
            ("apt", "double", None), ("human", "double", None), ("animal", "double", None),
            ("snapshot_version", "string", _SNAPSHOT_VERSION),
            ("authors", "string", None), ("molecular_cellular", "double", None),
            ("provisional", "bool", "RCR is provisional (paper under two years old)."),
            ("x_coord", "double", None), ("y_coord", "double", None),
            ("cited_by_clin", "list<int64>", "Clinical articles citing this one: " + _PMIDS),
            ("cited_by", "list<int64>", "Articles citing this one: " + _PMIDS),
            ("references", "list<int64>", "Articles this one cites: " + _PMIDS),
            ("last_modified", "timestamp[us]", "Upstream last-modified time of the record, "
                                               "UTC."))),
    "annotation.icite__publication": (None, (
        ("pmid", "int64", None), ("doi", "string", None), ("title", "string", None),
        ("authors", "string", None), ("journal", "string", None), ("year", "int32", None),
        ("is_research_article", "bool", None), ("is_clinical", "bool", None))),
    "annotation.icite__metrics": (
        "iCite's citation metrics for one paper as of the latest monthly snapshot, keyed by "
        "pmid; snapshot_version is the snapshot in which the row's values last changed. "
        "Source: NIH iCite, CC BY 4.0.", (
            ("pmid", "int64", None), ("snapshot_version", "string", _SNAPSHOT_VERSION),
            ("rcr", "double", "RCR: field- and time-normalised citation rate; NIH R01-funded "
                              "papers average 1.0."),
            ("nih_percentile", "double", None), ("citation_count", "int64", None),
            ("citations_per_year", "double", None),
            ("expected_citations_per_year", "double", None),
            ("field_citation_rate", "double", None), ("human", "double", None),
            ("animal", "double", None), ("molecular_cellular", "double", None),
            ("apt", "double", None), ("provisional", "bool", None))),
    "annotation.icite__citation": (None, (
        ("citing_pmid", "int64", None), ("cited_pmid", "int64", None),
        ("shard", "int32", None))),
}


def published_docs(name, declared):
    """(table comment, {column: doc}) of a PUBLISHED table, given its declaration."""
    comment, columns = PUBLISHED[name]
    docs = {f.name: f.doc for f in declared.schema.fields}
    return comment or declared.comment, {c: doc or docs[c] for c, _, doc in columns}


def transform(cat, release, snapshot):
    """Phase 2: the paper (Type 2, by pmid) and its metrics (by pmid and snapshot).

    Normalises (DOI, whitespace) and asserts the invariants in CHECKS before
    any merge runs, so a bad snapshot fails loudly and writes nothing.
    """
    con = duckdb.connect()
    # Only the columns derived here: the cited_by/references lists are most of
    # the file's 30 GB and nothing below reads them.
    con.register("raw", cat.load_table("raw.icite__metadata").scan(
        row_filter=EqualTo("snapshot", snapshot),
        selected_fields=("pmid", "snapshot", "doi", "title", "authors", "journal", "year",
                         "is_research_article", "is_clinical", "relative_citation_ratio",
                         "nih_percentile", "citation_count", "citations_per_year",
                         "expected_citations_per_year", "field_citation_rate", "human",
                         "animal", "molecular_cellular", "apt", "provisional")).to_arrow())

    con.execute(f"""
        CREATE TABLE pub AS
        SELECT pmid, {DOI} AS doi, NULLIF(trim(title), '') AS title,
               NULLIF(trim(authors), '') AS authors, NULLIF(trim(journal), '') AS journal,
               year::INTEGER AS year,
               {_flag('is_research_article')} AS is_research_article,
               {_flag('is_clinical')} AS is_clinical
        FROM raw
    """)
    _check(con)
    pub = con.sql("SELECT * FROM pub").to_arrow_table()

    metrics = con.sql(f"""
        SELECT pmid, snapshot,
               relative_citation_ratio::DOUBLE AS relative_citation_ratio,
               nih_percentile::DOUBLE AS nih_percentile,
               citation_count::BIGINT AS citation_count,
               citations_per_year::DOUBLE AS citations_per_year,
               expected_citations_per_year::DOUBLE AS expected_citations_per_year,
               field_citation_rate::DOUBLE AS field_citation_rate,
               human::DOUBLE AS human, animal::DOUBLE AS animal,
               molecular_cellular::DOUBLE AS molecular_cellular, apt::DOUBLE AS apt,
               {_flag('provisional')} AS provisional
        FROM raw
    """).to_arrow_table()

    return {
        "annotation.icite__publication": merge.merge(
            cat, "annotation.icite__publication", pub, release, AlwaysTrue()),
        "annotation.icite__metrics": merge.merge(
            cat, "annotation.icite__metrics", metrics, release, EqualTo("snapshot", snapshot)),
    }


SHARDS = 16


def transform_citations(cat, release, snapshot):
    """Phase 2b: the citation graph, one merge per shard of cited_pmid.

    `references` is the PMIDs each paper cites, space-separated. Exploded, the
    lists are exactly the NIH Open Citation Collection: 928,458,585 edges in
    the 2026-08 snapshot on both sides. (`cited_by` is the same graph from the
    other end but 68,932 edges short — the ones whose cited paper has no iCite
    record — so it is not used.) Exploded once into DuckDB, then merged SHARDS
    times with `cited_pmid % SHARDS` as the scope, so each merge holds ~1/16 of
    the graph; the shard is a partition column, so the scope scan prunes to it.
    """
    con = duckdb.connect()
    con.register("raw", cat.load_table("raw.icite__metadata").scan(
        row_filter=EqualTo("snapshot", snapshot),
        selected_fields=("pmid", "references")).to_arrow())
    con.execute(f"""
        CREATE TABLE edges AS
        SELECT DISTINCT pmid AS citing_pmid, cited_pmid,
               (cited_pmid::BIGINT % {SHARDS})::INTEGER AS shard
        FROM (SELECT pmid, unnest(str_split("references", ' ')) AS cited_pmid
              FROM raw WHERE "references" IS NOT NULL AND "references" <> '')
        WHERE cited_pmid <> ''
    """)
    con.unregister("raw")
    out = {}
    for shard in range(SHARDS):
        inc = con.sql(f"SELECT citing_pmid, cited_pmid, shard FROM edges WHERE shard = {shard}"
                      ).to_arrow_table()
        out[f"annotation.icite__citation [shard {shard}]"] = merge.merge(
            cat, "annotation.icite__citation", inc, release, EqualTo("shard", shard))
    return out


def ingest(cat, release, snapshot=None, csv=None):
    label, n = land_raw(cat, release, snapshot, csv)
    return {f"raw.icite__metadata [{label}]": n, **transform(cat, release, label),
            **transform_citations(cat, release, label)}
