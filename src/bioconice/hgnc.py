"""HGNC complete set -> Iceberg, in the same two phases as the other sources.

HGNC is the authority for human gene symbols and names: one TSV, one row per
approved HGNC id (45,083 on 2026-09-18), CC0. It is small enough to read as one
Arrow table, so this needs none of the streaming machinery the NCBI dumps do.

**Versioned by date, and the date's provenance is recorded.** The rolling
`hgnc_complete_set.txt` is regenerated in place and carries no version in-band
(checked 2026-09-18: no banner line, and the only HTTP facts are Last-Modified
and an etag, which move with every regeneration), so by default the version is
the retrieval date, as for NCBI. HGNC also freezes dated copies under
`archive/{monthly,quarterly}/tsv/hgnc_complete_set_YYYY-MM-DD.txt`; landing
one of those through `url` takes the date from the file name and records it as
`release_number`, which is the citable form. Raw is replaced per version, so a
re-landing is idempotent and versions accumulate, like raw.obo__*.

The column contract is the header, checked whole. HGNC does change it: the
archives through 2026-08-07 carry a `location_sortable` column that the
2026-09-04 archive and the rolling file no longer have. A header that is not exactly COLUMNS therefore
fails before anything is read, rather than landing a file with a column
silently missing (or, with a positional spec, shifted). Landing an older
archive means declaring its columns first.

It is a further writer to `annotation.identifier_mapping`, under its own scope
(taxon 9606, source='HGNC'): NCBI and Ensembl assert HGNC ids too, and a scope
without the source would let each writer retire the others' rows (ADR-0004).

ponytail: `withdrawn.txt` is not landed. The complete set holds approved
records only — status is 'Approved' on every row — so withdrawn and merged ids
live in that second file, with their own columns. Land it as its own raw table
when something needs symbol succession.
"""

import re
import urllib.request
from datetime import datetime, timezone

import duckdb
from pyiceberg.expressions import AlwaysTrue, And, EqualTo

from . import merge

URL = "https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/hgnc_complete_set.txt"
TAXON = 9606

# The file's header, in file order. Two names are not valid bare column names
# ('pseudogene.org', 'mamit-trnadb'); raw spells them with underscores and the
# column docs record the upstream spelling.
COLUMNS = (
    "hgnc_id", "symbol", "name", "locus_group", "locus_type", "status", "location",
    "alias_symbol", "alias_name", "prev_symbol", "prev_name", "gene_group", "gene_group_id",
    "date_approved_reserved", "date_symbol_changed", "date_name_changed", "date_modified",
    "entrez_id", "ensembl_gene_id", "vega_id", "ucsc_id", "ena", "refseq_accession",
    "ccds_id", "uniprot_ids", "pubmed_id", "mgd_id", "rgd_id", "lsdb", "cosmic", "omim_id",
    "mirbase", "homeodb", "snornabase", "bioparadigms_slc", "orphanet", "pseudogene.org",
    "horde_id", "merops", "imgt", "iuphar", "kznf_gene_catalog", "mamit-trnadb", "cd",
    "lncrnadb", "enzyme_id", "intermediate_filament_db", "rna_central_id", "lncipedia",
    "gtrnadb", "agr", "mane_select", "gencc",
)


def _header(url):
    """The file's first line, split. Only that line is read, not the whole file."""
    opener = urllib.request.urlopen if url.startswith("http") else open
    with opener(url) as r:
        first = r.readline()
    if isinstance(first, bytes):
        first = first.decode("utf-8", "replace")
    return tuple(first.rstrip("\r\n").split("\t"))


