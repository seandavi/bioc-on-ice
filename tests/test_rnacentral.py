"""RNAcentral id mappings: land whole -> derive xrefs + RNA type -> coexist with other writers.

The fixture is handcrafted in the real file's dialect — no header, six
tab-separated columns, an empty last cell where there is no gene name — and
gzipped at test time, so the test never touches the 264M-row file.
"""

import gzip
from pathlib import Path

import pytest

from bioconice import catalog, ensembl, hgnc, ncbi, rnacentral

REL = "2026.08"
MIR = "URS0000626831"

DATA = [
    # one human pre-miRNA across the member databases. The GENCODE row repeats
    # the ENSEMBL one, as every GENCODE row does in the real file.
    [MIR, "ENSEMBL", "ENST00000408549", "9606", "pre_miRNA", "ENSG00000221476.2"],
    [MIR, "ENSEMBL_GENCODE", "ENST00000408549", "9606", "pre_miRNA", "ENSG00000221476.2"],
    [MIR, "HGNC", "HGNC:35391", "9606", "pre_miRNA", "MIR1827"],
    [MIR, "MIRBASE", "MI0008195", "9606", "pre_miRNA", "hsa-mir-1827"],
    [MIR, "REFSEQ", "NR_031728", "9606", "pre_miRNA", "MIR1827"],
    # two rows differing only in gene name: one mapping
    [MIR, "RFAM", "RF02028", "9606", "pre_miRNA", "CM000674.2/100189884-100189949"],
    [MIR, "RFAM", "RF02028", "9606", "pre_miRNA", "URS0000626831_9606/1-66"],
    # the same sequence in another organism is another RNA
    [MIR, "ENA", "LT000001.1:1..66:precursor_RNA", "9598", "pre_miRNA", ""],
    # mouse and a bacterium: raw lands whole even when only human is derived
    ["URS0000007529", "MGI", "MGI:102485", "10090", "tRNA", "mt-Th"],
    ["URS0000538405", "ENA", "KY412467.1:110326..110446:rRNA", "349476", "rRNA", "null"],
]

# The next release: miRBase withdraws its record and snoDB asserts a new one.
NEXT = [r for r in DATA if r[1] != "MIRBASE"] + [
    [MIR, "SNODB", "snoDB0001", "9606", "pre_miRNA", ""]]


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def gz(path, data):
    path.write_bytes(gzip.compress("".join("\t".join(r) + "\n" for r in data).encode()))
    return str(path)


@pytest.fixture
def tsv(tmp_path):
    return gz(tmp_path / "tiny_id_mapping.tsv.gz", DATA)


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def live(cat, source="RNACENTRAL"):
    return rows(cat, "annotation.identifier_mapping",
                row_filter=f"valid_to IS NULL AND source = '{source}'")


def targets(cat):
    return sorted((r["source_id"], r["target_namespace"], r["target_id"]) for r in live(cat))


def test_raw_is_verbatim_and_whole(cat, tsv):
    assert rnacentral.land_raw(cat, REL, "27", tsv) == ("27", 10)

    raw = rows(cat, "raw.rnacentral__id_mapping")
    assert {r["taxon_id"] for r in raw} == {9606, 9598, 10090, 349476}
    assert {r["database"] for r in raw} >= {"ENSEMBL", "ENSEMBL_GENCODE", "REFSEQ", "ENA"}
    # an empty cell is the missing marker; upstream's literal 'null' is data
    assert next(r for r in raw if r["taxon_id"] == 9598)["gene_name"] is None
    assert next(r for r in raw if r["taxon_id"] == 349476)["gene_name"] == "null"
    assert {(r["rnacentral_release"], r["landed_in"]) for r in raw} == {("27", REL)}


def test_manifest_records_the_release_number(cat, tsv):
    rnacentral.land_raw(cat, REL, "27", tsv)
    m = next(r for r in rows(cat, "provenance.release") if r["source"] == "rnacentral")
    assert (m["source_version"], m["version_method"], m["row_count"]) == ("27", "release_number", 10)


def test_a_local_copy_must_say_which_release_it_is(cat, tsv):
    with pytest.raises(SystemExit, match="--rnacentral-release"):
        rnacentral.land_raw(cat, REL, url=tsv)


def test_derives_taxon_qualified_ids_under_the_shared_namespaces(cat, tsv):
    rnacentral.land_raw(cat, REL, "27", tsv)
    rnacentral.transform(cat, REL, 9606)

    human = f"{MIR}_9606"
    assert targets(cat) == [
        # ENSEMBL + ENSEMBL_GENCODE fold into one transcript mapping, not 'ENSEMBL' (genes)
        (human, "ENSEMBL_TRANSCRIPT", "ENST00000408549"),
        (human, "HGNC", "HGNC:35391"),
        (human, "MIRBASE", "MI0008195"),
        (human, "REFSEQ_RNA", "NR_031728"),
        (human, "RFAM", "RF02028"),
    ]
    assert {(r["source_namespace"], r["taxon_id"]) for r in live(cat)} == {("RNACENTRAL", 9606)}
    # only the transformed taxon is derived
    assert rows(cat, "annotation.rnacentral__rna") == [
        {"urs_taxid": human, "taxon_id": 9606, "rna_type": "pre_miRNA",
         "valid_from": REL, "valid_to": None}]


