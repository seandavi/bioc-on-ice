"""Cellosaurus: land the flat file line by line -> derive cell lines, xrefs, diseases.

The fixture is handcrafted in the real file's dialect — a header with blank
lines and a ' Version:' line, 'XX   value' body lines, '//' terminators, the ID
line ahead of AC — so the test never touches the network.
"""

import pytest

from bioconice import catalog, cellosaurus

REL = "2026.08"

HEADER = """\
----------------------------------------------------------------------------
        CALIPHO group at the SIB - Swiss Institute of Bioinformatics
----------------------------------------------------------------------------

 Description: Cellosaurus: a knowledge resource on cell lines
 Version: {version}
 Last update: 25-June-2026

 ---------  ------------------------------  -----------------------
 Line code  Content                         Occurrence in an entry
 ---------  ------------------------------  -----------------------
 ID         Identifier (cell line name)     Once; starts an entry
 //         Terminator                      Once; ends an entry

____________________________________________________________________________
"""

HELA = """\
ID   HeLa
AC   CVCL_0030
SY   HELA; Hela; He La
DR   CLO; CLO_0003684
DR   EFO; EFO_0001185
DR   DepMap; ACH-001086
DR   Wikidata; Q847482
RX   PubMed=13052828;
CC   Problematic cell line: Contaminating. Shown to be the source of many contaminations.
ST   Amelogenin: X
DI   NCIt; C27677; Human papillomavirus-related endocervical adenocarcinoma
OX   NCBI_TaxID=9606; ! Homo sapiens (Human)
SX   Female
AG   30Y6M
CA   Cancer cell line
DT   Created: 04-04-12; Last updated: 25-06-26; Version: 53
//
"""
HELA_S3 = """\
ID   HeLa S3
AC   CVCL_0058
DR   Wikidata; Q54881279
OX   NCBI_TaxID=9606; ! Homo sapiens (Human)
HI   CVCL_0030 ! HeLa
SX   Female
AG   30Y6M
CA   Cancer cell line
DT   Created: 04-04-12; Last updated: 10-04-25; Version: 31
//
"""
K562 = """\
ID   K-562
AC   CVCL_0004
SY   K562; K.562
DR   DepMap; ACH-000551
DI   NCIt; C9110; Blast phase chronic myelogenous leukemia, BCR-ABL1 positive
DI   ORDO; Orphanet_521; Chronic myeloid leukemia
OX   NCBI_TaxID=9606; ! Homo sapiens (Human)
SX   Female
AG   53Y
CA   Cancer cell line
DT   Created: 04-04-12; Last updated: 25-06-26; Version: 50
//
"""
# two species, two parents, two merged accessions, and no SX/AG lines at all
HYBRID = """\
ID   PANC-1 x rat hybrid
AC   CVCL_2257
AS   CVCL_5145; CVCL_2953
OX   NCBI_TaxID=9606; ! Homo sapiens (Human)
OX   NCBI_TaxID=10116; ! Rattus norvegicus (Rat)
HI   CVCL_0480 ! PANC-1
HI   CVCL_0004 ! K-562
CA   Hybrid cell line
DT   Created: 04-04-12; Last updated: 10-04-25; Version: 16
//
"""
ENTRIES = [HELA, HELA_S3, K562, HYBRID]


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def write(path, entries, version="56.0"):
    path.write_text(HEADER.format(version=version) + "".join(entries))
    return str(path)


@pytest.fixture
def txt(tmp_path):
    return write(tmp_path / "tiny_cellosaurus.txt", ENTRIES)


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def test_raw_is_the_file_again(cat, txt):
    version, n = cellosaurus.land_raw(cat, REL, url=txt)
    text = open(txt).read()
    assert (version, n) == ("56.0", text.count("\n"))

    raw = sorted(rows(cat, "raw.cellosaurus__release"), key=lambda r: r["line_number"])
    # verbatim and whole: header, blank lines, and the codes nothing derives
    line = lambda r: r["value"] if r["code"] is None else "//" if r["code"] == "//" else f"{r['code']}   {r['value']}"
    assert "".join(line(r) + "\n" for r in raw) == text
    assert {r["code"] for r in raw} >= {None, "CC", "ST", "RX", "DT", "//"}
    # the ID line precedes AC and still carries its entry's accession; the header has none
    assert next(r for r in raw if r["value"] == "HeLa S3")["accession"] == "CVCL_0058"
    assert {r["accession"] for r in raw if r["code"] == "//"} == {
        "CVCL_0030", "CVCL_0058", "CVCL_0004", "CVCL_2257"}
    assert all(r["accession"] is None for r in raw if r["code"] is None)
    assert {(r["landed_in"], r["cellosaurus_version"]) for r in raw} == {(REL, "56.0")}

    # re-landing the same version replaces it rather than appending
    cellosaurus.land_raw(cat, REL, url=txt)
    assert len(rows(cat, "raw.cellosaurus__release")) == n


def test_manifest_records_the_files_own_version(cat, txt):
    _, n = cellosaurus.land_raw(cat, REL, url=txt)
    m = next(r for r in rows(cat, "provenance.release") if r["source"] == "cellosaurus")
    assert (m["version_method"], m["source_version"], m["row_count"]) == ("release_number", "56.0", n)


