"""Complex Portal: land every complextab file whole -> derive complexes and participants.

The fixture is handcrafted at test time in the real files' dialect — the header
verbatim, '"' as cell content, '-' for missing, one file per species plus the
predicted one — under a dated directory, which is where upstream puts the version.
"""

import pytest

from bioconice import catalog, complexportal

REL = "2026.08"


def line(**cells):
    return "\t".join(cells.get(c, "-") for c in complexportal.COLUMNS.values())


HUMAN = [
    line(complex_ac="CPX-663", recommended_name="TP53-MDM4 transcription regulation complex",
         aliases="TP53-MDMX complex", taxonomy_identifier="9606", participants="O15151(0)|P04637(0)",
         evidence_code="ECO:0000353(physical interaction evidence used in manual assertion)",
         experimental_evidence="intact:EBI-8000888", complex_assembly="Heterodimer",
         go_annotations="GO:0045892(negative regulation of transcription, DNA-templated)|GO:0017053(transcription repressor complex)",
         source='psi-mi:"MI:2228"(ceitec)', expanded_participants="O15151(0)|P04637(0)"),
    # a molecule set, a small molecule with known stoichiometry, an RNA, a
    # sub-complex (not flattened here; it is in the expanded list), an isoform
    line(complex_ac="CPX-1", recommended_name="Everything complex", taxonomy_identifier="9606",
         participants="[P02400,P05319](1)|CHEBI:29105(2)|URS000075BAAE_9606(1)|CPX-663(1)|P04637-2(0)|M14387(1)",
         evidence_code="ECO:0005544(biological system reconstruction evidence based on orthology evidence used in manual assertion)",
         source='psi-mi:"MI:0469"(IntAct)',
         expanded_participants="[P02400,P05319](1)|CHEBI:29105(2)|URS000075BAAE_9606(1)|O15151(0)|P04637(0)|P04637-2(0)|M14387(1)"),
]
PREDICTED = [
    line(complex_ac="CPX-9000", recommended_name="HuMAP complex 1", taxonomy_identifier="9606",
         participants="A0AVF1(0)|Q9Y366(0)",
         evidence_code="ECO:0008004(machine learning method evidence used in automatic assertion)",
         source='psi-mi:"MI:2424"(HuMap)', expanded_participants="A0AVF1(0)|Q9Y366(0)"),
]
YEAST = [
    line(complex_ac="CPX-25", recommended_name="Yeast complex", taxonomy_identifier="559292",
         participants="P38011(1)", evidence_code="ECO:0000353(physical interaction evidence used in manual assertion)",
         source='psi-mi:"MI:0484"(Saccharomyces Genome Database)', expanded_participants="P38011(1)"),
]


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path / "wh"))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def release_dir(tmp_path, version, files, header=None):
    d = tmp_path / version / "complextab"
    d.mkdir(parents=True)
    for name, data in files.items():
        (d / name).write_text("\n".join(["\t".join(header or complexportal.COLUMNS), *data]) + "\n")
    return str(d) + "/"


@pytest.fixture
def url(tmp_path):
    return release_dir(tmp_path, "2026-01-09",
                       {"9606.tsv": HUMAN, "9606_predicted.tsv": PREDICTED, "559292.tsv": YEAST})


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def test_raw_is_verbatim_and_whole(cat, url):
    assert complexportal.land_raw(cat, REL, url=url) == ("2026-01-09", 4)

    raw = {r["complex_ac"]: r for r in rows(cat, "raw.complexportal__complex")}
    # every species' file and the predicted one land
    assert {r["file"] for r in raw.values()} == {"9606.tsv", "9606_predicted.tsv", "559292.tsv"}
    tp53 = raw["CPX-663"]
    assert len(tp53) == len(complexportal.COLUMNS) + 3
    # '"' is content, lists are unsplit, '-' is NULL, numbers are text
    assert tp53["source"] == 'psi-mi:"MI:2228"(ceitec)'
    assert tp53["participants"] == "O15151(0)|P04637(0)" and tp53["ligand"] is None
    assert tp53["taxonomy_identifier"] == "9606"
    assert {(r["complexportal_version"], r["landed_in"]) for r in raw.values()} == {("2026-01-09", REL)}

    m = next(r for r in rows(cat, "provenance.release") if r["source"] == "complexportal")
    assert (m["version_method"], m["source_version"], m["row_count"]) == ("release_number", "2026-01-09", 4)

    # re-landing the same version replaces it rather than appending
    complexportal.land_raw(cat, REL, url=url)
    assert len(rows(cat, "raw.complexportal__complex")) == 4


def test_a_changed_header_or_an_undated_directory_fails_before_landing(cat, tmp_path):
    header = [*complexportal.COLUMNS, "New column"]
    bad = release_dir(tmp_path, "2026-05-01", {"9606.tsv": [HUMAN[0] + "\tx"]}, header)
    with pytest.raises(SystemExit, match="New column"):
        complexportal.land_raw(cat, REL, url=bad)
    with pytest.raises(SystemExit, match="dated directory"):
        complexportal.land_raw(cat, REL, url="https://ftp.ebi.ac.uk/pub/databases/intact/complex/current/complextab/")