def land_raw(cat, release, url=None):
    """Phase 1: the complete set, verbatim and whole, replaced per version.

    Returns (version, rows). `url` is a dated archive file, or a local copy.
    """
    url = url or URL
    if (header := _header(url)) != COLUMNS:
        raise SystemExit(f"hgnc: {url} header is not the declared one; "
                         f"differs in {sorted(set(header) ^ set(COLUMNS))}")
    # A dated archive names its own version; the rolling file has none.
    dated = re.search(r"hgnc_complete_set_(\d{4}-\d{2}-\d{2})\.", url)
    version = dated.group(1) if dated else str(datetime.now(timezone.utc).date())

    facts = merge.reading(release, "hgnc", "complete_set", url)
    con = duckdb.connect()
    select = ", ".join(f'"{c}" AS {c.replace(".", "_").replace("-", "_")}' for c in COLUMNS)
    # The dialect is stated rather than sniffed. HGNC quotes the cells that hold
    # a '|'-separated list and nothing else; the quotes are the dialect, the
    # pipes are the value and stay. An empty cell is HGNC's missing marker and
    # reads as NULL, the treatment NCBI's '-' gets. all_varchar keeps raw unparsed.
    arrow = con.sql(f"""
        SELECT {select}, '{version}' AS hgnc_version, '{release}' AS landed_in
        FROM read_csv('{url}', header=true, all_varchar=true, delim='\\t', quote='"',
                      escape='"', nullstr='')
    """).to_arrow_table()
    if not arrow.num_rows:
        raise SystemExit(f"hgnc: {url} yielded no rows")

    n = merge.write(cat, "raw.hgnc__complete_set", arrow, EqualTo("hgnc_version", version))
    merge.manifest(cat, release, "hgnc", "complete_set", url, n, version=version,
                   method="release_number" if dated else "retrieval_date", **facts)
    return version, n


def transform(cat, release, version):
    """Phase 2: HGNC's nomenclature record per gene, and its cross-references.

    Scoped to `version`'s rows: raw accumulates every landed version, so an
    unscoped read would derive from all of them at once.
    """
    con = duckdb.connect()
    con.register("raw", cat.load_table("raw.hgnc__complete_set").scan(
        row_filter=EqualTo("hgnc_version", version)).to_arrow())

    # date_modified is left in raw on purpose: it moves when ANY upstream field
    # changes, so carrying it would open a new version row for edits to columns
    # this table does not have.
    gene = con.sql(f"""
        SELECT hgnc_id, {TAXON} AS taxon_id, symbol, name, locus_group, locus_type, status,
               location, alias_symbol, alias_name, prev_symbol, prev_name,
               date_approved_reserved, date_symbol_changed, date_name_changed
        FROM raw
    """).to_arrow_table()

    # HGNC ids keep their 'HGNC:' prefix, the form ncbi.transform already writes
    # under this namespace, so the two writers' rows join. omim_id is the one
    # '|'-list among the four (11 genes on 2026-09-18); the others are single.
    mapping = con.sql(f"""
        SELECT DISTINCT 'HGNC' AS source_namespace, hgnc_id AS source_id,
               target_namespace, target_id, {TAXON} AS taxon_id,
               'HGNC' AS source, NULL::DOUBLE AS confidence
        FROM (
            SELECT hgnc_id, 'ENTREZ' AS target_namespace, entrez_id AS target_id FROM raw
          UNION ALL
            SELECT hgnc_id, 'ENSEMBL', ensembl_gene_id FROM raw
          UNION ALL
            SELECT hgnc_id, 'UCSC', ucsc_id FROM raw
          UNION ALL
            SELECT hgnc_id, 'OMIM', unnest(str_split(omim_id, '|')) FROM raw
        )
        WHERE target_id IS NOT NULL AND target_id <> ''
    """).to_arrow_table()

    return {
        # HGNC is this table's only writer, so its scope is the whole table.
        "annotation.hgnc__gene": merge.merge(
            cat, "annotation.hgnc__gene", gene, release, AlwaysTrue()),
        # The scope names THIS writer: NCBI and Ensembl write the same taxon.
        "annotation.identifier_mapping": merge.merge(
            cat, "annotation.identifier_mapping", mapping, release,
            And(EqualTo("taxon_id", TAXON), EqualTo("source", "HGNC"))),
    }


def ingest(cat, release, url=None):
    version, n = land_raw(cat, release, url)
    return {"raw.hgnc__complete_set": n, **transform(cat, release, version)}
