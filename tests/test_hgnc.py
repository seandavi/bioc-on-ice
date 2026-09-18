"""HGNC: land whole -> derive nomenclature + xrefs -> coexist with the NCBI writer.

The fixture is handcrafted at test time in the real file's dialect — the header
verbatim, '|'-lists wrapped in double quotes, empty cells for missing — so the
test never touches the network.
"""

from pathlib import Path

import pytest

from bioconice import catalog, hgnc, ncbi

REL = "2026.08"


def line(**cells):
    return "\t".join(cells.get(c, "") for c in hgnc.COLUMNS)


DATA = [
    line(hgnc_id="HGNC:11998", symbol="TP53", name="tumor protein p53",
         locus_group="protein-coding gene", locus_type="gene with protein product",
         status="Approved", location="17p13.1", alias_symbol='"p53|LFS1"',
         date_approved_reserved="1986-01-01", entrez_id="7157",
         ensembl_gene_id="ENSG00000141510", ucsc_id="uc060aur.1", omim_id="191170",
         **{"pseudogene.org": "PGOHUM1", "mamit-trnadb": "7"}),
    # two OMIM ids in one quoted cell, a symbol history, and no UCSC id
    line(hgnc_id="HGNC:4641", symbol="GSTT1", name="glutathione S-transferase theta 1",
         locus_group="protein-coding gene", locus_type="gene with protein product",
         status="Approved", prev_symbol='"GSTT1A|GSTT1B"', date_symbol_changed="2010-11-25",
         entrez_id="2952", ensembl_gene_id="ENSG00000277656", omim_id='"600436|613143"'),
    # no cross-references at all: lands in raw and in hgnc__gene, maps to nothing
    line(hgnc_id="HGNC:37133", symbol="A1BG-AS1", name="A1BG antisense RNA 1",
         locus_group="non-coding RNA", locus_type="RNA, long non-coding", status="Approved"),
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
    return write(tmp_path / "tiny_hgnc.txt", hgnc.COLUMNS, DATA)


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def live(cat, source):
    return rows(cat, "annotation.identifier_mapping",
                row_filter=f"valid_to IS NULL AND source = '{source}'")


def test_raw_is_verbatim_and_whole(cat, tsv):
    version, n = hgnc.land_raw(cat, REL, url=tsv)
    assert n == 3

    raw = {r["hgnc_id"]: r for r in rows(cat, "raw.hgnc__complete_set")}
    assert set(raw) == {"HGNC:11998", "HGNC:4641", "HGNC:37133"}
    # every upstream column lands, including the two whose names had to change
    tp53 = raw["HGNC:11998"]
    assert len(tp53) == len(hgnc.COLUMNS) + 2
    assert tp53["pseudogene_org"] == "PGOHUM1" and tp53["mamit_trnadb"] == "7"
    # the quotes are the dialect; the '|' list inside them is the value, unsplit
    assert tp53["alias_symbol"] == "p53|LFS1"
    assert raw["HGNC:4641"]["omim_id"] == "600436|613143"
    # unparsed strings, and an empty cell is NULL
    assert tp53["entrez_id"] == "7157" and tp53["prev_symbol"] is None
    assert {r["landed_in"] for r in raw.values()} == {REL}
    assert {r["hgnc_version"] for r in raw.values()} == {version}

    # re-landing the same version replaces it rather than appending
    hgnc.land_raw(cat, REL, url=tsv)
    assert len(rows(cat, "raw.hgnc__complete_set")) == 3


def test_manifest_says_where_the_version_came_from(cat, tsv, tmp_path):
    version, _ = hgnc.land_raw(cat, REL, url=tsv)
    m = next(r for r in rows(cat, "provenance.release") if r["source"] == "hgnc")
    assert (m["version_method"], m["source_version"], m["row_count"]) == ("retrieval_date", version, 3)

    # a dated archive names its own version
    dated = write(tmp_path / "hgnc_complete_set_2026-09-04.txt", hgnc.COLUMNS, DATA)
    assert hgnc.land_raw(cat, "2026.09", url=dated)[0] == "2026-09-04"
    m = next(r for r in rows(cat, "provenance.release") if r["release"] == "2026.09")
    assert (m["version_method"], m["source_version"]) == ("release_number", "2026-09-04")


def test_a_changed_header_fails_before_landing(cat, tmp_path):
    """HGNC does change it: archives through 2026-08-07 carry location_sortable."""
    header = [*hgnc.COLUMNS[:7], "location_sortable", *hgnc.COLUMNS[7:]]
    with pytest.raises(SystemExit, match="location_sortable"):
        hgnc.land_raw(cat, REL, url=write(tmp_path / "old.txt", header, []))


def test_derives_nomenclature_and_cross_references(cat, tsv):
    counts = hgnc.ingest(cat, REL, url=tsv)
    assert counts["raw.hgnc__complete_set"] == 3

    genes = {g["hgnc_id"]: g for g in rows(cat, "annotation.hgnc__gene")}
    assert len(genes) == 3
    gstt1 = genes["HGNC:4641"]
    assert (gstt1["symbol"], gstt1["prev_symbol"], gstt1["date_symbol_changed"]) == (
        "GSTT1", "GSTT1A|GSTT1B", "2010-11-25")
    assert gstt1["taxon_id"] == 9606 and gstt1["valid_from"] == REL and gstt1["valid_to"] is None

    xrefs = {(r["source_id"], r["target_namespace"], r["target_id"]) for r in live(cat, "HGNC")}
    assert xrefs == {
        ("HGNC:11998", "ENTREZ", "7157"), ("HGNC:11998", "ENSEMBL", "ENSG00000141510"),
        ("HGNC:11998", "UCSC", "uc060aur.1"), ("HGNC:11998", "OMIM", "191170"),
        ("HGNC:4641", "ENTREZ", "2952"), ("HGNC:4641", "ENSEMBL", "ENSG00000277656"),
        # the '|' list is split here, not in raw
        ("HGNC:4641", "OMIM", "600436"), ("HGNC:4641", "OMIM", "613143"),
    }
    assert {(r["source_namespace"], r["taxon_id"]) for r in live(cat, "HGNC")} == {("HGNC", 9606)}


def test_rerun_is_a_noop_and_a_rename_is_a_new_version(cat, tsv, tmp_path):
    version, _ = hgnc.land_raw(cat, REL, url=tsv)
    hgnc.transform(cat, REL, version)
    counts = hgnc.transform(cat, "2026.09", version)
    assert {k: (c["written"], c["unchanged"]) for k, c in counts.items()} == {
        "annotation.hgnc__gene": (0, 3), "annotation.identifier_mapping": (0, 8)}

    # a later file renames one gene and withdraws another
    renamed = [DATA[0], DATA[1].replace("\tGSTT1\t", "\tGSTT1X\t")]
    later = write(tmp_path / "hgnc_complete_set_2026-10-06.txt", hgnc.COLUMNS, renamed)
    hgnc.ingest(cat, "2026.10", url=later)
    history = sorted((g["symbol"], g["valid_from"], g["valid_to"])
                     for g in rows(cat, "annotation.hgnc__gene") if g["hgnc_id"] != "HGNC:11998")
    assert history == [("A1BG-AS1", REL, "2026.10"), ("GSTT1", REL, "2026.10"),
                       ("GSTT1X", "2026.10", None)]


NCBI_URLS = {name: str(Path(__file__).parent / f"tiny_{name}.tsv")
             for name in ("gene2ensembl", "gene_info", "gene_history")}


def test_hgnc_and_ncbi_do_not_retire_each_other(cat, tsv):
    """The flip-flop regression (ADR-0004). NCBI asserts ENTREZ -> HGNC for the
    same taxon; a taxon-only scope would let each run retire the other's rows."""
    ncbi.land_raw(cat, REL, urls=NCBI_URLS)
    ncbi.transform(cat, REL, 9606)
    hgnc.ingest(cat, REL, url=tsv)

    n_ncbi, n_hgnc = len(live(cat, "NCBI")), len(live(cat, "HGNC"))
    assert n_ncbi and n_hgnc == 8

    # re-running either writer leaves the other's live rows untouched
    ncbi.transform(cat, "2026.09", 9606)
    assert len(live(cat, "HGNC")) == n_hgnc
    counts = hgnc.ingest(cat, "2026.10", url=tsv)
    assert len(live(cat, "NCBI")) == n_ncbi
    assert counts["annotation.identifier_mapping"]["written"] == 0


def test_every_column_is_documented(cat, tsv):
    """SPEC.md section B1, for the two tables this source adds."""
    hgnc.ingest(cat, REL, url=tsv)
    for identifier in ("raw.hgnc__complete_set", "annotation.hgnc__gene"):
        table = cat.load_table(identifier)
        assert table.properties.get("comment"), identifier
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"
