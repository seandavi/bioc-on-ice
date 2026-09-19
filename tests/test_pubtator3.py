"""PubTator3: five dumps land whole -> one mention table merged in shards of pmid.

Offline: the fixtures are a few real-shaped lines per file, gzipped into a temp
directory that stands in for NCBI's FTP directory. Run with `uv run pytest`.
"""

import gzip

import pytest

from bioconice import catalog, pubtator3

REL = "2026.09"

# Lines as upstream prints them: no header, '-' as data, stray quotes, a ';'-joined
# gene id, a '|'-joined resource list in either order, an empty mentions column.
DUMP = {
    "gene": ("100\tGene\t7157\tp53|TP53\tPubTator3|gene2pubmed\n"
             "100\tGene\t6900;1491938\tTax\tPubTator3\n"
             "100\tGene\t6900\t-\tPubTator3\n"
             "200\tGene\t7099\tToll\"-like receptor (4|TLR(4\tPubTator3\n"),
    "disease": "100\tDisease\tMESH:D009369\ttumor|Tumor\tMESH|PubTator3\n",
    "chemical": ("100\tChemical\t-\tBE9|GCAL-9 bioactive extract\tPubTator3\n"
                 "200\tChemical\tMESH:D004875\tergosterol\tPubTator3\n"),
    "species": "100\tSpecies\t9606\tpatients\tPubTator3\n",
    "mutation": ("100\tMutation\ttmVar:c|SUB|A|2063|G;HGVS:c.2063A>G;VariantGroup:0\tA2063G\tPubTator3\n"
                 "200\tMutation\trs121434568\t\tClinVar|dbSNP\n"),
}


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path / "wh"))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def dump(tmp_path, name, files):
    d = tmp_path / name
    d.mkdir()
    for kind, text in files.items():
        with gzip.open(d / f"{kind}2pubtator3.gz", "wt") as f:
            f.write(text)
    return f"{d}/"


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def key(r):
    return r["pmid"], r["concept_type"], r["concept_id"], r["resource"]


def test_raw_lands_whole_and_verbatim_with_a_manifest_row_per_file(cat, tmp_path):
    counts = pubtator3.ingest(cat, REL, dump(tmp_path, "d1", DUMP))
    assert {k: v for k, v in counts.items() if k.startswith("raw.")} == {
        "raw.pubtator3__gene": 4, "raw.pubtator3__disease": 1, "raw.pubtator3__chemical": 2,
        "raw.pubtator3__species": 1, "raw.pubtator3__mutation": 2}
    gene = {(r["concept_id"], r["mentions"]) for r in rows(cat, "raw.pubtator3__gene")}
    # '-' is data, quotes are data, the ';'-joined id is not split on the way in
    assert gene == {("7157", "p53|TP53"), ("6900;1491938", "Tax"), ("6900", "-"),
                    ("7099", 'Toll"-like receptor (4|TLR(4')}
    assert [r["concept_id"] for r in rows(cat, "raw.pubtator3__chemical")
            if r["pmid"] == "100"] == ["-"]
    mut = {r["pmid"]: r for r in rows(cat, "raw.pubtator3__mutation")}
    assert mut["200"]["mentions"] is None and mut["200"]["resource"] == "ClinVar|dbSNP"
    man = {r["artifact"]: r for r in rows(cat, "provenance.release")}
    assert {r["source"] for r in man.values()} == {"pubtator3"}
    assert {a: man[a]["row_count"] for a in man} == {
        "gene": 4, "disease": 1, "chemical": 2, "species": 1, "mutation": 2}
    assert man["gene"]["version_method"] == "retrieval_date"
    assert man["gene"]["url"].endswith("gene2pubtator3.gz")


