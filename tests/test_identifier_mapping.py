"""annotation.identifier_mapping as a release union over cdsci-lake (#183 G2, ADR-0015 b).

Every writer's lander runs into one catalog on its own module's fixture; the lake is
then stood up from what they landed, as cdsci-lake holds it, and publish.FROM_LAKE's
union must give the landers' current rows, with the same columns and types, less what
the release drops on purpose: RNAcentral's ENA ids (ADR-0015 c) and the taxa no release
has published.

The two NCBI branches read cdsci-lake's transform models ncbi_gene.mapping and
ncbi_gene2accession.mapping, ports of the landers' SQL; they are stood up here from the
landers' rows, so what this checks for them is the branch's shape. That the models
reproduce the landers on the real dumps is the supervisor's run
(scripts/migrate/identifier-mapping-supervisor.md).
"""

import gzip

import duckdb
import pytest
import test_ensembl
import test_hgnc
import test_mane
import test_ncbi_accession
import test_rnacentral

from bioconice import catalog, ensembl, hgnc, mane, ncbi_accession, publish, rnacentral

REL = "2026.08"
NAME = "annotation.identifier_mapping"
# A human ENA row: the published taxa had ENA ids until ADR-0015 c.
ENA = [test_rnacentral.MIR, "ENA", "GU786683.1:1..200:rRNA", "9606", "pre_miRNA", ""]
UNIPROT = [("ENTREZ", "7157", "UNIPROT", "P04637", 9606, "UniProt", None),
           ("ENTREZ", "22059", "UNIPROT", "P02340", 10090, "UniProt", None)]


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def landed(con, cat, identifier):
    con.register("t", cat.load_table(identifier).scan().to_arrow())
    return "t"


def test_release_union_over_the_lake_reproduces_every_writer(cat, tmp_path):
    # Ensembl: mouse's canonical assembly and a strain; only the first writes mappings.
    for species, info, gtf in (("mus_musculus", test_ensembl.GRCM39, test_ensembl.GTF),
                               ("mus_musculus_129s1svimj", test_ensembl.STRAIN,
                                test_ensembl.STRAIN_GTF)):
        ensembl.land_raw(cat, REL, species, "116", url=str(gtf), info=info)
        ensembl.transform(cat, REL, info, "116")
    test_ensembl.load_ncbi(cat, REL, taxa=(None,))
    g2a = tmp_path / "gene2accession.gz"
    g2a.write_bytes(gzip.compress("".join(
        "\t".join(r) + "\n" for r in [test_ncbi_accession.HEADER.split("\t"),
                                      *test_ncbi_accession.DATA]).encode()))
    ncbi_accession.land_raw(cat, REL, str(g2a))
    ncbi_accession.transform(cat, REL)
    hgnc.ingest(cat, REL, test_hgnc.write(tmp_path / "tiny_hgnc.txt", hgnc.COLUMNS,
                                          test_hgnc.DATA))
    mane.ingest(cat, REL, test_mane.write(tmp_path / "MANE.GRCh38.v1.5.summary.txt",
                                          mane.COLUMNS, test_mane.DATA))
    rnacentral.ingest(cat, REL, list(rnacentral.RNA_TAXA), "27", test_rnacentral.gz(
        tmp_path / "id_mapping.tsv.gz", [*test_rnacentral.DATA, ENA]))

    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS lake")
    for s in ("ncbi_gene", "ncbi_gene2accession", "ensembl", "hgnc", "mane", "rnacentral",
              "uniprot"):
        con.execute(f"CREATE SCHEMA lake.{s}")
    lander = landed(con, cat, NAME)
    con.execute(f"CREATE TABLE lander AS SELECT * EXCLUDE (valid_from, valid_to) FROM {lander} "
                "WHERE valid_to IS NULL")
    for model, source in (("ncbi_gene.mapping", "NCBI"),
                          ("ncbi_gene2accession.mapping", "NCBI_ACCESSION")):
        con.execute(f"CREATE TABLE lake.{model} AS SELECT * EXCLUDE (confidence) FROM lander "
                    f"WHERE source = '{source}'")
    # lake.ensembl.gene and .genome carry the species entry and the release, not genome_id.
    con.execute(f"""CREATE TABLE lake.ensembl.gene AS SELECT g.* EXCLUDE (taxon_id, genome_id,
        valid_from, valid_to), taxon_id AS ncbitaxon_id, species, '116' AS ensembl_release
        FROM {landed(con, cat, 'annotation.gene')} g JOIN (VALUES
            ('{test_ensembl.MOUSE['accession']}', 'mus_musculus'),
            ('{test_ensembl.STRAIN['accession']}', 'mus_musculus_129s1svimj'))
        a(genome_id, species) USING (genome_id)""")
    con.execute("""CREATE TABLE lake.ensembl.genome AS SELECT DISTINCT ncbitaxon_id, species,
        ensembl_release FROM lake.ensembl.gene""")
    for lake, raw in (("hgnc.gene", "raw.hgnc__complete_set"),
                      ("mane.transcript", "raw.ncbi__mane_summary")):
        con.execute(f"CREATE TABLE lake.{lake} AS SELECT * EXCLUDE (landed_in) "
                    f"FROM {landed(con, cat, raw)}")
    con.execute("CREATE TABLE lake.rnacentral.id_mapping AS SELECT row_number() OVER ()::INTEGER "
                "AS line_number, * EXCLUDE (landed_in) "
                f"FROM {landed(con, cat, 'raw.rnacentral__id_mapping')}")
    con.execute("CREATE TABLE lake.uniprot.identifier_mapping (source_namespace VARCHAR, "
                "source_id VARCHAR, target_namespace VARCHAR, target_id VARCHAR, taxon_id INTEGER, "
                "source VARCHAR, confidence DOUBLE)")
    con.executemany("INSERT INTO lake.uniprot.identifier_mapping VALUES (?, ?, ?, ?, ?, ?, ?)",
                    UNIPROT)

    con.execute(f"CREATE TABLE got AS {publish.FROM_LAKE[NAME]}")
    con.execute("""CREATE TABLE want AS
        SELECT * FROM lander WHERE NOT (source = 'RNACENTRAL' AND target_namespace = 'ENA')
        UNION ALL SELECT * FROM lake.uniprot.identifier_mapping WHERE taxon_id = 9606""")
    assert con.sql("DESCRIBE got").fetchall() == con.sql("DESCRIBE want").fetchall()
    for a, b in (("got", "want"), ("want", "got")):
        assert con.sql(f"SELECT count(*) FROM (SELECT * FROM {a} EXCEPT ALL "
                       f"SELECT * FROM {b})").fetchone()[0] == 0, a
    # no branch is vacuous, and each is its writer's rows only
    assert dict(con.sql("SELECT source, count(*) FROM got GROUP BY 1").fetchall()).keys() == {
        "NCBI", "NCBI_ACCESSION", "ENSEMBL", "HGNC", "MANE", "RNACENTRAL", "UniProt"}
    # the one lander row the release drops: the human ENA id
    dropped = con.sql("SELECT * FROM lander EXCEPT ALL SELECT * FROM got").fetchall()
    assert dropped == [("RNACENTRAL", f"{test_rnacentral.MIR}_9606", "ENA", ENA[2], 9606,
                        "RNACENTRAL", None)]
    # the gate's key holds
    assert con.sql("SELECT count(*) - count(DISTINCT (source_namespace, source_id, "
                   "target_namespace, target_id, taxon_id, source)) FROM got").fetchone()[0] == 0
