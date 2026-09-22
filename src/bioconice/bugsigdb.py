"""BugSigDB exports -> Iceberg: land full_dump, derive the signature <-> taxon bridge.

BugSigDB is manually curated microbial signatures: per publication, a contrast
between two groups of subjects, and the taxa that were differentially abundant in
one of them. `full_dump.csv` is the canonical export, flattened across study,
experiment and signature — 7,425 rows at v1.3.1, so this needs none of the
streaming machinery the NCBI dumps do.

**Versioned by release tag, not retrieval date.** The repo re-exports from
bugsigdb.org every hour, so `devel` is a moving target; the tagged releases are
the manually-reviewed ones, each archived under a Zenodo DOI. Landing from a tag
is what makes raw immutable and idempotent per version, which is what SPEC asks
of a source that has real releases. The file's own banner timestamp is landed
alongside as in-band provenance, so a landing taken from `devel` would still be
able to say when it was taken.

The column contract differs from `ncbi.py` on purpose. NCBI's dumps are
positional, so those readers declare every column explicitly and turn
`auto_detect` off. Here the CSV *header* is the contract, so the header is
trusted and the SELECT names each column: an upstream rename or removal then
fails loudly in DuckDB's binder, rather than silently shifting every value one
column to the left.

**Two read paths (ADR-0012 pilot, #114).** The GitHub CSV is the original; the
lake path reads cdsci-lake's already-curated `lake.bugsigdb.signatures` through
its own `lake_connect(read_only=True)` and renders the handful of typed columns
back to the CSV's strings, so raw is byte-identical whichever path landed it.
Selected by `land_raw(lake=...)` / `--from-lake`; the CSV stays the fallback.
The write side (`merge.write`, PyIceberg) is untouched either way.

ponytail: only `full_dump.csv` is landed, not the twelve `*.gmt` files. Those are
re-renderings of the two member-list columns at fixed taxonomic ranks and ID
types. The `mixed` ones are derivable from what we land; the `genus`/`species`
ones additionally encode a taxonomic rollup that needs NCBI Taxonomy (#18) to
reproduce. Land them if that rollup turns out to be wanted before #18 arrives.
"""

import re
import urllib.request

import duckdb
from pyiceberg.expressions import AlwaysTrue, EqualTo

from . import merge


REPO = "https://raw.githubusercontent.com/waldronlab/bugsigdbexports"
DEFAULT_VERSION = "v1.3.1"
RAW = "raw.bugsigdb__full_dump"
LAKE_TABLE = "lake.bugsigdb.signatures"

# Upstream header -> our column name. Snake-cased throughout; `Source` becomes
# `source_in_paper` because `source` means "the asserting authority" everywhere
# else in this catalog, and here it means "Table 2".
COLUMNS = {
    "BSDB ID": "bsdb_id",
    "Study": "study",
    "Study design": "study_design",
    "PMID": "pmid",
    "DOI": "doi",
    "URL": "url",
    "Authors list": "authors_list",
    "Title": "title",
    "Journal": "journal",
    "Year": "year",
    "Keywords": "keywords",
    "Experiment": "experiment",
    "Location of subjects": "location_of_subjects",
    "Host species": "host_species",
    "Body site": "body_site",
    "UBERON ID": "uberon_id",
    "Condition": "condition",
    "EFO ID": "efo_id",
    "Group 0 name": "group_0_name",
    "Group 1 name": "group_1_name",
    "Group 1 definition": "group_1_definition",
    "Group 0 sample size": "group_0_sample_size",
    "Group 1 sample size": "group_1_sample_size",
    "Antibiotics exclusion": "antibiotics_exclusion",
    "Sequencing type": "sequencing_type",
    "16S variable region": "variable_region_16s",
    "Sequencing platform": "sequencing_platform",
    "Data transformation": "data_transformation",
    "Statistical test": "statistical_test",
    "Significance threshold": "significance_threshold",
    "MHT correction": "mht_correction",
    "LDA Score above": "lda_score_above",
    "Matched on": "matched_on",
    "Confounders controlled for": "confounders_controlled_for",
    "Pielou": "pielou",
    "Shannon": "shannon",
    "Chao1": "chao1",
    "Simpson": "simpson",
    "Inverse Simpson": "inverse_simpson",
    "Richness": "richness",
    "Signature page name": "signature_page_name",
    "Source": "source_in_paper",
    "Curated date": "curated_date",
    "Curator": "curator",
    "Revision editor": "revision_editor",
    "Description": "description",
    "Abundance in Group 1": "abundance_in_group_1",
    "MetaPhlAn taxon names": "metaphlan_taxon_names",
    "NCBI Taxonomy IDs": "ncbi_taxonomy_ids",
    "State": "state",
    "Reviewer": "reviewer",
}