def test_a_line_that_is_not_code_and_value_fails_before_landing(cat, tmp_path):
    with pytest.raises(SystemExit, match="1 body lines"):
        cellosaurus.land_raw(cat, REL, url=write(tmp_path / "bad.txt", [HELA, "stray text\n"]))
    with pytest.raises(SystemExit, match="Version"):
        cellosaurus.land_raw(cat, REL, url=write(tmp_path / "unversioned.txt", ENTRIES, version=""))


def test_derives_cell_lines_xrefs_and_diseases(cat, txt):
    counts = cellosaurus.ingest(cat, REL, url=txt)
    assert counts["raw.cellosaurus__release [56.0]"]

    lines = {c["accession"]: c for c in rows(cat, "annotation.cellosaurus__cell_line")}
    assert set(lines) == {"CVCL_0030", "CVCL_0058", "CVCL_0004", "CVCL_2257"}
    hela = lines["CVCL_0030"]
    assert (hela["name"], hela["synonyms"], hela["taxon_ids"], hela["sex"], hela["age"],
            hela["category"], hela["parent_accessions"]) == (
        "HeLa", "HELA|He La|Hela", [9606], "Female", "30Y6M", "Cancer cell line", None)
    assert (hela["valid_from"], hela["valid_to"]) == (REL, None)
    assert lines["CVCL_0058"]["parent_accessions"] == "CVCL_0030"
    assert lines["CVCL_0004"]["secondary_accessions"] is None

    # a hybrid has every species and every parent, sorted; a missing SX line is NULL
    hybrid = lines["CVCL_2257"]
    assert hybrid["taxon_ids"] == [9606, 10116]
    assert hybrid["parent_accessions"] == "CVCL_0004|CVCL_0480"
    assert hybrid["secondary_accessions"] == "CVCL_2953|CVCL_5145"
    assert hybrid["sex"] is None and hybrid["synonyms"] is None

    xrefs = {(x["accession"], x["database"], x["identifier"])
             for x in rows(cat, "annotation.cellosaurus__xref")}
    assert xrefs == {
        ("CVCL_0030", "CLO", "CLO_0003684"), ("CVCL_0030", "EFO", "EFO_0001185"),
        ("CVCL_0030", "DepMap", "ACH-001086"), ("CVCL_0030", "Wikidata", "Q847482"),
        ("CVCL_0058", "Wikidata", "Q54881279"), ("CVCL_0004", "DepMap", "ACH-000551")}

    diseases = {(d["accession"], d["database"], d["disease_id"], d["disease_name"])
                for d in rows(cat, "annotation.cellosaurus__disease")}
    assert diseases == {
        ("CVCL_0030", "NCIt", "C27677", "Human papillomavirus-related endocervical adenocarcinoma"),
        # the label keeps its own comma; only '; ' separates fields
        ("CVCL_0004", "NCIt", "C9110", "Blast phase chronic myelogenous leukemia, BCR-ABL1 positive"),
        ("CVCL_0004", "ORDO", "Orphanet_521", "Chronic myeloid leukemia")}


def test_rerun_is_a_noop_and_a_second_release_retires_and_adds(cat, txt, tmp_path):
    version, _ = cellosaurus.land_raw(cat, REL, url=txt)
    cellosaurus.transform(cat, REL, version)
    counts = cellosaurus.transform(cat, "2026.09", version)
    assert {k: (c["written"], c["unchanged"]) for k, c in counts.items()} == {
        "annotation.cellosaurus__cell_line": (0, 4), "annotation.cellosaurus__xref": (0, 6),
        "annotation.cellosaurus__disease": (0, 3)}

    # release 57 withdraws HeLa S3, registers a new line, and swaps one of HeLa's xrefs
    new = "ID   NewLine-1\nAC   CVCL_ZZ99\nOX   NCBI_TaxID=10090; ! Mus musculus (Mouse)\nCA   Hybridoma\n//\n"
    later = write(tmp_path / "later.txt", version="57.0", entries=[
        HELA.replace("DR   EFO; EFO_0001185\n", "DR   BTO; BTO_0000567\n"), K562, HYBRID, new])
    assert cellosaurus.ingest(cat, "2026.10", url=later)["annotation.cellosaurus__cell_line"][
        "unchanged"] == 3

    history = {(c["accession"], c["valid_from"], c["valid_to"])
               for c in rows(cat, "annotation.cellosaurus__cell_line")}
    assert history >= {("CVCL_0058", REL, "2026.10"), ("CVCL_ZZ99", "2026.10", None),
                       ("CVCL_0030", REL, None)}
    swapped = {(x["database"], x["valid_from"], x["valid_to"])
               for x in rows(cat, "annotation.cellosaurus__xref")
               if x["accession"] == "CVCL_0030" and x["database"] in ("EFO", "BTO")}
    assert swapped == {("EFO", REL, "2026.10"), ("BTO", "2026.10", None)}

    # raw keeps both versions, and the transform read only the one it was given
    assert {r["cellosaurus_version"] for r in rows(cat, "raw.cellosaurus__release")} == {"56.0", "57.0"}


def test_every_column_is_documented(cat, txt):
    """SPEC.md section B1, for the tables this source adds."""
    cellosaurus.ingest(cat, REL, url=txt)
    for identifier in ("raw.cellosaurus__release", "annotation.cellosaurus__cell_line",
                       "annotation.cellosaurus__xref", "annotation.cellosaurus__disease"):
        table = cat.load_table(identifier)
        assert "CC BY 4.0" in table.properties.get("comment", ""), identifier
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"
