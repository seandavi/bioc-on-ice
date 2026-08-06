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


NEXT = Path(__file__).parent / "tiny_next.gtf"


def load_from(cat, info, gtf, release, ensembl_release):
    ensembl.land_raw(cat, release, "x", ensembl_release, url=str(gtf), info=info)
    return ensembl.transform(cat, release, info, ensembl_release)


def test_merge_versions_changes_rather_than_overwriting(cat):
    load(cat, HUMAN)                                    # release 2026.08, Ensembl 116
    genes = rows(cat, "annotation.gene")
    assert {g["valid_from"] for g in genes} == {REL}
    assert all(g["valid_to"] is None for g in genes)

    # same data, later release: nothing written at all
    counts = load_from(cat, HUMAN, GTF, "2026.09", ENS)
    assert counts["annotation.gene"]["written"] == 0
    assert counts["annotation.gene"]["unchanged"] == 2

    # next upstream release: TP53 version bumped 18 -> 19, the lncRNA gone
    counts = load_from(cat, HUMAN, NEXT, "2026.10", "117")
    # one changed record costs two rows: the closed old version and the new one
    assert counts["annotation.gene"]["changed"] == 1
    assert counts["annotation.gene"]["superseded"] == 1
    assert counts["annotation.gene"]["retired"] == 1

    tp53 = sorted((g for g in rows(cat, "annotation.gene")
                   if g["gene_id"] == "ENSG00000141510"), key=lambda g: g["valid_from"])
    assert len(tp53) == 2
    # the old attribute value survives — this is what Type 1 destroyed
    assert (tp53[0]["version"], tp53[0]["valid_from"], tp53[0]["valid_to"]) == ("18", REL, "2026.10")
    assert (tp53[1]["version"], tp53[1]["valid_from"], tp53[1]["valid_to"]) == ("19", "2026.10", None)

    current = rows(cat, "annotation.gene", row_filter="valid_to IS NULL")
    assert [g["gene_id"] for g in current] == ["ENSG00000141510"]


def test_point_in_time_returns_the_attribute_of_that_release(cat):
    """The defect that motivated ADR-0006: PIT must reconstruct values, not just rows."""
    load(cat, HUMAN)
    load_from(cat, HUMAN, NEXT, "2026.10", "117")
    tp53 = [g for g in rows(cat, "annotation.gene") if g["gene_id"] == "ENSG00000141510"]
    assert [g["version"] for g in pit(tp53, REL)] == ["18"]
    assert [g["version"] for g in pit(tp53, "2026.10")] == ["19"]


def test_manifest_records_what_the_release_was_built_from(cat):
    load(cat, HUMAN)
    m = rows(cat, "provenance.release")
    assert len(m) == 1
    assert (m[0]["release"], m[0]["source"], m[0]["source_version"]) == (REL, "ensembl", ENS)
    assert m[0]["version_method"] == "release_number"
    assert m[0]["row_count"] == 11 and m[0]["retrieved_at"].startswith("20")


def pit(rows, release):
    """SPEC's point-in-time predicate."""
    return [r for r in rows
            if r["valid_from"] <= release
            and (r["valid_to"] is None or r["valid_to"] > release)]


def test_resurrection_is_a_new_version(cat):
    load_from(cat, HUMAN, GTF, "2026.08", "116")   # lncRNA present
    load_from(cat, HUMAN, NEXT, "2026.09", "117")  # lncRNA gone
    load_from(cat, HUMAN, GTF, "2026.10", "118")   # lncRNA back

    lnc = [r for r in rows(cat, "annotation.gene") if r["gene_id"] == "ENSG00000288825"]
    # two records, not one revived record
    assert len(lnc) == 2
    assert sorted((r["valid_from"], r["valid_to"]) for r in lnc) == [
        ("2026.08", "2026.09"), ("2026.10", None)]

    # the invariant that actually matters: one live row per business key
    assert len([r for r in lnc if r["valid_to"] is None]) == 1

    # disjoint intervals, so point-in-time still resolves to one row per release
    assert len(pit(lnc, "2026.08")) == 1
    assert len(pit(lnc, "2026.09")) == 0   # genuinely absent that release
    assert len(pit(lnc, "2026.10")) == 1


def test_duplicate_incoming_keys_are_rejected(cat):
    """The invariant is ours to enforce — Iceberg declares it and checks nothing."""
    import pyarrow as pa
    from pyiceberg.expressions import EqualTo
    from bioconice import merge

    load(cat, HUMAN)
    gene = cat.load_table("annotation.gene")
    cols = [f.name for f in gene.schema().fields if f.name not in ("valid_from", "valid_to")]
    one = gene.scan(row_filter="gene_id = 'ENSG00000141510'").to_arrow().select(cols)
    doubled = pa.concat_tables([one, one])          # same business key twice

    with pytest.raises(ValueError, match="more than one live row"):
        merge.merge(cat, "annotation.gene", doubled, "2026.11", EqualTo("taxon_id", 9606))
