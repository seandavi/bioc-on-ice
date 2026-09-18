"""CELLxGENE Discover -> Iceberg: the single-cell dataset catalog, matrices referenced.

CELLxGENE Discover publishes a complete listing of every public dataset as one
JSON array (`curation/v1/datasets?visibility=PUBLIC`) -- 2,228 datasets,
291.75M cells at retrieval 2026-09-18. It carries no version of its own: it is
a live current-state listing, not an archived per-release dump, so raw holds
the **latest crawl only** -- the same deliberate narrowing `raw.icite__metadata`
makes of the accumulate-per-version rule, stated here for the same reason.

This is the "Large Data Integration: TileDB, referenced" case in SPEC.md.
Expression matrices are never ingested: `resource.cellxgene__dataset` carries
`h5ad_uri` (per dataset) and `census_release` (the Census LTS build the
dataset's cells join through), both pointers into public S3, never bytes.

Two phases, as everywhere else in this catalog:

  raw.cellxgene__dataset       the listing, one row per dataset, landed whole.
                                Multi-valued label/ontology_term_id fields
                                (organism, assay, tissue, disease, cell_type,
                                development_stage, self_reported_ethnicity,
                                sex) and other nested values (assets, spatial,
                                donor_id, ...) are kept as their JSON text,
                                UNEXPLODED -- exploding them is interpretation,
                                same reason Ensembl's GTF column 9 lands as one
                                unparsed attribute blob rather than split key
                                "value" pairs.
  resource.cellxgene__dataset  one row per dataset VERSION (Type 2 by
                                dataset_version_id): organism resolved to a
                                taxon id, the multi-valued fields exploded to
                                sorted lists of term ids, and one row per term in
                                (issue #84 acceptance criterion 7 -- readable
                                now, joinable to ontology.term once #83 lands),
                                h5ad_uri, and the spatial columns added in the
                                issue's 2026-09-18 comment.

A dataset that disappears from the next crawl (tombstoned, or superseded by a
revision under a new dataset_version_id) is retired by the ordinary merge
set-difference rule -- no special-cased tombstone handling needed, since
`tombstone=true` datasets are excluded from this PUBLIC listing outright
rather than kept with the flag set (0 of 2,228 at 2026-09-18).

`annotation.cellxgene__gene` (the Census `var` dataframe, issue #84's third
table) is DEFERRED: the Census only publishes `var` inside the TileDB-SOMA
store, and tiledbsoma is not an allowed dependency here (AGENTS.md: no new
dependencies without a recorded reason, and #85 already earmarks it, recorded,
for the cell-metadata source that has no DuckDB-readable alternative). If the
Census ever ships `var` as flat Parquet/CSV this can land the same way as
everything else in this module; until then it stays out.
"""

import json
import urllib.request
from datetime import datetime, timezone

import duckdb
from pyiceberg.expressions import AlwaysTrue, EqualTo

from . import merge
from .ncbi import _land

DATASETS_URL = "https://api.cellxgene.cziscience.com/curation/v1/datasets?visibility=PUBLIC"
CENSUS_RELEASE_URL = "https://census.cellxgene.cziscience.com/cellxgene-census/v1/release.json"
LICENSE = "CC BY 4.0"

# EFO assay term -> canonical spatial platform name. Built only from terms
# actually observed in a live PUBLIC crawl (2026-09-18: 350 Visium, 310
# Slide-seqV2, 4 Curio Seeker datasets, all single-assay). Extend when CZI
# adds a platform; never guess an EFO id that hasn't been seen in the data. A
# spatial dataset whose assay term is not here fails the ingest loudly
# (issue #84 acceptance criterion 11) rather than silently going NULL.
SPATIAL_PLATFORMS = {
    "EFO:0022857": "Visium",
    "EFO:0030062": "Slide-seqV2",
    "EFO:0920002": "Curio Seeker",
}

# Multi-valued fields shaped [{"label": ..., "ontology_term_id": ...}, ...].
# `tissue` additionally carries `tissue_type`; from_json's template only needs
# the fields read, so the extra key is simply ignored.
_LABEL_ID = '[{"label":"VARCHAR","ontology_term_id":"VARCHAR"}]'
_ASSET = '[{"filesize":"BIGINT","filetype":"VARCHAR","url":"VARCHAR"}]'

