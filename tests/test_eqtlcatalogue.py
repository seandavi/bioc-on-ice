"""eQTL Catalogue: land metadata + FTP paths whole -> derive resource rows, statistics referenced.

The fixture is a handcrafted copy of the resources repo's layout at test time —
data_tables/dataset_metadata_r<N>.tsv and tabix/tabix_ftp_paths.tsv, in the real
files' dialect (header, tabs, no quoting) — so the test never touches the network.
"""

import pyarrow as pa
import pytest
from pyiceberg.expressions import EqualTo

from bioconice import catalog, eqtlcatalogue, merge

REL = "2026.08"
FTP = "ftp://ftp.ebi.ac.uk/pub/databases/spot/eQTL"

# (study, dataset, label, group, tissue_id, tissue_label, condition, n, method, pmid, type)
R7 = [
    ("QTS000001", "QTD000001", "Alasoo_2018", "macrophage_naive", "CL_0000235", "macrophage",
     "naive", "84", "ge", "29379200", "bulk"),
    ("QTS000001", "QTD000002", "Alasoo_2018", "macrophage_naive", "CL_0000235", "macrophage",
     "naive", "84", "exon", "29379200", "bulk"),
    ("QTS000015", "QTD000116", "GTEx", "adipose_subcutaneous", "UBERON_0002190",
     "adipose (subcutaneous)", "naive", "581", "ge", "32913098", "bulk"),
]


def paths(row, sample_size=None):
    study, ds, method = row[0], row[1], row[8]
    kind = "all" if method == "ge" else "cc"
    return (*row[:7], sample_size or row[7], method,
            f"{FTP}/sumstats/{study}/{ds}/{ds}.{kind}.tsv.gz",
            f"{FTP}/susie/{study}/{ds}/{ds}.credible_sets.tsv.gz",
            f"{FTP}/susie/{study}/{ds}/{ds}.lbf_variable.txt.gz")


def repo(root, name, datasets, path_rows=None, version="7"):
    """A directory shaped like the resources repo, holding the two landed files."""
    d = root / name
    (d / "data_tables").mkdir(parents=True)
    (d / "tabix").mkdir()
    for path, header, body in (
        (d / "data_tables" / f"dataset_metadata_r{version}.tsv", eqtlcatalogue.DATASET_COLUMNS, datasets),
        (d / "tabix" / "tabix_ftp_paths.tsv", eqtlcatalogue.PATH_COLUMNS,
         path_rows if path_rows is not None else [paths(r) for r in datasets]),
    ):
        path.write_text("".join("\t".join(r) + "\n" for r in [header, *body]))
    return str(d)


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path / "wh"))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


@pytest.fixture
def r7(tmp_path):
    # the path table's sample_size is the stale copy, as upstream's really is
    return repo(tmp_path, "r7", R7, [paths(R7[0], "89"), paths(R7[1]), paths(R7[2])])


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def test_raw_is_verbatim_and_whole(cat, r7):
    version, counts = eqtlcatalogue.land_raw(cat, REL, url=r7)
    assert version == "7"
    assert counts == {"raw.eqtlcatalogue__dataset": 3, "raw.eqtlcatalogue__tabix_ftp_paths": 3}

    raw = {r["dataset_id"]: r for r in rows(cat, "raw.eqtlcatalogue__dataset")}
    first = raw["QTD000001"]
    assert len(first) == len(eqtlcatalogue.DATASET_COLUMNS) + 2
    # unparsed: the underscore id, the sample size as text
    assert (first["tissue_id"], first["sample_size"], first["pmid"]) == ("CL_0000235", "84", "29379200")
    assert {(r["eqtlcatalogue_release"], r["landed_in"]) for r in raw.values()} == {("7", REL)}

    p = {r["dataset_id"]: r for r in rows(cat, "raw.eqtlcatalogue__tabix_ftp_paths")}
    assert len(p["QTD000001"]) == len(eqtlcatalogue.PATH_COLUMNS) + 2
    assert p["QTD000001"]["sample_size"] == "89"   # landed as published, stale or not
    assert p["QTD000002"]["ftp_path"] == f"{FTP}/sumstats/QTS000001/QTD000002/QTD000002.cc.tsv.gz"

    # re-landing the same release replaces it rather than appending
    eqtlcatalogue.land_raw(cat, REL, url=r7)
    assert len(rows(cat, "raw.eqtlcatalogue__dataset")) == 3
    assert len(rows(cat, "raw.eqtlcatalogue__tabix_ftp_paths")) == 3


