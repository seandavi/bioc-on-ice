"""MANE summary: land whole -> derive the matched pairs + xrefs -> coexist with other writers.

The fixture is handcrafted at test time in the real file's dialect — the header
verbatim, empty cells for missing, '-' for the minus strand — so the test never
touches the network.
"""

import pyarrow as pa
import pytest
from pyiceberg.expressions import And, EqualTo

from bioconice import catalog, mane, merge

REL = "2026.08"
DATA = [
    "GeneID:7157\tENSG00000141510.21\tHGNC:11998\tTP53\ttumor protein p53\tNM_000546.6\t"
    "NP_000537.3\tENST00000269305.9\tENSP00000269305.4\tMANE Select\tNC_000017.11\t7668421\t7687490\t-",
    # one gene, two rows: Select plus a Plus Clinical
    "GeneID:6334\tENSG00000196876.19\tHGNC:10596\tSCN8A\tsodium voltage-gated channel alpha subunit 8\t"
    "NM_001330260.2\tNP_001317189.1\tENST00000627620.5\tENSP00000487583.2\tMANE Select\t"
    "NC_000012.12\t51590266\t51812864\t+",
    "GeneID:6334\tENSG00000196876.19\tHGNC:10596\tSCN8A\tsodium voltage-gated channel alpha subunit 8\t"
    "NM_014191.4\tNP_055006.1\tENST00000354534.11\tENSP00000346534.4\tMANE Plus Clinical\t"
    "NC_000012.12\t51591233\t51812864\t+",
    # non-coding: no proteins, and here no HGNC id either — empty cells
    "GeneID:999999\tENSG00000999999.1\t\tNCRNA1\ta non-coding gene\tNR_000001.1\t\t"
    "ENST00000999999.1\t\tMANE Select\tNT_187633.1\t100\t900\t-",
]


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def write(path, header, data):
    path.write_text("\n".join(["\t".join(header), *data]) + "\n")
    return str(path)


@pytest.fixture
def tsv(tmp_path):
    return write(tmp_path / "MANE.GRCh38.v1.5.summary.txt", mane.COLUMNS, DATA)


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def live(cat, source):
    return rows(cat, "annotation.identifier_mapping",
                row_filter=f"valid_to IS NULL AND source = '{source}'")


def test_raw_is_verbatim_and_whole(cat, tsv):
    version, n = mane.land_raw(cat, REL, url=tsv)
    assert (version, n) == ("1.5", 4)

    raw = {r["refseq_nuc"]: r for r in rows(cat, "raw.ncbi__mane_summary")}
    tp53 = raw["NM_000546.6"]
    assert len(tp53) == len(mane.COLUMNS) + 2
    # verbatim: MANE's GeneID prefix and the id versions are untouched
    assert (tp53["ncbi_geneid"], tp53["ensembl_nuc"]) == ("GeneID:7157", "ENST00000269305.9")
    # '-' is the minus strand, not a missing marker; the empty cell is
    assert tp53["chr_strand"] == "-" and tp53["chr_start"] == "7668421"
    nc = raw["NR_000001.1"]
    assert nc["chr_strand"] == "-" and nc["hgnc_id"] is None and nc["refseq_prot"] is None

    # re-landing the same version replaces it rather than appending
    mane.land_raw(cat, REL, url=tsv)
    assert len(rows(cat, "raw.ncbi__mane_summary")) == 4


def test_version_comes_from_the_file_name_or_not_at_all(cat, tsv, tmp_path):
    mane.land_raw(cat, REL, url=tsv)
    m = next(r for r in rows(cat, "provenance.release") if r["source"] == "mane")
    assert (m["version_method"], m["source_version"], m["row_count"]) == ("release_number", "1.5", 4)
    assert m["url"] == tsv

    with pytest.raises(SystemExit, match="does not name its release"):
        mane.land_raw(cat, REL, url=write(tmp_path / "summary.txt", mane.COLUMNS, DATA))


def test_a_changed_header_fails_before_landing(cat, tmp_path):
    header = [*mane.COLUMNS[:10], "MANE_flag", *mane.COLUMNS[10:]]
    with pytest.raises(SystemExit, match="MANE_flag"):
        mane.land_raw(cat, REL, url=write(tmp_path / "MANE.GRCh38.v9.9.summary.txt", header, []))