# Top-level keys of one curation/v1/datasets record -> DuckDB read_json's
# `columns=` type. Nested label/id lists and objects are typed JSON here and
# read out as text (see _source): kept unexploded in raw, per the module doc.
_STRUCT_LIST_FIELDS = (
    "organism", "assay", "tissue", "disease", "cell_type", "development_stage",
    "self_reported_ethnicity", "sex",
)
_OTHER_JSON_FIELDS = (
    "donor_id", "suspension_type", "is_primary_data", "assets", "spatial",
    "batch_condition", "perturbation_types", "genetic_perturbation_strategy",
)
COLUMNS = {
    "dataset_id": "VARCHAR", "dataset_version_id": "VARCHAR", "collection_id": "VARCHAR",
    "collection_version_id": "VARCHAR", "collection_name": "VARCHAR", "collection_doi": "VARCHAR",
    "collection_doi_label": "VARCHAR", "title": "VARCHAR", "citation": "VARCHAR",
    "schema_version": "VARCHAR", "cell_count": "BIGINT", "primary_cell_count": "BIGINT",
    "mean_genes_per_cell": "DOUBLE", "published_at": "VARCHAR", "revised_at": "VARCHAR",
    "explorer_url": "VARCHAR", "processing_status": "VARCHAR", "tombstone": "BOOLEAN",
    "visibility": "VARCHAR", "is_pre_analysis": "BOOLEAN",
    "revision_of_collection": "VARCHAR", "revision_of_dataset": "VARCHAR",
    "x_approximate_distribution": "VARCHAR",
    **{f: "JSON" for f in _STRUCT_LIST_FIELDS},
    **{f: "JSON" for f in _OTHER_JSON_FIELDS},
}


def _fetch_json(url):
    with urllib.request.urlopen(url, timeout=60) as r:
        return json.load(r)


def _source(path, retrieval_date):
    """The DuckDB read for the listing, by name and explicitly typed.

    Nested fields come back as their JSON text (`col::VARCHAR`); everything
    else is read straight through. `columns=` makes this name-based rather
    than positional, so `_check_keys` is what catches a renamed key -- DuckDB
    itself would otherwise just return NULL for one, unlike a CSV positional
    mismatch, which read_csv's own binder rejects.
    """
    colstr = "{" + ", ".join(f"'{k}':'{v}'" for k, v in COLUMNS.items()) + "}"
    select = ", ".join(
        f"{k}::VARCHAR AS {k}" if t == "JSON" else k for k, t in COLUMNS.items()
    )
    return (f"(SELECT {select}, '{retrieval_date}' AS retrieval_date "
            f"FROM read_json('{path}', format='array', columns={colstr}))")


def _check_keys(path):
    """Fail loudly if CZI renames or drops a top-level key.

    Explicit `columns=` reads by name: a missing key silently becomes NULL
    rather than erroring, which would otherwise let a renamed field (say
    `cell_count` -> `n_cells`) land as an all-NULL column with no signal.
    """
    actual = set(duckdb.sql(
        f"SELECT * FROM read_json_auto('{path}', format='array', sample_size=-1) LIMIT 0"
    ).columns)
    missing = set(COLUMNS) - actual
    if missing:
        raise SystemExit(f"cellxgene listing: expected keys missing upstream: {sorted(missing)}")


def land_raw(cat, release, url=None, json_path=None, retrieval_date=None):
    """Phase 1: the PUBLIC dataset listing, verbatim and whole, latest crawl only."""
    url = url or DATASETS_URL
    path = json_path or url
    retrieval_date = retrieval_date or datetime.now(timezone.utc).date().isoformat()
    _check_keys(path)
    n = _land(cat, release, "raw.cellxgene__dataset", _source(path, retrieval_date))
    merge.manifest(cat, release, "cellxgene", url, n, version=retrieval_date, method="retrieval_date")
    return retrieval_date, n


def resolve_census(census_release=None):
    """(build, S3 SOMA prefix) of the Census release datasets join through.

    An explicit `census_release` skips the manifest fetch entirely -- offline
    tests always pass one, and it is how `--census-release` bypasses the
    network live. Otherwise resolves the manifest's `stable` alias, CZI's own
    pointer to the current LTS build (verified 2026-09-18: stable = 2025-11-08,
    flags.lts = true).
    """
    if census_release:
        return (census_release,
                f"s3://cellxgene-census-public-us-west-2/cell-census/{census_release}/soma/")
    manifest = _fetch_json(CENSUS_RELEASE_URL)
    build = manifest["stable"]
    return build, manifest[build]["soma"]["uri"]


def _ids(col):
    # Sorted, so the merge's attribute comparison is order-independent.
    return f"list_sort(list_transform(from_json({col}, '{_LABEL_ID}'), x -> x.ontology_term_id))"


MULTI = ("assay", "tissue", "disease", "cell_type")


# Assertions on the derived rows, run before any write -- the organism and
# spatial-platform invariants (acceptance criteria 3 and 11) are enforced
# earlier, inline in the SQL below via `error()`, so a bad row never reaches a
# materialized table at all; these catch what a single-row expression cannot.
CHECKS = {
    "dataset_version_id is unique":  "SELECT count(*) - count(DISTINCT dataset_version_id) FROM ds",
    "h5ad_uri is present":           "SELECT count(*) FROM ds WHERE h5ad_uri IS NULL",
    "text has no empty strings":     "SELECT count(*) FROM ds WHERE '' IN "
                                      "(dataset_id, dataset_version_id, collection_id, "
                                      "license, h5ad_uri, census_release)",
}


def _check(con):
    failed = {name: con.sql(sql).fetchone()[0] for name, sql in CHECKS.items()}
    failed = {k: v for k, v in failed.items() if v}
    if failed:
        raise ValueError("cellxgene: derived rows violate " +
                          "; ".join(f"{k} ({v:,} rows)" for k, v in failed.items()))


