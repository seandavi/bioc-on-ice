"""gene2go: land whole -> derive per-taxon -> query, on a gzipped fixture.

Gzipped rather than plain like the other tiny_*.tsv fixtures, because the real
file only exists as gene2go.gz and the gz path through read_csv is otherwise
untested. Run with `uv run pytest`.
"""

from pathlib import Path

import pytest

from bioconice import catalog, ncbi_go

G2G = str(Path(__file__).parent / "tiny_gene2go.tsv.gz")
REL = "2026.08"


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def load(cat, release=REL, taxa=(9606,)):
    n = ncbi_go.land_raw(cat, release, url=G2G)
    counts = {}
    for taxon in taxa:
        counts.update(ncbi_go.transform(cat, release, taxon))
    return n, counts


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def test_raw_is_landed_whole_and_verbatim(cat):
    n, _ = load(cat, taxa=(9606,))
    raw = rows(cat, "raw.ncbi__gene2go")
    assert n == len(raw) == 8
    # whole: mouse lands even though only human is derived
    assert {r["taxon_id"] for r in raw} == {9606, 10090}
    assert {g["taxon_id"] for g in rows(cat, "annotation.ncbi__gene_go")} == {9606}
    # PubMed keeps its pipes, '-' reads as NULL, both duplicate TAS rows land
    a = next(r for r in raw if r["go_id"] == "GO:0000122" and r["evidence"] == "IDA")
    assert a["pubmed"] == "21873635|24051492"
    bare = next(r for r in raw if r["go_id"] == "GO:0003700")
    assert bare["evidence"] is None and bare["qualifier"] is None
    assert sum(r["go_id"] == "GO:0006915" and r["taxon_id"] == 9606 for r in raw) == 2


def test_derived_gene_go(cat):
    load(cat, taxa=(9606, 10090))
    go = rows(cat, "annotation.ncbi__gene_go")
    human = [r for r in go if r["taxon_id"] == 9606]
    # 6 human raw rows minus the two TAS rows differing only in PubMed
    assert len(human) == 5 and len(go) == 7

    # same term under two evidence codes is two annotations
    assert {r["evidence"] for r in human if r["go_id"] == "GO:0000122"} == {"IDA", "IEA"}
    # NCBI's '-' becomes empty string, never NULL: these are merge-key columns
    bare = next(r for r in human if r["go_id"] == "GO:0003700")
    assert (bare["evidence"], bare["qualifier"]) == ("", "")
    # term name and aspect ride along
    nuc = next(r for r in human if r["go_id"] == "GO:0005634")
    assert (nuc["go_term"], nuc["category"], nuc["qualifier"]) == (
        "nucleus", "Component", "located_in")

    mouse = next(r for r in go if r["taxon_id"] == 10090 and r["go_id"] == "GO:0006915")
    assert (mouse["gene_id"], mouse["evidence"]) == ("22059", "IMP")


def test_rerun_is_idempotent_not_churn(cat):
    """Same upstream state at a later release: nothing written, nothing retired.

    This is what the ''-not-NULL key rule buys: a NULL qualifier would make
    GO:0003700 retire and reappear on every merge.
    """
    load(cat, taxa=(9606,))
    _, counts = load(cat, release="2026.09", taxa=(9606,))
    assert counts["annotation.ncbi__gene_go"]["written"] == 0
    assert counts["annotation.ncbi__gene_go"]["unchanged"] == 5
    live = rows(cat, "annotation.ncbi__gene_go", row_filter="valid_to IS NULL")
    assert len(live) == 5
    assert {r["valid_from"] for r in live} == {REL}


def test_manifest_uses_retrieval_date(cat):
    load(cat)
    m = next(r for r in rows(cat, "provenance.release") if r["source"] == "ncbi_gene2go")
    assert m["version_method"] == "retrieval_date"
    assert m["row_count"] == 8
    assert m["url"].endswith("gene2go.gz")


def test_every_column_is_documented(cat):
    """SPEC.md section B1, for the two tables this source creates."""
    load(cat)
    for identifier in ("raw.ncbi__gene2go", "annotation.ncbi__gene_go"):
        table = cat.load_table(identifier)
        assert table.properties.get("comment"), identifier
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"


def test_landing_no_rows_fails_loudly(cat, tmp_path):
    """Otherwise a bad URL leaves the previous landing in place and reports success."""
    empty = tmp_path / "empty.tsv"
    empty.write_text("#tax_id\tGeneID\tGO_ID\tEvidence\tQualifier\tGO_term\tPubMed\tCategory\n")
    with pytest.raises(SystemExit, match="yielded no rows"):
        ncbi_go.land_raw(cat, REL, url=str(empty))
