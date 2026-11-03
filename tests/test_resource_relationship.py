"""resource.resource_relationship: the release's union over the lake reproduces its four
landers' slices (#183 wave D). Offline: each source's own fixtures."""

import pytest
import test_bedbase
import test_cellxgene
import test_encode
from test_eqtlcatalogue import R7, repo

from bioconice import bedbase, catalog, eqtlcatalogue, publish


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path / "wh"))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def test_release_union_over_the_lake_reproduces_every_lander(cat, tmp_path, release_matches_lander):
    eqtlcatalogue.ingest(cat, "2026.08", url=repo(tmp_path, "r7", R7))
    test_cellxgene.ingest(cat)
    test_encode.ingest(cat, tmp_path)
    bedbase.ingest(cat, "2026.09", paths=test_bedbase.snapshot(tmp_path / "snap"))
    sources = {r["source"] for r in cat.load_table("resource.resource_relationship").scan()
               .to_arrow().to_pylist()}
    assert sources == {"eqtlcatalogue", "cellxgene", "encode", "bedbase"}

    lake = {"lake.eqtl_catalogue.dataset": "raw.eqtlcatalogue__dataset",
            "lake.eqtl_catalogue.tabix_ftp_path": "raw.eqtlcatalogue__tabix_ftp_paths",
            "lake.cellxgene.dataset": "raw.cellxgene__dataset",
            "lake.encode.experiment": "raw.encode__experiment",
            "lake.encode.file": "raw.encode__file",
            **{f"lake.bedbase.{t}": f"raw.bedbase__{t}" for t in ("bed", "bedset", "bedset_membership")}}
    stamped = {"lake.cellxgene.dataset": f"'{test_cellxgene.CENSUS}' AS census_release",
               **{t: "'2026-09-01' AS bedbase_version" for t in lake if t.startswith("lake.bedbase.")}}
    name = "resource.resource_relationship"
    release_matches_lander(cat, lake, {name: publish.FROM_LAKE[name]}, stamped=stamped)