def test_derives_matched_pairs_and_cross_references(cat, tsv):
    counts = mane.ingest(cat, REL, url=tsv)
    assert counts["raw.ncbi__mane_summary"] == 4

    t = {r["ensembl_transcript_id"]: r for r in rows(cat, "annotation.mane__transcript")}
    assert len(t) == 4
    tp53 = t["ENST00000269305"]
    # RefSeq keeps its version, Ensembl is split: each as the catalog already writes it
    assert (tp53["refseq_rna"], tp53["ensembl_transcript_version"], tp53["refseq_protein"],
            tp53["ensembl_protein_id"], tp53["ensembl_protein_version"]) == (
        "NM_000546.6", "9", "NP_000537.3", "ENSP00000269305", "4")
    assert (tp53["gene_id"], tp53["ensembl_gene_id"], tp53["hgnc_id"], tp53["taxon_id"]) == (
        "7157", "ENSG00000141510", "HGNC:11998", 9606)
    # a gene can have a Plus Clinical beside its Select
    assert sorted(r["mane_status"] for r in t.values() if r["gene_id"] == "6334") == [
        "MANE Plus Clinical", "MANE Select"]
    assert t["ENST00000999999"]["ensembl_protein_id"] is None

    xrefs = {(r["source_namespace"], r["source_id"], r["target_namespace"], r["target_id"])
             for r in live(cat, "MANE")}
    assert len(xrefs) == 4
    assert ("REFSEQ_RNA", "NM_000546.6", "ENSEMBL_TRANSCRIPT", "ENST00000269305") in xrefs
    assert {r["taxon_id"] for r in live(cat, "MANE")} == {9606}


def test_rerun_is_a_noop_and_a_version_bump_is_a_new_version(cat, tsv, tmp_path):
    version, _ = mane.land_raw(cat, REL, url=tsv)
    mane.transform(cat, REL, version)
    counts = mane.transform(cat, "2026.09", version)
    assert {k: (c["written"], c["unchanged"]) for k, c in counts.items()} == {
        "annotation.mane__transcript": (0, 4), "annotation.identifier_mapping": (0, 4)}

    # MANE 1.6 bumps TP53's RefSeq version and drops the Plus Clinical
    later = write(tmp_path / "MANE.GRCh38.v1.6.summary.txt", mane.COLUMNS,
                  [DATA[0].replace("NM_000546.6", "NM_000546.7"), DATA[1], DATA[3]])
    mane.ingest(cat, "2026.10", url=later)
    history = sorted((r["refseq_rna"], r["valid_from"], r["valid_to"])
                     for r in rows(cat, "annotation.mane__transcript")
                     if r["gene_id"] in ("7157", "6334"))
    assert history == [("NM_000546.6", REL, "2026.10"), ("NM_000546.7", "2026.10", None),
                       ("NM_001330260.2", REL, None), ("NM_014191.4", REL, "2026.10")]
    # both versions stay in raw
    assert {r["mane_version"] for r in rows(cat, "raw.ncbi__mane_summary")} == {"1.5", "1.6"}


def test_mane_and_another_writer_do_not_retire_each_other(cat, tsv):
    """The flip-flop regression (ADR-0004): ncbi_accession writes REFSEQ_RNA for the same taxon."""
    other = pa.Table.from_pylist([{
        "source_namespace": "ENTREZ", "source_id": "7157", "target_namespace": "REFSEQ_RNA",
        "target_id": "NM_000546.6", "taxon_id": 9606, "source": "NCBI_ACCESSION",
        "confidence": None}], schema=pa.schema([
            ("source_namespace", pa.string()), ("source_id", pa.string()),
            ("target_namespace", pa.string()), ("target_id", pa.string()),
            ("taxon_id", pa.int32()), ("source", pa.string()), ("confidence", pa.float64())]))
    scope = And(EqualTo("taxon_id", 9606), EqualTo("source", "NCBI_ACCESSION"))
    merge.merge(cat, "annotation.identifier_mapping", other, REL, scope)
    mane.ingest(cat, REL, url=tsv)

    assert merge.merge(cat, "annotation.identifier_mapping", other, "2026.09", scope)["written"] == 0
    assert len(live(cat, "MANE")) == 4
    counts = mane.ingest(cat, "2026.10", url=tsv)
    assert counts["annotation.identifier_mapping"]["written"] == 0
    assert len(live(cat, "NCBI_ACCESSION")) == 1


def test_every_column_is_documented(cat, tsv):
    """SPEC.md section B1, for the two tables this source adds."""
    mane.ingest(cat, REL, url=tsv)
    for identifier in ("raw.ncbi__mane_summary", "annotation.mane__transcript"):
        table = cat.load_table(identifier)
        assert table.properties.get("comment"), identifier
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"
