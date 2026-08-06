"""Raw ingest -> transform -> query, on a fixture. Run with `uv run pytest`."""

from pathlib import Path

import pytest

from bioconice import catalog, ensembl, schemas

GTF = Path(__file__).parent / "tiny.gtf"
HUMAN = {"taxon_id": 9606, "assembly": "GRCh38.p14", "accession": "GCA_000001405.29"}
MOUSE = {"taxon_id": 10090, "assembly": "GRCm39", "accession": "GCA_000001635.9"}
REL, ENS = "2026.08", "116"


def load(cat, info):
    ensembl.land_raw(cat, REL, "x", ENS, url=str(GTF), info=info)
    return ensembl.transform(cat, REL, info, ENS)


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def test_raw_is_verbatim(cat):
    load(cat, HUMAN)
    raw = rows(cat, "raw.ensembl_gtf")
    assert len(raw) == 11  # every data line, including CDS and five_prime_utr
    assert {r["feature"] for r in raw} == {"gene", "transcript", "exon", "CDS", "five_prime_utr"}
    # the attribute blob is kept whole, so attributes we do not parse survive
    assert any("ENSP00000269305" in r["attribute"] for r in raw)
    assert {r["ensembl_release"] for r in raw} == {ENS}


def test_derived_tables(cat):
    load(cat, HUMAN)

    genes = rows(cat, "annotation.gene")
    assert len(genes) == 2
    tp53 = next(g for g in genes if g["symbol"] == "TP53")
    assert (tp53["gene_id"], tp53["version"]) == ("ENSG00000141510", "18")
    assert next(g for g in genes if g["gene_id"] == "ENSG00000288825")["symbol"] is None

    tx = rows(cat, "annotation.transcript")
    assert len(tx) == 3
    assert sum(t["canonical"] for t in tx) == 2

    exons = sorted(rows(cat, "annotation.exon", row_filter="transcript_id = 'ENST00000269305'"),
                   key=lambda e: e["rank"])
    assert [e["rank"] for e in exons] == [1, 2]
    # rank 1 is the higher coordinate on the minus strand: ordering by position
    # would reverse the transcript
    assert exons[0]["start"] > exons[1]["start"]
    # exon 1 is 5' UTR, so no coding bounds; exon 2 is coding with phase 0
    assert (exons[0]["cds_start"], exons[0]["cds_phase"]) == (None, None)
    assert (exons[1]["cds_start"], exons[1]["cds_end"], exons[1]["cds_phase"]) == (7676521, 7676622, 0)

    assert [i["target_id"] for i in rows(cat, "annotation.identifier_mapping")] == ["TP53"]


def test_transform_reruns_from_raw_without_refetch(cat):
    load(cat, HUMAN)
    # no url, no network: everything transform needs is already landed
    ensembl.transform(cat, REL, HUMAN, ENS)
    assert len(rows(cat, "annotation.gene")) == 2
    assert len(rows(cat, "annotation.exon")) == 4


def test_species_are_independent(cat):
    load(cat, HUMAN)
    load(cat, MOUSE)
    genes = rows(cat, "annotation.gene")
    assert len(genes) == 4
    assert {g["taxon_id"] for g in genes} == {9606, 10090}
    assert len(rows(cat, "raw.ensembl_gtf")) == 22


def test_every_column_is_documented(cat):
    """SPEC.md section B1: a table whose columns lack doc does not ship."""
    load(cat, HUMAN)
    for identifier in schemas.TABLES:
        table = cat.load_table(identifier)
        assert table.properties.get("comment"), identifier
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"
