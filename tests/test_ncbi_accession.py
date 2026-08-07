"""gene2accession: land whole -> derive accession xrefs -> coexist with ncbi.py.

The fixture is handcrafted and gzipped at test time, so the test exercises the
.gz read path without a binary blob in git — and never touches the real
~1e9-row file.
"""

import gzip
from pathlib import Path

import pytest

from bioconice import catalog, ncbi, ncbi_accession

REL = "2026.08"

HEADER = "\t".join([
    "#tax_id", "GeneID", "status", "RNA_nucleotide_accession.version",
    "RNA_nucleotide_gi", "protein_accession.version", "protein_gi",
    "genomic_nucleotide_accession.version", "genomic_nucleotide_gi",
    "start_position_on_the_genomic_accession",
    "end_position_on_the_genomic_accession", "orientation", "assembly",
    "mature_peptide_accession.version", "mature_peptide_gi", "Symbol",
])

DATA = [
    # RefSeq record: versioned RNA + protein; NC_ genomic is RefSeq, so it must
    # NOT surface as GENBANK_GENOMIC
    ["9606", "7157", "REVIEWED", "NM_000546.6", "1519245064", "NP_000537.3",
     "120407068", "NC_000017.11", "568815581", "7668401", "7687549", "-",
     "Reference GRCh38.p14 Primary Assembly", "-", "-", "TP53"],
    # same RNA/protein on a second placement: the derive must stay DISTINCT
    ["9606", "7157", "REVIEWED", "NM_000546.6", "1519245064", "NP_000537.3",
     "120407068", "NW_003315952.2", "-", "-", "-", "?", "-", "-", "-", "TP53"],
    # GenBank submission: status is '-', and GenBank RNA/protein accessions
    # (no underscore) are not derived
    ["9606", "7157", "-", "AF307851.1", "12751130", "AAH03596.1", "13097747",
     "-", "-", "-", "-", "-", "-", "-", "-", "TP53"],
    # GenBank genomic placement: this one IS derived
    ["9606", "7157", "-", "-", "-", "-", "-", "AC087388.1", "12408591", "-",
     "-", "-", "-", "-", "-", "TP53"],
    # mouse: raw lands whole even when only human is derived
    ["10090", "22059", "VALIDATED", "NM_011640.3", "755522516", "NP_035770.2",
     "6755881", "NC_000077.7", "-", "-", "-", "-", "Reference GRCm39 C57BL/6J",
     "-", "-", "Trp53"],
]


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


@pytest.fixture
def g2a(tmp_path):
    text = "\n".join(["\t".join(r) for r in [HEADER.split("\t"), *DATA]]) + "\n"
    path = tmp_path / "tiny_gene2accession.tsv.gz"
    path.write_bytes(gzip.compress(text.encode()))
    return str(path)


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def live(cat, source):
    return rows(cat, "annotation.identifier_mapping",
                row_filter=f"valid_to IS NULL AND source = '{source}'")


def maps(cat):
    """Live gene2accession xrefs as {(source_namespace, target_namespace): [target_id]}."""
    out = {}
    for r in live(cat, "NCBI_ACCESSION"):
        out.setdefault((r["source_namespace"], r["target_namespace"]), []).append(r["target_id"])
    return {k: sorted(v) for k, v in out.items()}


def test_raw_is_verbatim_whole_and_gzipped(cat, g2a):
    n = ncbi_accession.land_raw(cat, REL, url=g2a)
    assert n == 5

    raw = rows(cat, "raw.ncbi__gene2accession")
    # whole: mouse lands even though only human is ever derived here
    assert {r["taxon_id"] for r in raw} == {9606, 10090}
    # '-' is NCBI's missing marker and reads as NULL
    genbank = next(r for r in raw if r["genomic_nucleotide_accession_version"] == "AC087388.1")
    assert genbank["status"] is None and genbank["rna_nucleotide_accession_version"] is None
    # accession versions and positions arrive unparsed, exactly as published
    refseq = next(r for r in raw if r["genomic_nucleotide_accession_version"] == "NC_000017.11")
    assert refseq["rna_nucleotide_accession_version"] == "NM_000546.6"
    assert refseq["start_position_on_the_genomic_accession"] == "7668401"
    assert {r["landed_in"] for r in raw} == {REL}


