"""CELLxGENE: land the dataset listing whole -> derive one resource row per
dataset version, on a trimmed fixture. Run with `uv run pytest`.

Offline throughout: `json_path=` bypasses the Discover API and
`census_release=` bypasses the Census release manifest, so nothing here
touches the network. The fixture mirrors four real records taken from a live
crawl (2026-09-18) -- one human, one mouse, one Visium spatial, one zebrafish
(the long-tail-organism case) -- trimmed to the fields this module reads.
"""

import json
from pathlib import Path

import pytest

from bioconice import catalog, cellxgene

JSON1 = str(Path(__file__).parent / "tiny_cellxgene.json")
JSON2 = str(Path(__file__).parent / "tiny_cellxgene_next.json")
REL = "2026.09"
CENSUS = "2025-11-08"


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def ingest(cat, release=REL, json_path=JSON1, census_release=CENSUS, retrieval_date="2026-09-18"):
    return cellxgene.ingest(cat, release, json_path=json_path, census_release=census_release,
                            retrieval_date=retrieval_date)


def test_raw_is_landed_whole_with_nested_fields_unexploded(cat):
    counts = ingest(cat)
    assert counts["raw.cellxgene__dataset [2026-09-18]"] == 4
    raw = {r["dataset_version_id"]: r for r in rows(cat, "raw.cellxgene__dataset")}
    assert set(raw) == {"d1-v1", "d2-v1", "d3-v1", "d4-v1"}
    # nested fields land as JSON text, unexploded
    assert json.loads(raw["d1-v1"]["organism"]) == [
        {"label": "Homo sapiens", "ontology_term_id": "NCBITaxon:9606"}]
    assert json.loads(raw["d3-v1"]["spatial"]) == {"has_fullres": False, "is_single": True}
    assert raw["d1-v1"]["spatial"] is None
    assert raw["d1-v1"]["retrieval_date"] == "2026-09-18"
    m = next(r for r in rows(cat, "provenance.release") if r["source"] == "cellxgene")
    assert (m["source_version"], m["version_method"]) == ("2026-09-18", "retrieval_date")


def test_census_manifest_is_recorded(cat):
    ingest(cat)
    m = next(r for r in rows(cat, "provenance.release") if r["source"] == "cellxgene_census")
    assert (m["source_version"], m["version_method"]) == (CENSUS, "release_number")
    assert m["url"] == f"s3://cellxgene-census-public-us-west-2/cell-census/{CENSUS}/soma/"


def test_resource_row_per_dataset_version(cat):
    counts = ingest(cat)
    assert counts["resource.cellxgene__dataset"]["written"] == 4
    ds = {r["dataset_version_id"]: r for r in rows(cat, "resource.cellxgene__dataset")}
    assert set(ds) == {"d1-v1", "d2-v1", "d3-v1", "d4-v1"}
    assert ds["d1-v1"]["license"] == "CC BY 4.0"
    assert ds["d1-v1"]["h5ad_uri"] == "https://datasets.cellxgene.cziscience.com/d1-v1.h5ad"
    assert ds["d1-v1"]["census_release"] == CENSUS
    assert sum(r["cell_count"] for r in ds.values()) == 450 + 106436 + 4992 + 17598


def test_organism_resolves_to_taxon_id(cat):
    ingest(cat)
    ds = {r["dataset_version_id"]: r for r in rows(cat, "resource.cellxgene__dataset")}
    assert ds["d1-v1"]["taxon_id"] == 9606
    assert ds["d1-v1"]["organism_label"] == "Homo sapiens"
    assert ds["d2-v1"]["taxon_id"] == 10090
    assert ds["d4-v1"]["taxon_id"] == 7955  # the long-tail organism


def test_multivalued_fields_are_ids_and_labels(cat):
    ingest(cat)
    ds = {r["dataset_version_id"]: r for r in rows(cat, "resource.cellxgene__dataset")}
    row = ds["d1-v1"]
    assert row["cell_type_term_ids"] == "CL:0000084"
    assert row["cell_type_labels"] == "T cell"
    assert row["tissue_term_ids"] == "UBERON:0000178"
    assert row["disease_term_ids"] == "PATO:0000461"
    assert row["disease_labels"] == "normal"


def test_spatial_columns(cat):
    ingest(cat)
    ds = {r["dataset_version_id"]: r for r in rows(cat, "resource.cellxgene__dataset")}
    assert ds["d3-v1"]["is_spatial"] is True
    assert ds["d3-v1"]["spatial_platform"] == "Visium"
    assert ds["d3-v1"]["spatialdata_uri"] is None  # no such asset published, ever, yet
    for v in ("d1-v1", "d2-v1", "d4-v1"):
        assert ds[v]["is_spatial"] is False
        assert ds[v]["spatial_platform"] is None
    spatial_count = sum(r["is_spatial"] for r in ds.values())
    assert spatial_count == 1  # matches the listing's own spatial flag count exactly


