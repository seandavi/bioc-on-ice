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
  annotation.icite__metrics      keyed by (pmid, snapshot) — how it is *cited*
                                 as of one snapshot. Citation counts move for
                                 nearly every paper every month; as Type 2
                                 attributes that would open 40M version rows
                                 per snapshot and rewrite them all each merge.
                                 Keyed by snapshot they are facts that never
                                 change, and a snapshot is a merge scope.

Flags arrive as 'Yes'/'No' text and are read by name, not position, with every
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

from . import merge
from .ncbi import _land, _manifest

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
    return csv


def _read(csv):
    cols = ", ".join(f'"{c}"' for c in COLUMNS)  # "references" is a reserved word
    # all_varchar keeps raw unparsed and makes the read name-based. The dialect
    # is stated, not sniffed: titles carry commas and quotes. null_padding
    # because some rows end early (omicidx hit this on the same file).
    return (f"(SELECT {cols} FROM read_csv('{csv}', header=true, all_varchar=true, "
            f"quote='\"', escape='\"', null_padding=true, sample_size=-1))")


def land_raw(cat, release, snapshot=None, csv=None):
    """Phase 1: the snapshot CSV, verbatim and whole, replacing the previous snapshot."""
    if csv:
        label, url = snapshot or "local", str(csv)
    else:
        label, url, size = resolve(snapshot)
        csv = fetch(label, url, size)
    n = _land(cat, release, "raw.icite__metadata",
              f"(SELECT *, '{label}' AS snapshot FROM {_read(csv)})")
    _manifest(cat, release, "icite", url, n, version=label, method="release_number")
    return label, n


def _flag(col):
    # Loud on a new vocabulary: a third value would otherwise become NULL quietly.
    return (f"CASE {col} WHEN 'Yes' THEN true WHEN 'No' THEN false "
            f"WHEN NULL THEN NULL ELSE error('{col}: unexpected ' || {col}) END")


def transform(cat, release, snapshot):
    """Phase 2: the paper (Type 2, by pmid) and its metrics (by pmid and snapshot)."""
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

    pub = con.sql(f"""
        SELECT pmid, NULLIF(doi, '') AS doi, NULLIF(title, '') AS title,
               NULLIF(authors, '') AS authors, NULLIF(journal, '') AS journal,
               year::INTEGER AS year,
               {_flag('is_research_article')} AS is_research_article,
               {_flag('is_clinical')} AS is_clinical
        FROM raw
    """).to_arrow_table()

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


def ingest(cat, release, snapshot=None, csv=None):
    label, n = land_raw(cat, release, snapshot, csv)
    return {f"raw.icite__metadata [{label}]": n, **transform(cat, release, label)}