def test_rerun_is_a_noop(cat, tsv):
    rnacentral.land_raw(cat, REL, "27", tsv)
    rnacentral.transform(cat, REL, 9606)
    counts = rnacentral.transform(cat, "2026.09", 9606)
    assert counts["annotation.identifier_mapping"] == {"written": 0, "unchanged": 5}
    assert counts["annotation.rnacentral__rna"] == {"written": 0, "unchanged": 1}


def test_next_release_retires_one_mapping_and_adds_one(cat, tsv, tmp_path):
    rnacentral.land_raw(cat, REL, "27", tsv)
    rnacentral.transform(cat, REL, 9606)
    rnacentral.land_raw(cat, "2026.09", "28", gz(tmp_path / "next.tsv.gz", NEXT))
    counts = rnacentral.transform(cat, "2026.09", 9606)["annotation.identifier_mapping"]
    assert (counts["new"], counts["retired"], counts["unchanged"]) == (1, 1, 4)

    # raw holds the latest release only
    assert {r["rnacentral_release"] for r in rows(cat, "raw.rnacentral__id_mapping")} == {"28"}
    mirbase = [r for r in rows(cat, "annotation.identifier_mapping")
               if r["target_namespace"] == "MIRBASE"]
    assert [(r["valid_from"], r["valid_to"]) for r in mirbase] == [(REL, "2026.09")]
    assert (f"{MIR}_9606", "SNODB", "snoDB0001") in targets(cat)


def test_all_taxa_derive_in_ranges_that_retire_a_vanished_taxon(cat, tsv, tmp_path, monkeypatch):
    monkeypatch.setattr(rnacentral, "ROWS_PER_MERGE", 3)
    rnacentral.land_raw(cat, REL, "27", tsv)
    counts = rnacentral.transform(cat, REL)
    assert len(counts) > 2  # more than one range, two tables each
    assert {r["taxon_id"] for r in live(cat)} == {9606, 9598, 10090, 349476}
    assert len(live(cat)) == 8

    # The bacterium, the largest taxon id, is gone from the next release: its
    # rows sit above every remaining taxon and must still be inside a scope.
    gone = gz(tmp_path / "gone.tsv.gz", [r for r in DATA if r[3] != "349476"])
    rnacentral.land_raw(cat, "2026.09", "28", gone)
    rnacentral.transform(cat, "2026.09")
    assert {r["taxon_id"] for r in live(cat)} == {9606, 9598, 10090}
    assert len(live(cat)) == 7


HGNC_LINE = "\t".join({"hgnc_id": "HGNC:35391", "symbol": "MIR1827", "status": "Approved",
                       "entrez_id": "100302217", "ensembl_gene_id": "ENSG00000221476"}.get(c, "")
                      for c in hgnc.COLUMNS)


def test_does_not_retire_the_other_writers_rows(cat, tsv, tmp_path):
    """ADR-0004: Ensembl, NCBI and HGNC assert mappings for the same taxon, so
    this writer's scope must name its own source."""
    here = Path(__file__).parent
    info = {"taxon_id": 9606, "assembly": "GRCh38.p14", "accession": "GCA_000001405.29"}
    ensembl.land_raw(cat, REL, "x", "116", url=str(here / "tiny.gtf"), info=info)
    ensembl.transform(cat, REL, info, "116")
    ncbi.land_raw(cat, REL, urls={n: str(here / f"tiny_{n}.tsv")
                                  for n in ("gene2ensembl", "gene_info", "gene_history")})
    ncbi.transform(cat, REL, 9606)
    path = tmp_path / "tiny_hgnc.txt"
    path.write_text("\t".join(hgnc.COLUMNS) + "\n" + HGNC_LINE + "\n")
    hgnc.ingest(cat, REL, str(path))

    others = {s: len(live(cat, s)) for s in ("ENSEMBL", "NCBI", "HGNC")}
    assert all(others.values())

    rnacentral.land_raw(cat, REL, "27", tsv)
    rnacentral.transform(cat, REL, 9606)
    rnacentral.transform(cat, "2026.09")  # and the all-taxa ranges
    assert {s: len(live(cat, s)) for s in others} == others
    assert len(live(cat)) == 8

    # and the reverse: another writer's rerun leaves these alone
    ncbi.transform(cat, "2026.10", 9606)
    assert len(live(cat)) == 8


def test_every_column_is_documented(cat, tsv):
    rnacentral.land_raw(cat, REL, "27", tsv)
    rnacentral.transform(cat, REL, 9606)
    for identifier in ("raw.rnacentral__id_mapping", "annotation.rnacentral__rna"):
        table = cat.load_table(identifier)
        assert "CC0" in table.properties.get("comment")
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"