def test_derives_complexes_and_participants(cat, url):
    complexportal.ingest(cat, REL, url=url)

    cx = {r["complex_ac"]: r for r in rows(cat, "annotation.complexportal__complex")}
    assert set(cx) == {"CPX-663", "CPX-1", "CPX-9000", "CPX-25"}
    tp53 = cx["CPX-663"]
    assert (tp53["taxon_id"], tp53["name"], tp53["predicted"], tp53["evidence_code"], tp53["evidence"],
            tp53["experimental_evidence"], tp53["assembly"], tp53["curated_by"]) == (
        9606, "TP53-MDM4 transcription regulation complex", False, "ECO:0000353",
        "physical interaction evidence used in manual assertion", "intact:EBI-8000888",
        "Heterodimer", "ceitec")
    assert tp53["go_annotations"].startswith("GO:0045892(") and tp53["valid_from"] == REL
    # the file name is the only thing that says predicted
    assert (cx["CPX-9000"]["predicted"], cx["CPX-9000"]["curated_by"]) == (True, "HuMap")
    assert cx["CPX-25"]["taxon_id"] == 559292

    parts = {(r["complex_ac"], r["participant_id"]): r for r in rows(cat, "annotation.complexportal__participant")}
    got = {k[1]: (r["participant_namespace"], r["stoichiometry"], r["molecule_set"])
           for k, r in parts.items() if k[0] == "CPX-1"}
    assert got == {
        # a molecule set is one row per member, the set kept beside it
        "P02400": ("UNIPROT", 1, "[P02400,P05319]"), "P05319": ("UNIPROT", 1, "[P02400,P05319]"),
        "CHEBI:29105": ("CHEBI", 2, None), "URS000075BAAE_9606": ("RNACENTRAL", 1, None),
        # a sub-complex is a participant, not flattened; (0) is 'unknown', so NULL
        "CPX-663": ("COMPLEXPORTAL", 1, None), "P04637-2": ("UNIPROT", None, None),
        # an id of no recognised form keeps a NULL namespace rather than a guess
        "M14387": (None, 1, None),
    }
    assert {k[1] for k in parts if k[0] == "CPX-663"} == {"O15151", "P04637"}
    assert len(parts) == 7 + 2 + 2 + 1


def test_a_malformed_participant_fails_rather_than_vanishing(cat, tmp_path):
    bad = release_dir(tmp_path, "2026-05-01", {"9606.tsv": [HUMAN[0].replace("P04637(0)\t", "P04637\t", 1)]})
    with pytest.raises(Exception, match="participant_id|null"):
        complexportal.ingest(cat, REL, url=bad)


def test_rerun_is_a_noop_and_a_later_release_retires_and_adds(cat, url, tmp_path):
    version, _ = complexportal.land_raw(cat, REL, url=url)
    complexportal.transform(cat, REL, version)
    counts = complexportal.transform(cat, "2026.09", version)
    assert {k: (c["written"], c["unchanged"]) for k, c in counts.items()} == {
        "annotation.complexportal__complex": (0, 4), "annotation.complexportal__participant": (0, 12)}

    # the next release withdraws the predicted complex, swaps one member of CPX-663
    # and resolves the other's stoichiometry
    later = release_dir(tmp_path, "2026-05-01", {
        "9606.tsv": [HUMAN[0].replace("O15151(0)|P04637(0)", "Q00987(1)|P04637(1)"), HUMAN[1]],
        "559292.tsv": YEAST})
    complexportal.ingest(cat, "2026.10", url=later)
    cx = sorted((r["complex_ac"], r["valid_from"], r["valid_to"])
                for r in rows(cat, "annotation.complexportal__complex"))
    assert cx == [("CPX-1", REL, None), ("CPX-25", REL, None), ("CPX-663", REL, None),
                  ("CPX-9000", REL, "2026.10")]
    history = sorted(((r["participant_id"], r["stoichiometry"], r["valid_from"], r["valid_to"])
                      for r in rows(cat, "annotation.complexportal__participant")
                      if r["complex_ac"] == "CPX-663"), key=lambda h: (h[0], h[2]))
    assert history == [("O15151", None, REL, "2026.10"), ("P04637", None, REL, "2026.10"),
                       ("P04637", 1, "2026.10", None), ("Q00987", 1, "2026.10", None)]
    # raw accumulates versions
    assert {r["complexportal_version"] for r in rows(cat, "raw.complexportal__complex")} == {
        "2026-01-09", "2026-05-01"}


def test_every_column_is_documented(cat, url):
    """SPEC.md section B1, for the three tables this source adds."""
    complexportal.ingest(cat, REL, url=url)
    for identifier in ("raw.complexportal__complex", "annotation.complexportal__complex",
                       "annotation.complexportal__participant"):
        table = cat.load_table(identifier)
        assert "CC0" in table.properties.get("comment", ""), identifier
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"
