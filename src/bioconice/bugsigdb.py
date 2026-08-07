"""BugSigDB exports -> Iceberg. Landing only; the munging is deliberately absent.

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

ponytail: only `full_dump.csv` is landed, not the twelve `*.gmt` files. Those are
re-renderings of the two member-list columns at fixed taxonomic ranks and ID
types. The `mixed` ones are derivable from what we land; the `genus`/`species`
ones additionally encode a taxonomic rollup that needs NCBI Taxonomy (#18) to
reproduce. Land them if that rollup turns out to be wanted before #18 arrives.
"""

import re
import urllib.request
from datetime import datetime, timezone

import duckdb
from pyiceberg.expressions import And, EqualTo

from .ensembl import _write

REPO = "https://raw.githubusercontent.com/waldronlab/bugsigdbexports"
DEFAULT_VERSION = "v1.3.1"

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


def land_raw(cat, release, version=DEFAULT_VERSION, url=None):
    """Phase 1: full_dump.csv, verbatim, for one release tag.

    Replaced wholesale for its `bugsigdb_version`, so re-landing a tag is
    idempotent and landing a new tag accumulates alongside the old one.
    """
    url = url or dump_url(version)
    exported = _exported_at(url)

    con = duckdb.connect()
    select = ",\n               ".join(f'"{src}" AS {dst}' for src, dst in COLUMNS.items())
    # The dialect is stated rather than sniffed: free-text columns carry commas,
    # quotes and newlines, and a sniffer that guesses differently between two
    # releases would shift values silently. all_varchar keeps raw unparsed.
    # nullstr='NA' is BugSigDB's missing marker, treated like NCBI's '-'. skip=1
    # drops the banner line so the real header is read as the header.
    arrow = con.sql(f"""
        SELECT {select},
               {f"'{exported}'" if exported else 'NULL::VARCHAR'} AS export_timestamp,
               '{version}' AS bugsigdb_version,
               '{release}' AS landed_in
        FROM read_csv('{url}', skip=1, header=true, all_varchar=true, nullstr='NA',
                      delim=',', quote='"', escape='"')
    """).to_arrow_table()

    n = _write(cat, "raw.bugsigdb__full_dump", arrow, EqualTo("bugsigdb_version", version))
    _manifest(cat, release, version, url, n)
    return n


def _manifest(cat, release, version, url, rows):
    """Record what this release was built from — ADR-0007.

    `release_number` rather than `retrieval_date`: BugSigDB publishes real,
    citable release tags, so recording a date here would discard the version the
    source itself uses.
    """
    con = duckdb.connect()
    arrow = con.sql(f"""
        SELECT '{release}' AS release, 'bugsigdb' AS source,
               '{version}' AS source_version, 'release_number' AS version_method,
               '{datetime.now(timezone.utc).isoformat(timespec="seconds")}' AS retrieved_at,
               '{url}' AS url, NULL::VARCHAR AS checksum, {rows}::BIGINT AS row_count
    """).to_arrow_table()
    _write(cat, "provenance.release", arrow,
           And(EqualTo("release", release), EqualTo("source", "bugsigdb")))