def test_mentions_are_exploded_by_resource_and_concept_and_sharded(cat, tmp_path):
    pubtator3.ingest(cat, REL, dump(tmp_path, "d1", DUMP))
    got = rows(cat, "annotation.pubtator3__mention")
    tmvar = "tmVar:c|SUB|A|2063|G;HGVS:c.2063A>G;VariantGroup:0"
    assert {key(r) for r in got} == {
        ("100", "Gene", "7157", "PubTator3"), ("100", "Gene", "7157", "gene2pubmed"),
        # '6900;1491938' splits, and its 6900 collapses into the plain 6900 row
        ("100", "Gene", "6900", "PubTator3"), ("100", "Gene", "1491938", "PubTator3"),
        ("200", "Gene", "7099", "PubTator3"),
        ("100", "Disease", "MESH:D009369", "MESH"), ("100", "Disease", "MESH:D009369", "PubTator3"),
        # the un-normalised chemical ('-') is not a concept and is not derived
        ("200", "Chemical", "MESH:D004875", "PubTator3"),
        ("100", "Species", "9606", "PubTator3"),
        # a tmVar id keeps its ';': it is one identifier
        ("100", "Mutation", tmvar, "PubTator3"),
        ("200", "Mutation", "rs121434568", "ClinVar"), ("200", "Mutation", "rs121434568", "dbSNP")}
    assert len(got) == 12
    assert all(r["shard"] == int(r["pmid"]) % pubtator3.SHARDS for r in got)
    assert all(r["valid_from"] == REL and r["valid_to"] is None for r in got)


def test_rerun_is_a_noop(cat, tmp_path):
    base = dump(tmp_path, "d1", DUMP)
    pubtator3.ingest(cat, REL, base)
    again = [c for k, c in pubtator3.ingest(cat, REL, base).items() if k.startswith("annotation.")]
    assert sum(c["written"] for c in again) == 0 and sum(c["unchanged"] for c in again) == 12
    assert len(rows(cat, "raw.pubtator3__gene")) == 4


def test_next_dump_retires_and_adds_but_ignores_mention_and_resource_order_churn(cat, tmp_path):
    pubtator3.ingest(cat, REL, dump(tmp_path, "d1", DUMP))
    nxt = dict(DUMP)
    # the species hit is withdrawn and a new one appears; elsewhere only the things
    # that churn move: a new surface form, and the resource list printed in the other order
    nxt["species"] = "200\tSpecies\t10090\tmice\tPubTator3\n"
    nxt["disease"] = "100\tDisease\tMESH:D009369\ttumor|Tumor|tumour\tPubTator3|MESH\n"
    counts = pubtator3.ingest(cat, "2026.10", dump(tmp_path, "d2", nxt))
    der = [c for k, c in counts.items() if k.startswith("annotation.")]
    assert sum(c.get("new", 0) for c in der) == 1
    assert sum(c.get("retired", 0) for c in der) == 1
    assert sum(c["unchanged"] for c in der) == 11
    got = {key(r): r for r in rows(cat, "annotation.pubtator3__mention")}
    assert got[("100", "Species", "9606", "PubTator3")]["valid_to"] == "2026.10"
    assert got[("200", "Species", "10090", "PubTator3")]["valid_from"] == "2026.10"
    assert len(got) == 13
    # raw holds the latest dump only, mention strings included
    assert [r["mentions"] for r in rows(cat, "raw.pubtator3__disease")] == ["tumor|Tumor|tumour"]


def test_wrong_type_in_a_file_fails_before_any_write(cat, tmp_path):
    bad = dict(DUMP, species="100\tGene\t9606\tpatients\tPubTator3\n")
    with pytest.raises(ValueError, match="raw.pubtator3__species"):
        pubtator3.ingest(cat, REL, dump(tmp_path, "bad", bad))
    assert "annotation.pubtator3__mention" not in {".".join(t) for ns in cat.list_namespaces()
                                                   for t in cat.list_tables(ns)}


def test_every_column_is_documented():
    from bioconice import schemas
    for identifier, d in schemas.TABLES.items():
        if "pubtator3__" in identifier:
            assert d.comment and all(f.doc for f in d.schema.fields), identifier
