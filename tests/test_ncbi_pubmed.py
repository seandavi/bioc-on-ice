"""gene2pubmed: land -> transform -> query on a gzipped fixture. Run with `uv run pytest`."""

import gzip

import duckdb
import pytest

from bioconice import catalog, ncbi_pubmed

REL = "2026.08"

# Handcrafted: two human genes, one mouse, and one duplicated link — upstream
# publishes distinct triples, but the derivation must not depend on that.
G2P = (
    "#tax_id\tGeneID\tPubMed_ID\n"
    "9606\t7157\t1000000\n"
    "9606\t7157\t2000000\n"
    "9606\t7157\t2000000\n"
    "9606\t672\t3000000\n"
    "10090\t22059\t4000000\n"
)


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


@pytest.fixture
def g2p(tmp_path):
    # Gzipped like the real dump, so the landing exercises the same read path.
    path = tmp_path / "gene2pubmed.gz"
    path.write_bytes(gzip.compress(G2P.encode()))
    return str(path)


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def test_raw_is_landed_whole_and_only_transform_is_scoped(cat, g2p):
    """Raw is not a function of what we derive: mouse lands even deriving only human."""
    n = ncbi_pubmed.land_raw(cat, REL, url=g2p)
    assert n == 5  # verbatim: the duplicate link lands too
    assert {r["taxon_id"] for r in rows(cat, "raw.ncbi__gene2pubmed")} == {9606, 10090}

    ncbi_pubmed.transform(cat, REL, 9606)
    links = rows(cat, "annotation.ncbi__gene_pubmed")
    assert {l["taxon_id"] for l in links} == {9606}
    # DISTINCT: the duplicated (7157, 2000000) link is one row, not a merge error
    assert sorted((l["gene_id"], l["pubmed_id"]) for l in links) == [
        ("672", "3000000"), ("7157", "1000000"), ("7157", "2000000")]
    assert all(l["valid_from"] == REL and l["valid_to"] is None for l in links)


def test_rerun_is_idempotent_and_taxa_are_independent(cat, g2p):
    ncbi_pubmed.ingest(cat, REL, [9606], url=g2p)
    # same data, later release: carried forward, not a churn of retire-and-reassert
    counts = ncbi_pubmed.ingest(cat, "2026.09", [9606], url=g2p)
    c = counts["annotation.ncbi__gene_pubmed [9606]"]
    assert c["written"] == 0 and c["unchanged"] == 3

    # deriving mouse later needs no re-fetch, and does not disturb human
    ncbi_pubmed.transform(cat, "2026.10", 10090)
    live = rows(cat, "annotation.ncbi__gene_pubmed", row_filter="valid_to IS NULL")
    assert {l["taxon_id"] for l in live} == {9606, 10090}
    assert {l["valid_from"] for l in live if l["taxon_id"] == 9606} == {REL}


def test_manifest_and_column_docs(cat, g2p):
    ncbi_pubmed.ingest(cat, REL, [9606], url=g2p)
    m = next(r for r in rows(cat, "provenance.release") 
             if (r["source"], r["artifact"]) == ("ncbi_gene", "gene2pubmed"))
    assert m["version_method"] == "retrieval_date"
    assert m["row_count"] == 5 and m["retrieved_at"].startswith("20")

    # SPEC.md section B1: a table whose columns lack doc does not ship
    for identifier in ("raw.ncbi__gene2pubmed", "annotation.ncbi__gene_pubmed"):
        table = cat.load_table(identifier)
        assert table.properties.get("comment"), identifier
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"


def test_landing_a_url_with_no_rows_fails_loudly(cat, tmp_path):
    """Otherwise a bad URL leaves the previous landing in place and reports success."""
    empty = tmp_path / "empty.gz"
    empty.write_bytes(gzip.compress(b"#tax_id\tGeneID\tPubMed_ID\n"))
    with pytest.raises(SystemExit, match="yielded no rows"):
        ncbi_pubmed.land_raw(cat, REL, url=str(empty))


def test_release_matches_lander(cat, g2p):
    """The lake-backed RELEASE SELECTs give the lander's current rows and types (#183).

    The lake tables are built as cdsci-lake lands them: pubmed_id named pmid, it and
    gene_id BIGINT, a snapshot_version, and one row per whole-row key, so the fixture's
    duplicated link is one row there; gene_publication is its model, columns reordered.
    """
    ncbi_pubmed.ingest(cat, REL, url=g2p)
    con = duckdb.connect()
    con.register("raw", cat.load_table("raw.ncbi__gene2pubmed").scan().to_arrow())
    con.execute("ATTACH ':memory:' AS lake; CREATE SCHEMA lake.ncbi_gene2pubmed")
    con.execute("""CREATE TABLE lake.ncbi_gene2pubmed.gene2pubmed AS SELECT DISTINCT taxon_id,
        gene_id::BIGINT AS gene_id, pubmed_id::BIGINT AS pmid, '2026-09-12' AS snapshot_version
        FROM raw""")
    con.execute("""CREATE TABLE lake.ncbi_gene2pubmed.gene_publication AS SELECT DISTINCT
        gene_id, pmid, taxon_id FROM lake.ncbi_gene2pubmed.gene2pubmed""")
    for name, sql in ncbi_pubmed.RELEASE.items():
        con.register("lander", cat.load_table(name).scan(row_filter="valid_to IS NULL"
                     if name.startswith("annotation") else "True").to_arrow())
        con.execute("CREATE OR REPLACE TABLE want AS SELECT DISTINCT * EXCLUDE "
                    f"({'valid_from, valid_to' if name.startswith('annotation') else 'landed_in'}) "
                    "FROM lander")
        con.execute(f"CREATE OR REPLACE TABLE got AS {sql}")
        assert con.sql("DESCRIBE got").fetchall() == con.sql("DESCRIBE want").fetchall(), name
        for a, b in (("got", "want"), ("want", "got")):
            assert con.sql(f"SELECT count(*) FROM (SELECT * FROM {a} EXCEPT ALL "
                           f"SELECT * FROM {b})").fetchone()[0] == 0, (name, a)