def transform(cat, release, retrieval_date, census_release):
    """Phase 2: one resource row per dataset version, Type 2 by dataset_version_id.

    Organism -> taxon_id and spatial_platform are resolved with a SQL `error()`
    in the same expression that computes them (`_flag`'s pattern in icite.py):
    a dataset with an unmapped organism or, if spatial, an unmapped assay term
    fails the moment the row is built, before `_check` even runs and long
    before `merge.merge` writes anything.
    """
    con = duckdb.connect()
    con.register("raw", cat.load_table("raw.cellxgene__dataset").scan(
        row_filter=EqualTo("retrieval_date", retrieval_date)).to_arrow())

    platform_cases = " ".join(
        f"WHEN assay_term_id_1 = '{term}' THEN '{name}'" for term, name in SPATIAL_PLATFORMS.items())

    con.execute(f"""
        CREATE TABLE ds AS
        WITH x AS (
            SELECT
                dataset_id, dataset_version_id, collection_id, collection_name, collection_doi,
                title,
                len(from_json(organism, '{_LABEL_ID}')) AS n_organisms,
                (from_json(organism, '{_LABEL_ID}'))[1].ontology_term_id AS organism_term_id,
                (from_json(organism, '{_LABEL_ID}'))[1].label AS organism_label,
                {_ids('assay')} AS assay_term_ids,
                {_ids('tissue')} AS tissue_term_ids,
                {_ids('disease')} AS disease_term_ids,
                {_ids('cell_type')} AS cell_type_term_ids,
                cell_count, primary_cell_count, mean_genes_per_cell, schema_version,
                (list_filter(from_json(assets, '{_ASSET}'), a -> a.filetype = 'H5AD'))[1].url AS h5ad_uri,
                published_at, revised_at, tombstone,
                spatial IS NOT NULL AS is_spatial,
                (from_json(assay, '{_LABEL_ID}'))[1].ontology_term_id AS assay_term_id_1
            FROM raw
        )
        SELECT
            dataset_id, dataset_version_id, collection_id, collection_name, collection_doi, title,
            CASE WHEN n_organisms = 1 AND organism_term_id LIKE 'NCBITaxon:%'
                 THEN regexp_extract(organism_term_id, 'NCBITaxon:([0-9]+)', 1)::INTEGER
                 ELSE error('cellxgene: dataset ' || dataset_version_id || ' has an unmapped '
                            || 'organism ' || COALESCE(organism_term_id, 'NULL')
                            || ' (n_organisms=' || n_organisms || ')')
            END AS taxon_id,
            organism_label,
            assay_term_ids, tissue_term_ids, disease_term_ids, cell_type_term_ids,
            cell_count, primary_cell_count, mean_genes_per_cell, schema_version,
            '{LICENSE}' AS license,
            h5ad_uri,
            '{census_release}' AS census_release,
            published_at, revised_at, tombstone,
            is_spatial,
            CASE WHEN NOT is_spatial THEN NULL
                 {platform_cases}
                 ELSE error('cellxgene: spatial dataset ' || dataset_version_id
                            || ' has an unmapped assay term ' || assay_term_id_1)
            END AS spatial_platform,
            -- No source in this listing publishes a SpatialData (OME-Zarr) store yet
            -- (asset filetypes observed 2026-09-18: H5AD, ATAC_FRAGMENT, ATAC_INDEX
            -- only); NULL throughout until one does, per issue #84's spatial comment.
            NULL::VARCHAR AS spatialdata_uri
        FROM x
    """)
    _check(con)
    ds = con.sql("SELECT * FROM ds").to_arrow_table()

    # The joinable form: one row per (dataset version, relationship, term id).
    # DISTINCT: a listing can name the same term twice in one field (seen live,
    # 2026-09-18), and the relationship is a set.
    rel = con.sql("SELECT DISTINCT * FROM (" + " UNION ALL ".join(
        f"SELECT dataset_version_id AS resource_id, 'has_{f}' AS relationship, "
        f"unnest({f}_term_ids) AS target_id, 'cellxgene' AS source FROM ds"
        for f in MULTI) + ")").to_arrow_table()
    return {
        "resource.cellxgene__dataset": merge.merge(
            cat, "resource.cellxgene__dataset", ds, release, AlwaysTrue()),
        "resource.resource_relationship": merge.merge(
            cat, "resource.resource_relationship", rel, release, EqualTo("source", "cellxgene")),
    }


def ingest(cat, release, url=None, json_path=None, census_release=None, retrieval_date=None):
    retrieval_date, n = land_raw(cat, release, url, json_path, retrieval_date)
    build, soma_uri = resolve_census(census_release)
    counts = transform(cat, release, retrieval_date, build)
    # row_count here is "datasets in this release referencing this Census build",
    # the closest cheap integrity number available -- the Census release itself is
    # never landed (matrices stay referenced), so there is no row count of its own.
    merge.manifest(cat, release, "cellxgene_census", soma_uri, n, version=build, method="release_number")
    return {f"raw.cellxgene__dataset [{retrieval_date}]": n, **counts}