def test_manifest_records_the_release_number(cat, r7):
    eqtlcatalogue.land_raw(cat, REL, url=r7)
    m = next(r for r in rows(cat, "provenance.release") if r["source"] == "eqtlcatalogue")
    assert (m["version_method"], m["source_version"], m["row_count"], m["url"]) == (
        "release_number", "7", 3, r7)


def test_a_changed_header_fails_before_landing(cat, tmp_path):
    d = repo(tmp_path, "renamed", R7)
    meta = tmp_path / "renamed" / "data_tables" / "dataset_metadata_r7.tsv"
    meta.write_text(meta.read_text().replace("tissue_id", "tissue_ontology_id", 1))
    with pytest.raises(SystemExit, match="tissue_ontology_id"):
        eqtlcatalogue.land_raw(cat, REL, url=d)


def test_derives_resource_rows_with_uris_and_relationships(cat, r7):
    counts = eqtlcatalogue.ingest(cat, REL, url=r7)
    assert counts["raw.eqtlcatalogue__dataset"] == 3

    ds = {d["dataset_id"]: d for d in rows(cat, "resource.eqtlcatalogue__dataset")}
    assert set(ds) == {"QTD000001", "QTD000002", "QTD000116"}
    first = ds["QTD000001"]
    # attributes from the metadata file (84, not the path table's stale 89), typed
    assert (first["sample_size"], first["tissue_term_id"], first["taxon_id"], first["pmid"],
            first["study_type"], first["license"]) == (84, "CL:0000235", 9606, "29379200",
                                                       "bulk", "CC BY 4.0")
    # URIs from the path table, as published
    assert first["sumstats_uri"] == f"{FTP}/sumstats/QTS000001/QTD000001/QTD000001.all.tsv.gz"
    assert first["credible_sets_uri"] == f"{FTP}/susie/QTS000001/QTD000001/QTD000001.credible_sets.tsv.gz"
    assert first["lbf_uri"] == f"{FTP}/susie/QTS000001/QTD000001/QTD000001.lbf_variable.txt.gz"
    assert first["valid_from"] == REL and first["valid_to"] is None

    rel = {(r["resource_id"], r["relationship"], r["target_id"], r["source"])
           for r in rows(cat, "resource.resource_relationship")}
    assert rel == {
        ("QTD000001", "has_cell_type", "CL:0000235", "eqtlcatalogue"),
        ("QTD000002", "has_cell_type", "CL:0000235", "eqtlcatalogue"),
        ("QTD000116", "has_tissue", "UBERON:0002190", "eqtlcatalogue"),
    }


def test_missing_markers_are_null_and_make_no_relationship(cat, tmp_path):
    """r8 already spells missing as 'NA' in pmid and tissue_id."""
    na = [(*R7[0][:4], "NA", "NA", *R7[0][6:9], "NA", "bulk")]
    eqtlcatalogue.ingest(cat, REL, url=repo(tmp_path, "na", na))
    (raw,) = rows(cat, "raw.eqtlcatalogue__dataset")
    assert raw["tissue_id"] is None and raw["pmid"] is None
    (d,) = rows(cat, "resource.eqtlcatalogue__dataset")
    assert d["tissue_term_id"] is None and d["pmid"] is None
    assert rows(cat, "resource.resource_relationship") == []


