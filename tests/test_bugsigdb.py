"""BugSigDB: land from the CSV or from cdsci-lake (ADR-0012 pilot, #114), derive
annotation.signature_taxon (#152), and keep raw scoped per release tag.

The lake-path tests populate a LOCAL cdsci DuckLake (`lake_backend="local"`, a
tmp dir) from tests/tiny_bugsigdb.csv with cdsci-lake's own bugsigdb ingest, so
the parity claim is against what cdsci really curates, not a hand-made copy.
They skip when the [lake] dependency group is not installed.
"""

from pathlib import Path

import pytest
from pyiceberg.expressions import EqualTo

from bioconice import bugsigdb, catalog

REL = "2026.09"
HERE = Path(__file__).parent
CSV = str(HERE / "tiny_bugsigdb.csv")
CDSCI_MODEL = HERE.parent.parent / "cdsci-lake" / "transform" / "models" / "bugsigdb" / "signature_taxon.sql"


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path / "warehouse"))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


@pytest.fixture
def lake(tmp_path):
    """A local cdsci DuckLake holding the fixture as v1.3.1, loaded by cdsci's own ingest."""
    cdsci = pytest.importorskip("cdsci.lake")
    from cdsci.lake.sources import bugsigdb as cdsci_bugsigdb

    settings = cdsci.Settings(storage_base_uri=f"file://{tmp_path / 'lake'}", lake_backend="local")
    summary = cdsci_bugsigdb.ingest(file=CSV, version="v1.3.1", settings=settings)
    assert summary["rows"] == 2
    return settings


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def by_id(rs):
    return sorted(rs, key=lambda r: r["bsdb_id"])


def test_lake_path_lands_the_same_rows_as_the_csv_path(cat, lake):
    bugsigdb.land_raw(cat, REL, "v1.3.1", url=CSV)
    from_csv = by_id(rows(cat, bugsigdb.RAW))

    assert bugsigdb.land_raw(cat, REL, "v1.3.1", lake=lake) == 2
    from_lake = by_id(rows(cat, bugsigdb.RAW))

    assert {r["bsdb_id"] for r in from_lake} == {r["bsdb_id"] for r in from_csv}
    assert from_lake[0].keys() == from_csv[0].keys()
    # not just the key set: every value, including the five columns cdsci types
    # and this lander renders back to text (pmid, year, sample sizes, curated_date)
    assert from_lake == from_csv
    assert from_csv[0]["curated_date"] == "10 January 2021"

    urls = {r["url"] for r in rows(cat, "provenance.release", row_filter=EqualTo("source", "bugsigdb"))}
    assert urls == {bugsigdb.LAKE_TABLE}   # the manifest says which path landed the tag


def test_landing_a_new_tag_does_not_retire_the_old_one(cat):
    """ADR-0004: raw is replaced per bugsigdb_version, never across tags."""
    bugsigdb.land_raw(cat, REL, "v1.3.1", url=CSV)
    bugsigdb.land_raw(cat, REL, "v1.3.2", url=CSV)
    versions = [r["bugsigdb_version"] for r in rows(cat, bugsigdb.RAW)]
    assert sorted(versions) == ["v1.3.1"] * 2 + ["v1.3.2"] * 2

    bugsigdb.land_raw(cat, REL, "v1.3.1", url=CSV)   # re-landing a tag is idempotent
    assert len(rows(cat, bugsigdb.RAW)) == 4


def test_transform_explodes_members_at_their_asserted_rank(cat):
    bugsigdb.land_raw(cat, REL, "v1.3.1", url=CSV)
    counts = bugsigdb.transform(cat, REL)
    out = rows(cat, "annotation.signature_taxon")
    assert counts["annotation.signature_taxon"]["written"] == len(out) == 33

    assert len({(r["bsdb_id"], r["member_index"]) for r in out}) == len(out)   # the grain
    assert all(isinstance(r["ncbitaxon_id"], int) for r in out)
    assert {r["taxon_rank"] for r in out} <= {"k", "p", "c", "o", "f", "g", "s", "t"}
    assert all(r["valid_from"] == REL and r["valid_to"] is None for r in out)
    # no rank rollup: a genus-level member stays a genus, its lineage rides along
    m10 = next(r for r in out if r["bsdb_id"] == "bsdb:83/1/2" and r["member_index"] == 10)
    assert (m10["taxon_rank"], m10["taxon_name"], m10["ncbitaxon_id"]) == ("g", "Neurospora", 5140)
    assert m10["taxon_lineage_ids"].endswith("|5140") and m10["taxon_lineage"].endswith("g__Neurospora")
    assert not any(k.endswith(("genus", "species", "phylum")) for k in out[0])

    again = bugsigdb.transform(cat, REL)["annotation.signature_taxon"]
    assert again["written"] == 0 and again["unchanged"] == 33


@pytest.mark.skipif(not CDSCI_MODEL.exists(), reason="sibling cdsci-lake checkout not present")
def test_explode_matches_cdsci_model_over_the_same_lake(cat, lake):
    """The ported EXPLODE and cdsci's model SQL, run on one fixture, agree row for row."""
    from cdsci.lake import lake_connect

    sql = CDSCI_MODEL.read_text()
    body = sql[sql.index(");") + 2:]          # strip the SQLMesh MODEL(...) header
    code = "\n".join(l for l in body.splitlines() if not l.startswith("--")).strip()
    assert code.replace("FROM lake.bugsigdb.signatures", "FROM raw") == bugsigdb.EXPLODE.strip()

    con = lake_connect(lake, read_only=True)
    theirs = sorted(con.execute(body).fetchall())
    con.close()

    bugsigdb.land_raw(cat, REL, "v1.3.1", lake=lake)
    bugsigdb.transform(cat, REL)
    cols = ("bsdb_id", "member_index", "taxon_rank", "taxon_name", "ncbitaxon_id",
            "taxon_lineage", "taxon_lineage_ids")
    ours = sorted(tuple(r[c] for c in cols) for r in rows(cat, "annotation.signature_taxon"))
    assert ours == theirs and len(ours) == 33


def test_every_column_is_documented(cat):
    bugsigdb.ingest(cat, REL, url=CSV)
    for identifier in (bugsigdb.RAW, "annotation.signature_taxon"):
        table = cat.load_table(identifier)
        assert "CC BY 4.0" in table.properties.get("comment")
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"
    assert cat.load_table("annotation.signature_taxon").properties["bioc.column.ncbitaxon_id.prefix"] == "ncbitaxon"
