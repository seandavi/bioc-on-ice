"""eQTL Catalogue -> Iceberg: the dataset catalog, summary statistics referenced.

The eQTL Catalogue (EBI / University of Tartu) reprocesses public QTL studies
uniformly and publishes, per dataset (one study x sample group x quantification
method), a tabix-indexed summary-statistics file and SuSiE fine-mapping results
on the EBI FTP — about 2 GB a dataset for gene expression. CC BY 4.0. This is
the "referenced, not ingested" case in SPEC.md, as cellxgene.py is for
matrices: `resource.eqtlcatalogue__dataset` carries URIs, never bytes.

The metadata lives in github.com/eQTL-Catalogue/eQTL-Catalogue-resources, and
two of its files are landed, whole:

  data_tables/dataset_metadata_r<N>.tsv   the datasets of release N, with pmid
                                          and study_type; the file ebi.ac.uk/eqtl
                                          links as "the metadata"
  tabix/tabix_ftp_paths.tsv               the same datasets with the three FTP
                                          paths — the only place the URIs are
                                          published

They overlap on eight columns. Transform takes the attributes from the metadata
file, which upstream keeps correcting (sample_size differs on 120 of 758
datasets on 2026-09-18; the path table was last touched in June 2024), and
only the URIs from the path table. The two must name exactly the same datasets
or the ingest fails before anything is written: that is the check that the path
table belongs to the release being landed (it names none itself), so no row
ever points at a file that is not there.

**Release 7, pinned at a tag.** The version is the release number in the file
name, recorded as `release_number`. Release 7 (June 2024) is what the site's
release notes announce and what the FTP serves. The repo already carries
`dataset_metadata_r8.tsv` (1,205 datasets) but, checked 2026-09-18, the 481 new
ones have no path rows and no files under sumstats/ — only an `r8_beta/` tree —
so release 8 is not landable yet, and the same-datasets check is what says so.
The files are fetched at git tag v26.09.2 (the repo's only tag; it is a repo
version, not a Catalogue release) so the bytes are immutable; the manifest url
records it. Raw is replaced per release, so re-landing is idempotent.

Both files are plain TSV: a header line, tab-delimited, no quoting (no quote
character occurs), LF. 'NA' is R's missing marker and reads as NULL, as the
empty cell does — r7 has neither, r8 already has 'NA' in pmid and tissue_id.

ponytail: credible sets are NOT ingested. The issue proposes them as
annotation.eqtlcatalogue__credible_set; they are one gzipped TSV per dataset
(758 fetches from an FTP that blacklists bursty clients), which is a crawl with
its own politeness and failure handling, not a metadata landing. This PR
references them by `credible_sets_uri`; land them as their own source when
something needs variant-level rows. `tabix_ftp_paths_imported.tsv` (49 GTEx v8
files in upstream's own format, a different column set) is likewise not landed.
"""

import duckdb
from pyiceberg.expressions import AlwaysTrue, EqualTo

from . import merge
from .hgnc import _header

# ponytail: the tag and the release are constants, bumped by hand when release
# 8 ships (paths published, files out of r8_beta/), along with the "release 7"
# in the three table comments' attribution. No "latest" discovery: nothing
# upstream names the current release in a machine-readable place.
URL = "https://raw.githubusercontent.com/eQTL-Catalogue/eQTL-Catalogue-resources/v26.09.2"
RELEASE = "7"
TAXON = 9606
LICENSE = "CC BY 4.0"

# Each file's header, in file order, checked whole before anything is read.
DATASET_COLUMNS = (
    "study_id", "dataset_id", "study_label", "sample_group", "tissue_id", "tissue_label",
    "condition_label", "sample_size", "quant_method", "pmid", "study_type",
)
PATH_COLUMNS = (
    "study_id", "dataset_id", "study_label", "sample_group", "tissue_id", "tissue_label",
    "condition_label", "sample_size", "quant_method", "ftp_path", "ftp_cs_path", "ftp_lbf_path",
)


def _read(url, columns, version, release):
    if (header := _header(url)) != columns:
        raise SystemExit(f"eqtlcatalogue: {url} header is not the declared one; "
                         f"differs in {sorted(set(header) ^ set(columns))}")
    # The dialect is stated rather than sniffed; all_varchar keeps raw unparsed.
    arrow = duckdb.connect().sql(f"""
        SELECT *, '{version}' AS eqtlcatalogue_release, '{release}' AS landed_in
        FROM read_csv('{url}', header=true, all_varchar=true, delim='\\t', quote='',
                      escape='', nullstr=['', 'NA'])
    """).to_arrow_table()
    if not arrow.num_rows:
        raise SystemExit(f"eqtlcatalogue: {url} yielded no rows")
    return arrow