def test_a_path_table_for_another_release_fails_before_landing(cat, tmp_path):
    """The real state on 2026-09-18: r8 metadata exists, the path table is still r7's."""
    r8 = [*R7, ("QTS000045", "QTD000795", "IBDverse", "colon_T", "CL_0000084", "T cell",
                "naive", "233", "ge", "42236949", "single-cell")]
    d = repo(tmp_path, "r8", r8, [paths(r) for r in R7], version="8")
    with pytest.raises(SystemExit, match=r"1 datasets are in only one .* \(e\.g\. QTD000795\)"):
        eqtlcatalogue.ingest(cat, REL, url=d, version="8")
    # nothing was written: no raw rows mislabelled release 8, no manifest row
    assert cat.list_namespaces() == [] or not any(cat.list_tables(ns) for ns in cat.list_namespaces())


def test_a_malformed_tissue_id_fails_loudly(cat, tmp_path):
    bad = [(*R7[0][:4], "macrophage", *R7[0][5:])]
    version, _ = eqtlcatalogue.land_raw(cat, REL, url=repo(tmp_path, "bad", bad))
    with pytest.raises(Exception, match="not PREFIX_NUMBER: macrophage"):
        eqtlcatalogue.transform(cat, REL, version)


def test_rerun_is_a_noop_and_a_later_release_retires_and_adds(cat, r7, tmp_path):
    version, _ = eqtlcatalogue.land_raw(cat, REL, url=r7)
    eqtlcatalogue.transform(cat, REL, version)
    counts = eqtlcatalogue.transform(cat, "2026.09", version)
    assert {k: (c["written"], c["unchanged"]) for k, c in counts.items()} == {
        "resource.eqtlcatalogue__dataset": (0, 3), "resource.resource_relationship": (0, 3)}

    # release 8 drops the exon dataset, adds a single-cell one, corrects a sample size
    r8 = [(*R7[0][:7], "86", *R7[0][8:]), R7[2],
          ("QTS000045", "QTD000795", "IBDverse", "colon_T", "CL_0000084", "T cell",
           "naive", "233", "ge", "42236949", "single-cell")]
    eqtlcatalogue.ingest(cat, "2026.10", url=repo(tmp_path, "r8", r8, version="8"), version="8")

    history = sorted((d["dataset_id"], d["sample_size"], d["valid_from"], d["valid_to"])
                     for d in rows(cat, "resource.eqtlcatalogue__dataset"))
    assert history == [
        ("QTD000001", 84, REL, "2026.10"), ("QTD000001", 86, "2026.10", None),
        ("QTD000002", 84, REL, "2026.10"),
        ("QTD000116", 581, REL, None),
        ("QTD000795", 233, "2026.10", None),
    ]
    live = {(r["resource_id"], r["target_id"]) for r in rows(
        cat, "resource.resource_relationship", row_filter="valid_to IS NULL")}
    assert live == {("QTD000001", "CL:0000235"), ("QTD000116", "UBERON:0002190"),
                    ("QTD000795", "CL:0000084")}
    # raw accumulates releases
    assert {r["eqtlcatalogue_release"] for r in rows(cat, "raw.eqtlcatalogue__dataset")} == {"7", "8"}


def test_does_not_retire_another_catalogs_relationships(cat, r7):
    """ADR-0004: the relationship table is shared; the scope is this writer's source."""
    other = pa.table({"resource_id": ["x"], "relationship": ["has_tissue"],
                      "target_id": ["UBERON:0002190"], "source": ["cellxgene"]})
    merge.merge(cat, "resource.resource_relationship", other, REL, EqualTo("source", "cellxgene"))
    eqtlcatalogue.ingest(cat, "2026.09", url=r7)
    live = rows(cat, "resource.resource_relationship", row_filter="valid_to IS NULL")
    assert sorted(r["source"] for r in live) == ["cellxgene", "eqtlcatalogue", "eqtlcatalogue",
                                                 "eqtlcatalogue"]


def test_every_column_is_documented(cat, r7):
    """SPEC.md section B1, for the three tables this source adds."""
    eqtlcatalogue.ingest(cat, REL, url=r7)
    for identifier in ("raw.eqtlcatalogue__dataset", "raw.eqtlcatalogue__tabix_ftp_paths",
                       "resource.eqtlcatalogue__dataset"):
        table = cat.load_table(identifier)
        assert table.properties.get("comment"), identifier
        assert "CC BY 4.0; Kerimov et al. Nat Genet 2021" in table.properties["comment"]
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"