def dump_url(version):
    return f"{REPO}/{version}/full_dump.csv"


def _exported_at(url):
    """The export's self-declared timestamp, from the CSV's banner line.

    The first line is `# BugSigDB 2026-04-24_00:41_UTC, License: ..., URL: ...`,
    which is also where the licence is asserted in-band. Only that line is read,
    not the whole file.
    """
    opener = urllib.request.urlopen if url.startswith("http") else open
    with opener(url) as r:
        first = r.readline()
    if isinstance(first, bytes):
        first = first.decode("utf-8", "replace")
    m = re.match(r"#\s*BugSigDB\s+([^,\s]+)", first)
    return m.group(1) if m else None


def _from_csv(url, version, release):
    exported = _exported_at(url)
    con = duckdb.connect()
    select = ",\n               ".join(f'"{src}" AS {dst}' for src, dst in COLUMNS.items())
    # The dialect is stated rather than sniffed: free-text columns carry commas,
    # quotes and newlines, and a sniffer that guesses differently between two
    # releases would shift values silently. all_varchar keeps raw unparsed.
    # nullstr='NA' is BugSigDB's missing marker, treated like NCBI's '-'. skip=1
    # drops the banner line so the real header is read as the header.
    return con.sql(f"""
        SELECT {select},
               {f"'{exported}'" if exported else 'NULL::VARCHAR'} AS export_timestamp,
               '{version}' AS bugsigdb_version,
               '{release}' AS landed_in
        FROM read_csv('{url}', skip=1, header=true, all_varchar=true, nullstr='NA',
                      delim=',', quote='"', escape='"')
    """).to_arrow_table()


# cdsci-lake types five columns on ingest (sources/bugsigdb/ingest.py `_TYPED`); raw
# here is all-varchar, so they are rendered back to the CSV's text. Two documented
# divergences from the CSV path, shared by all five: a value TRY_CAST could not parse
# ('NR', 'n.d.') is NULL where the CSV kept the text, and a parseable one is
# re-rendered canonically ('05 January 2021' -> '5 January 2021', '0012345' -> '12345').
# test_bugsigdb.py pins both; the raw table comment in schemas.py states them.
_UNTYPED = {
    "pmid": "pmid::VARCHAR",
    "year": "year::VARCHAR",
    "group_0_sample_size": "group_0_sample_size::VARCHAR",
    "group_1_sample_size": "group_1_sample_size::VARCHAR",
    "curated_date": "strftime(curated_date, '%-d %B %Y')",
}


def _from_lake(settings, version, release):
    """The same rows from cdsci-lake's curated table, filtered to one release tag.

    `lake.bugsigdb.signatures` is cdsci's upsert_latest_snapshot silver table,
    keyed on bsdb_id: a row carries the tag it was last seen in, so the table holds
    exactly one tag's complete dump only while `version` is the newest tag loaded
    there. Once cdsci has moved on, a `bugsigdb_version = version` filter returns
    zero or a partial set, and writing that would replace the tag's scope in raw
    with nothing — so this refuses unless every row in the table carries the tag.
    Older tags need DuckLake time travel on the cdsci side (cdsci-lake#103); that
    is a shared-contract gap, not something to paper over here.
    """
    from cdsci.lake import lake_connect  # dev-only: `uv pip install -e ../cdsci-lake`

    con = lake_connect(None if settings is True else settings, read_only=True)
    try:
        total, tagged = con.execute(f"""
            SELECT count(*), count(*) FILTER (WHERE bugsigdb_version = ?) FROM {LAKE_TABLE}
        """, [version]).fetchone()
        if not total or tagged != total:
            raise ValueError(f"{LAKE_TABLE}: {tagged} of {total} rows carry {version}; the lake "
                             f"holds one tag at a time, so this is not that tag's complete dump "
                             f"(cdsci-lake#103). Land it from the CSV instead.")
        select = ",\n               ".join(f"{_UNTYPED.get(c, c)} AS {c}" for c in COLUMNS.values())
        return con.execute(f"""
            SELECT {select},
                   export_timestamp,
                   bugsigdb_version,
                   '{release}' AS landed_in
            FROM {LAKE_TABLE}
            WHERE bugsigdb_version = ?
        """, [version]).to_arrow_table()
    finally:
        con.close()


