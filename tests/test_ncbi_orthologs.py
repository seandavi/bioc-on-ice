"""gene_orthologs + gene_group: land whole -> derive pairs both ways -> scoped merges.

The fixtures are written at test time in the real files' five-column format
(header verbatim), so the test never touches the network.
"""

import pyarrow as pa
import pytest
from pyiceberg.expressions import EqualTo

from bioconice import catalog, merge, ncbi_orthologs

REL = "2026.08"
HEADER = "#tax_id\tGeneID\trelationship\tOther_tax_id\tOther_GeneID"
ORTHOLOGS = [
    "9606\t7157\tOrtholog\t10090\t22059",          # TP53 -> Trp53
    "9606\t7157\tOrtholog\t10116\t24842",          # TP53 -> rat Tp53
    "9606\t672\tOrtholog\t10090\t12189",           # BRCA1 -> Brca1
    "10090\t100313531\tOrtholog\t9606\t100874189",  # mouse is the primary here
    "7070\t641505\tOrtholog\t7048\t115881920",     # names neither human nor mouse
]
GROUP = [
    "3218\t112277806\tRelated functional gene\t3218\t112291008",
    "3218\t112291008\tRelated pseudogene\t3218\t112277806",
]


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def load(cat, tmp_path, release=REL, taxa=(None,), orthologs=ORTHOLOGS):
    urls = {}
    for name, lines in (("gene_orthologs", orthologs), ("gene_group", GROUP)):
        path = tmp_path / f"{name}.tsv"
        path.write_text("\n".join([HEADER, *lines]) + "\n")
        urls[name] = str(path)
    landed = ncbi_orthologs.land_raw(cat, release, urls)
    counts = {}
    for taxon in taxa:
        counts.update(ncbi_orthologs.transform(cat, release, taxon))
    return landed, counts["annotation.ortholog"]


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def pairs(cat, where="valid_to IS NULL"):
    return {(r["taxon_id"], r["gene_id"], r["ortholog_taxon_id"], r["ortholog_gene_id"])
            for r in rows(cat, "annotation.ortholog", row_filter=where)}


def test_raw_is_landed_whole_and_verbatim(cat, tmp_path):
    landed, _ = load(cat, tmp_path, taxa=(10090,))
    assert landed == {"raw.ncbi__gene_orthologs": 5, "raw.ncbi__gene_group": 2}
    raw = rows(cat, "raw.ncbi__gene_orthologs")
    # whole: the fly pair lands though only mouse is derived, and in file direction only
    assert {r["taxon_id"] for r in raw} == {9606, 10090, 7070}
    assert {r["taxon_id"] for r in rows(cat, "annotation.ortholog")} == {10090}
    # gene_group lands every relationship and derives nothing
    assert {r["relationship"] for r in rows(cat, "raw.ncbi__gene_group")} == {
        "Related functional gene", "Related pseudogene"}


def test_pairs_are_derived_in_both_directions(cat, tmp_path):
    _, counts = load(cat, tmp_path)
    assert counts["written"] == 10
    got = pairs(cat)
    assert (9606, "7157", 10090, "22059") in got and (10090, "22059", 9606, "7157") in got
    # only what NCBI lists: mouse and rat both hang off human and are not paired directly
    assert not any(p[0] == 10090 and p[2] == 10116 for p in got)
    assert {r["source"] for r in rows(cat, "annotation.ortholog")} == {"NCBI"}


def test_rerun_is_a_noop(cat, tmp_path):
    load(cat, tmp_path)
    _, counts = load(cat, tmp_path, release="2026.09")
    assert (counts["written"], counts["unchanged"]) == (0, 10)
    assert {r["valid_from"] for r in rows(cat, "annotation.ortholog")} == {REL}


def test_a_narrowed_run_owns_its_taxon_and_nothing_else(cat, tmp_path):
    """The row belongs to taxon_id, so a --taxa run retires only rows it recomputed."""
    load(cat, tmp_path)
    # upstream drops BRCA1 <-> Brca1; only mouse is re-derived
    _, counts = load(cat, tmp_path, release="2026.09", taxa=(10090,),
                     orthologs=[o for o in ORTHOLOGS if "12189" not in o])
    assert (counts["retired"], counts["unchanged"]) == (1, 2)
    live = pairs(cat)
    assert (10090, "12189", 9606, "672") not in live
    # the mirror is human's row: untouched until a run covers human
    assert (9606, "672", 10090, "12189") in live
    assert len(live) == 9


def test_another_writer_is_neither_retired_nor_retires(cat, tmp_path):
    compara = pa.Table.from_pylist([{
        "gene_id": "ENSG00000141510", "taxon_id": 9606, "ortholog_gene_id": "ENSMUSG00000059552",
        "ortholog_taxon_id": 10090, "source": "ENSEMBL_COMPARA"}])
    scope = EqualTo("source", "ENSEMBL_COMPARA")
    merge.merge(cat, "annotation.ortholog", compara, REL, scope)
    load(cat, tmp_path)
    assert merge.merge(cat, "annotation.ortholog", compara, "2026.09", scope)["unchanged"] == 1
    _, counts = load(cat, tmp_path, release="2026.09")
    assert counts["unchanged"] == 10
    assert len(pairs(cat)) == 11 and not pairs(cat, "valid_to IS NOT NULL")


def test_manifest_has_a_row_per_file(cat, tmp_path):
    load(cat, tmp_path)
    m = {r["artifact"]: r for r in rows(cat, "provenance.release")}
    assert {r["source"] for r in m.values()} == {"ncbi_gene"}
    assert m["gene_orthologs"]["row_count"] == 5 and m["gene_group"]["row_count"] == 2
    assert m["gene_group"]["url"].endswith("gene_group.gz")
    assert {r["version_method"] for r in m.values()} == {"retrieval_date"}


def test_every_column_is_documented(cat, tmp_path):
    load(cat, tmp_path)
    for identifier in ("raw.ncbi__gene_orthologs", "raw.ncbi__gene_group", "annotation.ortholog"):
        table = cat.load_table(identifier)
        assert table.properties.get("comment"), identifier
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"