def land_raw(cat, release, url=None, version=None):
    """Phase 1: the release's dataset metadata and the FTP path table, verbatim and whole.

    `url` is the root of a copy of the resources repo: another ref, or a local
    checkout. Returns (version, {table: rows}).
    """
    url, version = (url or URL).rstrip("/"), version or RELEASE
    files = {
        "raw.eqtlcatalogue__dataset": (
            f"{url}/data_tables/dataset_metadata_r{version}.tsv", DATASET_COLUMNS),
        "raw.eqtlcatalogue__tabix_ftp_paths": (f"{url}/tabix/tabix_ftp_paths.tsv", PATH_COLUMNS),
    }
    raw = {identifier: _read(file, columns, version, release)
           for identifier, (file, columns) in files.items()}
    # The path table names no release, so it is about to be labelled with the
    # metadata file's. That label is only true if the two name the same
    # datasets, no more, no fewer — checked before either is written.
    unpaired = set.symmetric_difference(*(set(t["dataset_id"].to_pylist()) for t in raw.values()))
    if unpaired:
        raise SystemExit(f"eqtlcatalogue: {len(unpaired):,} datasets are in only one of the "
                         f"release {version} metadata and tabix_ftp_paths.tsv (e.g. "
                         f"{sorted(unpaired)[0]}); the path table is for another release")

    # Each file is declared at its write, not its read: both are read first (the
    # pairing check above), and a tagged GitHub ref does not change in between.
    counts = {}
    for identifier, (file, _) in files.items():
        artifact = identifier.split("__")[1]
        facts = merge.reading(release, "eqtlcatalogue", artifact, file)
        counts[identifier] = merge.write(cat, identifier, raw[identifier],
                                         EqualTo("eqtlcatalogue_release", version))
        merge.manifest(cat, release, "eqtlcatalogue", artifact, file, counts[identifier],
                       version=version, method="release_number", **facts)
    return version, counts


def transform(cat, release, version):
    """Phase 2: one resource row per dataset, and its tissue / cell type as a relationship.

    Scoped to `version`'s rows: raw accumulates every landed release.
    """
    con = duckdb.connect()
    for name in ("dataset", "tabix_ftp_paths"):
        con.register(name, cat.load_table(f"raw.eqtlcatalogue__{name}").scan(
            row_filter=EqualTo("eqtlcatalogue_release", version)).to_arrow())

    # tissue_id is an OBO id spelled PREFIX_NUMBER; ontology.term spells it as
    # a CURIE. Anything else fails here rather than landing as a dead join key.
    con.execute(f"""
        CREATE TABLE ds AS
        SELECT d.dataset_id, d.study_id, d.study_label, d.sample_group, {TAXON} AS taxon_id,
               CASE WHEN d.tissue_id IS NULL OR regexp_full_match(d.tissue_id, '[A-Za-z]+_[0-9]+')
                    THEN replace(d.tissue_id, '_', ':')
                    ELSE error('eqtlcatalogue: ' || d.dataset_id || ' has a tissue_id that is '
                               || 'not PREFIX_NUMBER: ' || d.tissue_id) END AS tissue_term_id,
               d.tissue_label, d.condition_label, d.sample_size::INTEGER AS sample_size,
               d.quant_method, d.study_type, d.pmid, '{LICENSE}' AS license,
               p.ftp_path AS sumstats_uri, p.ftp_cs_path AS credible_sets_uri,
               p.ftp_lbf_path AS lbf_uri
        FROM dataset d JOIN tabix_ftp_paths p USING (dataset_id)
    """)
    ds = con.sql("SELECT * FROM ds").to_arrow_table()

    # Upstream's one tissue_id column holds cell types (CL) as well as tissues
    # (UBERON) and cell lines (EFO, BTO). CL goes under has_cell_type, the
    # relationship CELLxGENE's cell types use, so one join finds both catalogs.
    rel = con.sql("""
        SELECT dataset_id AS resource_id,
               CASE WHEN tissue_term_id LIKE 'CL:%' THEN 'has_cell_type' ELSE 'has_tissue' END
                   AS relationship,
               tissue_term_id AS target_id, 'eqtlcatalogue' AS source
        FROM ds WHERE tissue_term_id IS NOT NULL
    """).to_arrow_table()

    return {
        "resource.eqtlcatalogue__dataset": merge.merge(
            cat, "resource.eqtlcatalogue__dataset", ds, release, AlwaysTrue()),
        "resource.resource_relationship": merge.merge(
            cat, "resource.resource_relationship", rel, release,
            EqualTo("source", "eqtlcatalogue")),
    }


def ingest(cat, release, url=None, version=None):
    version, counts = land_raw(cat, release, url, version)
    return {**counts, **transform(cat, release, version)}