def test_revision_and_retirement_close_versions(cat, tmp_path):
    ingest(cat)
    counts = ingest(cat, release="2026.10", json_path=JSON2, retrieval_date="2026-10-01")

    # d2 vanished from the crawl (tombstoned/removed) -> retired.
    # d3's revision carries a brand new dataset_version_id -- the business key
    # itself -- so to the merge it is indistinguishable from one dataset
    # disappearing and an unrelated one appearing: d3-v1 retired, d3-v2 new.
    # That is exactly what issue #84 acceptance criterion 5 asks for: the old
    # version closed, the new version a new row. d1, d4 unchanged.
    assert counts["resource.cellxgene__dataset"]["retired"] == 2
    assert counts["resource.cellxgene__dataset"]["new"] == 1
    assert counts["resource.cellxgene__dataset"]["unchanged"] == 2

    live = rows(cat, "resource.cellxgene__dataset", row_filter="valid_to IS NULL")
    assert {r["dataset_version_id"] for r in live} == {"d1-v1", "d3-v2", "d4-v1"}

    d2 = next(r for r in rows(cat, "resource.cellxgene__dataset") if r["dataset_version_id"] == "d2-v1")
    assert d2["valid_from"] == REL and d2["valid_to"] == "2026.10"

    d3v1 = next(r for r in rows(cat, "resource.cellxgene__dataset") if r["dataset_version_id"] == "d3-v1")
    assert d3v1["valid_to"] == "2026.10"
    d3v2 = next(r for r in rows(cat, "resource.cellxgene__dataset") if r["dataset_version_id"] == "d3-v2")
    assert d3v2["valid_from"] == "2026.10" and d3v2["valid_to"] is None
    assert d3v2["cell_count"] == 5100

    # raw holds the latest crawl only
    assert {r["dataset_version_id"] for r in rows(cat, "raw.cellxgene__dataset")} == {"d1-v1", "d3-v2", "d4-v1"}


def test_unmapped_organism_fails_loudly(cat, tmp_path):
    bad = json.loads(Path(JSON1).read_text())
    bad[0]["organism"] = [{"label": "Something new", "ontology_term_id": "FOO:123"}]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(Exception, match="unmapped organism"):
        ingest(cat, json_path=str(path))
    assert "resource.cellxgene__dataset" not in {".".join(t) for ns in cat.list_namespaces()
                                                 for t in cat.list_tables(ns)}


def test_multiple_organisms_fails_loudly(cat, tmp_path):
    bad = json.loads(Path(JSON1).read_text())
    bad[0]["organism"].append({"label": "Mus musculus", "ontology_term_id": "NCBITaxon:10090"})
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(Exception, match="unmapped organism"):
        ingest(cat, json_path=str(path))


def test_unmapped_spatial_assay_fails_loudly(cat, tmp_path):
    bad = json.loads(Path(JSON1).read_text())
    spatial_row = next(r for r in bad if r["dataset_version_id"] == "d3-v1")
    spatial_row["assay"] = [{"label": "some new spatial platform", "ontology_term_id": "EFO:9999999"}]
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(Exception, match="unmapped assay term"):
        ingest(cat, json_path=str(path))


def test_renamed_key_fails_loudly(cat, tmp_path):
    bad = json.loads(Path(JSON1).read_text())
    for r in bad:
        r["n_cells"] = r.pop("cell_count")
    path = tmp_path / "bad.json"
    path.write_text(json.dumps(bad))
    with pytest.raises(SystemExit, match="cell_count"):
        ingest(cat, json_path=str(path))


def test_landing_no_rows_fails_loudly(cat, tmp_path):
    """An empty listing has no keys to sniff at all, so _check_keys is what
    fires -- still loud, just earlier than _land's own empty-result guard."""
    empty = tmp_path / "empty.json"
    empty.write_text("[]")
    with pytest.raises(SystemExit, match="expected keys missing upstream"):
        cellxgene.land_raw(cat, REL, json_path=str(empty), retrieval_date="2026-09-18")


def test_every_column_is_documented(cat):
    """SPEC.md section B1, for the two tables this source creates."""
    ingest(cat)
    for identifier in ("raw.cellxgene__dataset", "resource.cellxgene__dataset"):
        table = cat.load_table(identifier)
        assert table.properties.get("comment"), identifier
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"