def land_raw(cat, release, version=DEFAULT_VERSION, url=None, lake=None):
    """Phase 1: full_dump.csv, verbatim, for one release tag.

    Replaced wholesale for its `bugsigdb_version`, so re-landing a tag is
    idempotent and landing a new tag accumulates alongside the old one.

    `lake` selects the ADR-0012 read path: `True` reads cdsci-lake as configured
    by its own environment (`CU_OPENALEX_LAKE_BACKEND` etc.), a
    `cdsci.lake.Settings` reads that lake (tests), `None` reads the CSV at `url`.
    """
    url = LAKE_TABLE if lake else (url or dump_url(version))
    facts = merge.reading(release, "bugsigdb", "full_dump", url)   # before the read (merge.reading)
    arrow = _from_lake(lake, version, release) if lake else _from_csv(url, version, release)

    n = merge.write(cat, RAW, arrow, EqualTo("bugsigdb_version", version))
    # release_number: BugSigDB publishes real, citable release tags.
    merge.manifest(cat, release, "bugsigdb", "full_dump", url, n, version=version, method="release_number",
                   **facts)
    return n


# Verbatim body of cdsci-lake's transform/models/bugsigdb/signature_taxon.sql, with
# its FROM pointed at the raw rows registered as `raw`. cdsci does not ship the model
# in its package, so the text lives here too; test_bugsigdb.py runs both over one
# fixture and fails if they drift — but only where the sibling checkout exists (the
# test skips elsewhere, CI included), so drift is caught on a developer's machine, not
# by the pipeline. cdsci-lake#103 asks cdsci to ship the model SQL or the gold table.
# ponytail: no rank rollup (needs NCBI Taxonomy, #18).
EXPLODE = """
WITH lists AS (
    SELECT
        bsdb_id,
        string_split(metaphlan_taxon_names, ',') AS taxa,
        string_split(ncbi_taxonomy_ids, ';') AS taxids
    FROM raw
    WHERE metaphlan_taxon_names IS NOT NULL AND ncbi_taxonomy_ids IS NOT NULL
),
members AS (
    SELECT
        bsdb_id,
        ord AS member_index,
        trim(taxon_lineage) AS taxon_lineage,
        trim(taxids[ord]) AS taxon_lineage_ids,
        list_extract(string_split(trim(taxon_lineage), '|'), -1) AS leaf_raw
    FROM lists, UNNEST(taxa) WITH ORDINALITY AS u(taxon_lineage, ord)
    WHERE trim(taxon_lineage) <> ''
)
SELECT
    bsdb_id,
    member_index,
    regexp_extract(leaf_raw, '^([a-z])__', 1) AS taxon_rank,
    regexp_replace(leaf_raw, '^[a-z]__', '') AS taxon_name,
    TRY_CAST(list_extract(string_split(taxon_lineage_ids, '|'), -1) AS BIGINT) AS ncbitaxon_id,
    taxon_lineage,
    taxon_lineage_ids
FROM members
"""


def transform(cat, release, version=DEFAULT_VERSION):
    """Phase 2: one row per signature member, merged with release history.

    One tag is one complete state of every signature, and this is the only
    writer, so the scope is the whole table (scd2_release, ADR-0004/0006). With
    several tags landed in raw, `transform(tag)` retires any member unique to
    the other tags: the last run wins, which is right for a single writer
    deriving one tag per release.
    """
    con = duckdb.connect()
    con.register("raw", cat.load_table(RAW).scan(
        row_filter=EqualTo("bugsigdb_version", version),
        selected_fields=("bsdb_id", "metaphlan_taxon_names", "ncbi_taxonomy_ids")).to_arrow())
    rows = con.sql(EXPLODE).to_arrow_table()
    con.close()
    return {"annotation.signature_taxon": merge.merge(
        cat, "annotation.signature_taxon", rows, release, AlwaysTrue())}


def ingest(cat, release, version=DEFAULT_VERSION, url=None, lake=None):
    n = land_raw(cat, release, version, url, lake)
    return {f"{RAW} [{version}]": n, **transform(cat, release, version)}
