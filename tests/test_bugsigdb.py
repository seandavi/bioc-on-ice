"""BugSigDB: land from the CSV or from cdsci-lake (ADR-0012 pilot, #114), derive
annotation.signature_taxon (#152), and keep raw scoped per release tag.

The lake-path tests populate a LOCAL cdsci DuckLake (`lake_backend="local"`, a
tmp dir) from tests/tiny_bugsigdb.csv with cdsci-lake's own bugsigdb ingest, so
the parity claim is against what cdsci really curates, not a hand-made copy.
They skip when cdsci-lake is not installed (`uv pip install -e ../cdsci-lake`).

The third fixture row (bsdb:83/1/3, no members) exists for the untyped-column
divergence: its five typed-by-cdsci columns hold values the lake path renders
differently from the CSV path, as the raw table comment documents.
"""

from pathlib import Path

import pyarrow as pa
import pytest
from pyiceberg.exceptions import NoSuchTableError
from pyiceberg.expressions import EqualTo
from pyiceberg.schema import Schema

from bioconice import bugsigdb, catalog, merge, migrate, schemas

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
    assert summary["rows"] == 3
    return settings


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def by_id(rs):
    return sorted(rs, key=lambda r: r["bsdb_id"])


def test_lake_path_lands_the_same_rows_as_the_csv_path(cat, lake):
    bugsigdb.land_raw(cat, REL, "v1.3.1", url=CSV)
    from_csv = by_id(rows(cat, bugsigdb.RAW))

    assert bugsigdb.land_raw(cat, REL, "v1.3.1", lake=lake) == 3
    from_lake = by_id(rows(cat, bugsigdb.RAW))

    assert {r["bsdb_id"] for r in from_lake} == {r["bsdb_id"] for r in from_csv}
    assert from_lake[0].keys() == from_csv[0].keys()
    # not just the key set: every value, including the five columns cdsci types
    # and this lander renders back to text (pmid, year, sample sizes, curated_date)
    assert from_lake[:2] == from_csv[:2]
    assert from_csv[0]["curated_date"] == "10 January 2021"

    # The documented divergence, confined to those five columns: what cdsci's
    # TRY_CAST cannot parse is NULL, what it can is re-rendered canonically.
    csv3, lake3 = from_csv[2], from_lake[2]
    diverging = {k for k in csv3 if csv3[k] != lake3[k]}
    assert diverging == {"pmid", "year", "group_0_sample_size", "group_1_sample_size", "curated_date"}
    assert (csv3["pmid"], lake3["pmid"]) == ("0012345", "12345")
    assert (csv3["year"], lake3["year"]) == ("n.d.", None)
    assert (csv3["group_0_sample_size"], lake3["group_0_sample_size"]) == ("NR", None)
    assert (csv3["group_1_sample_size"], lake3["group_1_sample_size"]) == ("012", "12")
    assert (csv3["curated_date"], lake3["curated_date"]) == ("05 January 2021", "5 January 2021")
    comment = cat.load_table(bugsigdb.RAW).properties["comment"]
    assert "'05 January 2021' -> '5 January 2021'" in comment and "'NR'" in comment

    urls = {r["url"] for r in rows(cat, "provenance.release", row_filter=EqualTo("source", "bugsigdb"))}
    assert urls == {bugsigdb.LAKE_TABLE}   # the manifest says which path landed the tag


def test_landing_a_new_tag_does_not_retire_the_old_one(cat):
    """ADR-0004: raw is replaced per bugsigdb_version, never across tags."""
    bugsigdb.land_raw(cat, REL, "v1.3.1", url=CSV)
    bugsigdb.land_raw(cat, REL, "v1.3.2", url=CSV)
    versions = [r["bugsigdb_version"] for r in rows(cat, bugsigdb.RAW)]
    assert sorted(versions) == ["v1.3.1"] * 3 + ["v1.3.2"] * 3

    bugsigdb.land_raw(cat, REL, "v1.3.1", url=CSV)   # re-landing a tag is idempotent
    assert len(rows(cat, bugsigdb.RAW)) == 6


