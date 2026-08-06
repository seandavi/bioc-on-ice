"""Round-trip: GTF -> Iceberg -> query. Run with `uv run pytest`."""

from pathlib import Path

import duckdb

from bioconice import catalog, ensembl

GTF = Path(__file__).parent / "tiny.gtf"
HUMAN = {"taxon_id": 9606, "assembly": "GRCh38.p14", "accession": "GCA_000001405.29"}
MOUSE = {"taxon_id": 10090, "assembly": "GRCm39", "accession": "GCA_000001635.9"}


def load(cat, info, release="116"):
    con = duckdb.connect()
    ensembl.parse(con, str(GTF))
    for identifier, arrow in ensembl.tables(con, release, info).items():
        ensembl.write(cat, identifier, arrow, info["taxon_id"])


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def test_roundtrip(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    cat = catalog()
    load(cat, HUMAN)

    genes = rows(cat, "annotation.gene")
    assert len(genes) == 2
    tp53 = next(g for g in genes if g["symbol"] == "TP53")
    assert (tp53["gene_id"], tp53["stable_id"]) == ("ENSG00000141510.18", "ENSG00000141510")
    assert tp53["gene_type"] == "protein_coding"
    # a gene with no gene_name attribute keeps a null symbol, not ""
    assert next(g for g in genes if g["stable_id"] == "ENSG00000288825")["symbol"] is None

    tx = rows(cat, "annotation.transcript")
    assert len(tx) == 3
    assert sum(t["canonical"] for t in tx) == 2
    assert {t["gene_id"] for t in tx} <= {g["gene_id"] for g in genes}

    exons = rows(cat, "annotation.exon", row_filter="transcript_id = 'ENST00000269305.9'")
    assert [(e["start"], e["end"], e["strand"]) for e in exons] == [
        (7687377, 7687550, "-"), (7676521, 7676622, "-")]

    ids = rows(cat, "annotation.identifier_mapping")
    assert [i["target_id"] for i in ids] == ["TP53"]  # the unnamed gene is skipped

    # re-ingesting a release replaces its rows rather than duplicating them
    load(cat, HUMAN)
    assert len(rows(cat, "annotation.gene")) == 2

    # ...and another species lands beside it, not on top of it
    load(cat, MOUSE)
    genes = rows(cat, "annotation.gene")
    assert len(genes) == 4
    assert {g["taxon_id"] for g in genes} == {9606, 10090}
    assert len(rows(cat, "reference.genome")) == 2