def test_manifest_uses_the_retrieval_date(cat, g2a):
    ncbi_accession.land_raw(cat, REL, url=g2a)
    m = next(r for r in rows(cat, "provenance.release")
             if r["source"] == "ncbi_gene2accession")
    assert m["version_method"] == "retrieval_date"
    assert m["row_count"] == 5 and m["retrieved_at"].startswith("20")


def test_derives_refseq_and_genbank_namespaces(cat, g2a):
    ncbi_accession.land_raw(cat, REL, url=g2a)
    ncbi_accession.transform(cat, REL, 9606)
    m = maps(cat)

    # versions kept as the file gives them; two placements collapse to one row
    assert m[("ENTREZ", "REFSEQ_RNA")] == ["NM_000546.6"]
    assert m[("ENTREZ", "REFSEQ_PROTEIN")] == ["NP_000537.3"]
    # GenBank genomic surfaces; RefSeq genomic (NC_/NW_) deliberately does not
    assert m[("ENTREZ", "GENBANK_GENOMIC")] == ["AC087388.1"]
    all_targets = {t for v in m.values() for t in v}
    assert not {"NC_000017.11", "NW_003315952.2", "AF307851.1", "AAH03596.1"} & all_targets

    # only the transformed taxon is derived, under this writer's own source
    assert {r["taxon_id"] for r in live(cat, "NCBI_ACCESSION")} == {9606}
    assert "NM_011640.3" not in all_targets


def test_rerun_is_a_noop_not_a_churn(cat, g2a):
    ncbi_accession.land_raw(cat, REL, url=g2a)
    ncbi_accession.transform(cat, REL, 9606)
    counts = ncbi_accession.transform(cat, "2026.09", 9606)
    assert counts["annotation.identifier_mapping"]["written"] == 0
    assert counts["annotation.identifier_mapping"]["unchanged"] == 3


NCBI_URLS = {name: str(Path(__file__).parent / f"tiny_{name}.tsv")
             for name in ("gene2ensembl", "gene_info", "gene_history")}


def test_two_ncbi_writers_do_not_retire_each_other(cat, g2a):
    """The flip-flop regression (ADR-0004): ncbi.py merges under source='NCBI',
    this module under 'NCBI_ACCESSION'. Sharing 'NCBI' from a separate merge
    call would make the two calls retire each other's rows on every run."""
    ncbi.land_raw(cat, REL, urls=NCBI_URLS)
    ncbi.transform(cat, REL, 9606)
    ncbi_accession.land_raw(cat, REL, url=g2a)
    ncbi_accession.transform(cat, REL, 9606)

    n_ncbi, n_acc = len(live(cat, "NCBI")), len(live(cat, "NCBI_ACCESSION"))
    assert n_ncbi and n_acc

    # re-running either writer leaves the other's live rows untouched
    ncbi.transform(cat, "2026.09", 9606)
    assert len(live(cat, "NCBI_ACCESSION")) == n_acc
    counts = ncbi_accession.transform(cat, "2026.10", 9606)
    assert len(live(cat, "NCBI")) == n_ncbi
    assert counts["annotation.identifier_mapping"]["written"] == 0


def test_every_column_is_documented(cat, g2a):
    """SPEC.md section B1, for the one table this source adds."""
    ncbi_accession.land_raw(cat, REL, url=g2a)
    table = cat.load_table("raw.ncbi__gene2accession")
    assert table.properties.get("comment")
    for f in table.schema().fields:
        assert f.doc, f"raw.ncbi__gene2accession.{f.name} has no doc"


def test_landing_a_url_with_no_rows_fails_loudly(cat, tmp_path):
    """Otherwise a bad URL leaves the previous landing in place and reports success."""
    empty = tmp_path / "empty.tsv.gz"
    empty.write_bytes(gzip.compress((HEADER + "\n").encode()))
    with pytest.raises(SystemExit, match="yielded no rows"):
        ncbi_accession.land_raw(cat, REL, url=str(empty))