def test_a_tag_the_lake_has_moved_past_is_refused_before_any_write(cat, lake):
    """upsert_latest_snapshot keeps one tag per bsdb_id: a stale tag filter yields a
    partial dump, then none at all. Either would have replaced the tag's raw rows
    with fewer (cdsci-lake#103); the read refuses instead and raw is untouched."""
    from cdsci.lake.sources import bugsigdb as cdsci_bugsigdb

    bugsigdb.land_raw(cat, REL, "v1.3.1", url=CSV)
    before = rows(cat, bugsigdb.RAW)
    snapshot = cat.load_table(bugsigdb.RAW).current_snapshot().snapshot_id

    cdsci_bugsigdb.ingest(file=CSV, version="v1.3.2", settings=lake, limit=1)   # partial
    with pytest.raises(ValueError, match=r"2 of 3 rows carry v1.3.1"):
        bugsigdb.land_raw(cat, REL, "v1.3.1", lake=lake)
    cdsci_bugsigdb.ingest(file=CSV, version="v1.3.2", settings=lake)            # gone
    with pytest.raises(ValueError, match=r"0 of 3 rows carry v1.3.1"):
        bugsigdb.land_raw(cat, REL, "v1.3.1", lake=lake)

    assert cat.load_table(bugsigdb.RAW).current_snapshot().snapshot_id == snapshot
    assert rows(cat, bugsigdb.RAW) == before
    assert bugsigdb.land_raw(cat, REL, "v1.3.2", lake=lake) == 3               # the tag it holds


def test_write_refuses_an_empty_table(cat):
    """Every lander goes through merge.write, so a read that came back empty can
    never replace a scope with nothing."""
    bugsigdb.land_raw(cat, REL, "v1.3.1", url=CSV)
    empty = cat.load_table(bugsigdb.RAW).scan().to_arrow().slice(0, 0)
    with pytest.raises(ValueError, match="0 rows"):
        merge.write(cat, bugsigdb.RAW, empty, EqualTo("bugsigdb_version", "v1.3.1"))
    assert len(rows(cat, bugsigdb.RAW)) == 3
    with pytest.raises(ValueError, match="0 rows"):
        merge.write(cat, "raw.bugsigdb__never_created", empty, EqualTo("bugsigdb_version", "x"))
    with pytest.raises(NoSuchTableError):
        cat.load_table("raw.bugsigdb__never_created")


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
    code = "\n".join(line for line in body.splitlines() if not line.startswith("--")).strip()
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
    bridge = cat.load_table("annotation.signature_taxon")
    assert bridge.properties["bioc.column.ncbitaxon_id.prefix"] == "ncbitaxon"
    assert "Temporal model: scd2_release" in bridge.properties["comment"]
    assert "2,147,483,647" in bridge.schema().find_field("ncbitaxon_id").doc


@pytest.mark.parametrize("copy_swap", [False, True])
def test_migrate_sets_the_pre_152_table_aside_so_ingest_can_create_it(cat, copy_swap):
    """The live table has the seven explode columns and no validity: _evolve refuses
    to add a required valid_from, so ingest fails until migrate-signature-taxon."""
    identifier, v1 = "annotation.signature_taxon", "annotation.signature_taxon__v1"
    declared = schemas.TABLES[identifier].schema
    old = Schema(*[f for f in declared.fields if f.name not in merge.VALIDITY])
    cat.create_namespace_if_not_exists("annotation")
    cat.create_table(identifier, schema=old).append(pa.Table.from_pylist(
        [{"bsdb_id": "bsdb:1/1/1", "member_index": 1, "taxon_rank": "g", "taxon_name": "Old",
          "ncbitaxon_id": 1, "taxon_lineage": "g__Old", "taxon_lineage_ids": "1"}],
        schema=old.as_arrow()))
    with pytest.raises(ValueError, match="rebuild the table"):
        bugsigdb.ingest(cat, REL, url=CSV)

    migrate.signature_taxon(cat, copy_swap)
    migrate.signature_taxon(cat, copy_swap)                          # re-runnable
    assert bugsigdb.ingest(cat, REL, url=CSV)[identifier]["written"] == 33
    assert len(rows(cat, identifier)) == 33
    assert [f.name for f in cat.load_table(identifier).schema().fields][-2:] == list(merge.VALIDITY)
    assert rows(cat, v1) == [{"bsdb_id": "bsdb:1/1/1", "member_index": 1, "taxon_rank": "g",
                              "taxon_name": "Old", "ncbitaxon_id": 1, "taxon_lineage": "g__Old",
                              "taxon_lineage_ids": "1"}]
    migrate.signature_taxon(cat, copy_swap)                          # declared shape: untouched
    assert len(rows(cat, identifier)) == 33
