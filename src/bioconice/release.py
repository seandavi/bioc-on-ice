"""Dataset release `bioconice-bugsigdb` (ADR-0013, cdsci-lake ADR-0025).

One full-snapshot release of the BugSigDB signature-member table, projected from the
shared cdsci-lake table `lake.bugsigdb.signature_taxon` and published through
`cdsci.lake.publish` to local storage only. Nothing here writes to Iceberg or the lake.
"""

import tempfile
import uuid
from datetime import date
from pathlib import Path

from cdsci.lake.contracts import ColumnContract, DatasetContract, TableContract, TemporalModel
from cdsci.lake.publish.builder import LocalDirStore
from cdsci.lake.publish.pipeline import publish_release
from cdsci.lake.publish.release import ReleaseManifest, SourceAssetVersion, release_date

_NULL_UNCURATED = "BugSigDB curated no value for this member."

SIGNATURE_TAXON = TableContract(
    name="annotation.signature_taxon",
    description=(
        "BugSigDB signature <-> NCBI taxon bridge: one row per taxon member of a curated "
        "signature, exploded from the two positionally-matched member-list columns of the "
        "BugSigDB full dump. Each member is reported at the rank the curators asserted "
        "(taxon_rank), with no rollup to a fixed rank; join ncbitaxon_id to a taxonomy for "
        "that. Study, experiment and contrast attributes are not in this table. Licence "
        "CC BY 4.0 (BugSigDB)."
    ),
    grain="one row per (bsdb_id, member_index)",
    primary_key=("bsdb_id", "member_index"),
    sort_by=("bsdb_id", "member_index"),
    temporal_model=TemporalModel.UPSERT_LATEST_SNAPSHOT,
    owner="bioc-on-ice",
    license="CC-BY-4.0",
    columns=(
        ColumnContract(
            "bsdb_id", "string",
            "BugSigDB signature id, e.g. 'bsdb:83/1/1' (the 'bsdb:' prefix is part of the "
            "value). Part of the key; recurs once per member of the signature.",
            False, identifier_namespace="bugsigdb"),
        ColumnContract(
            "member_index", "int64",
            "1-based position of this member in the signature's curated member list. Part "
            "of the key: (bsdb_id, member_index) is the grain. Not stable across BugSigDB "
            "versions if curators reorder a signature.",
            False),
        ColumnContract(
            "taxon_rank", "string",
            "MetaPhlAn rank letter of the asserted taxon: k (kingdom), p, c, o, f, g, s "
            "(species), or t (strain). The rank the curators reported the member at, not "
            "rolled up to a fixed rank; a signature mixes ranks.",
            True, null_meaning=_NULL_UNCURATED),
        ColumnContract(
            "taxon_name", "string",
            "Name of the asserted taxon with its rank prefix stripped, e.g. 'Anaerostipes "
            "caccae' from 's__Anaerostipes caccae'. MetaPhlAn's spelling, which may lag "
            "NCBI's current name.",
            True, null_meaning=_NULL_UNCURATED),
        ColumnContract(
            "ncbitaxon_id", "int64",
            "NCBI Taxonomy id of the asserted taxon: the last element of the member's "
            "lineage ids. Bare local id (bioregistry prefix ncbitaxon). Not part of the "
            "key: two members of one signature may resolve to the same taxon.",
            True, identifier_namespace="ncbitaxon",
            null_meaning="BugSigDB curated no taxonomy id for this member, or it is not "
                         "an integer."),
        ColumnContract(
            "taxon_lineage", "string",
            "The member's full MetaPhlAn lineage verbatim, '|'-separated with k__/p__/.../"
            "s__ prefixes. Kept for provenance; taxon_name/taxon_rank are its leaf.",
            True, null_meaning=_NULL_UNCURATED),
        ColumnContract(
            "taxon_lineage_ids", "string",
            "The member's NCBI Taxonomy ids up its lineage, '|'-separated, in the same "
            "rank order as taxon_lineage. Kept verbatim; ncbitaxon_id is its last element.",
            True, null_meaning=_NULL_UNCURATED),
    ),
)

BUGSIGDB = DatasetContract(
    id="bioconice-bugsigdb",
    title="biocOnIce: BugSigDB signature members",
    description=(
        "Full-snapshot releases of BugSigDB's curated signature members (one row per taxon "
        "of each signature), projected from the cdsci-lake table "
        "lake.bugsigdb.signature_taxon."
    ),
    publisher="bioc-on-ice",
    tables={"annotation.signature_taxon": SIGNATURE_TAXON},
)

_SELECT = (
    "SELECT bsdb_id, CAST(member_index AS BIGINT) AS member_index, taxon_rank, taxon_name, "
    "CAST(ncbitaxon_id AS BIGINT) AS ncbitaxon_id, taxon_lineage, taxon_lineage_ids "
    "FROM lake.bugsigdb.signature_taxon"
)


def release_bugsigdb(con, out: Path | None = None, *, today: date | None = None) -> ReleaseManifest:
    """Publish one `bioconice-bugsigdb` release; `con` has the lake attached as `lake`.

    Builds into a temporary directory removed on return when `out` is None."""
    sid = con.sql("SELECT max(snapshot_id) FROM lake.snapshots()").fetchone()[0]
    sources = (SourceAssetVersion(ref="lake.bugsigdb.signature_taxon", version=f"snapshot:{sid}"),)

    def run(root: Path) -> ReleaseManifest:
        return publish_release(
            LocalDirStore(root), contract=BUGSIGDB,
            tables={"annotation.signature_taxon": con.sql(_SELECT)},
            source_asset_versions=sources, run_id=str(uuid.uuid4()), today=today, con=None)

    if out is None:
        with tempfile.TemporaryDirectory(prefix="bioconice-bugsigdb-") as tmp:
            manifest = run(Path(tmp))
    else:
        manifest = run(out)
    t = manifest.tables[0]
    print(f"{BUGSIGDB.id} {manifest.release} ({release_date(manifest.release)}): {t.name} "
          f"{t.row_count:,} rows; output {out or 'temporary, removed'}")
    return manifest
