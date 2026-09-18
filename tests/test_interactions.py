"""BioGRID and IntAct: land whole -> derive -> stack in annotation.interaction.

Fixtures are handcrafted at test time in each real file's dialect and zipped with
stdlib zipfile, as upstream ships them, so the zip path is exercised and the test
never touches the network. Version comes from where upstream puts it: BioGRID's
file name, IntAct's dated directory.
"""

import zipfile

import pytest

from bioconice import biogrid, catalog, intact

REL = "2026.08"


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path / "wh"))
    monkeypatch.setenv("BIOCONICE_SCRATCH", str(tmp_path / "scratch"))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def live(cat, source):
    return rows(cat, "annotation.interaction", row_filter=f"valid_to IS NULL AND source = '{source}'")


def zipped(path, members):
    """A zip at `path` holding {member name: (header, data lines)}; returns its file:// url."""
    path.parent.mkdir(parents=True, exist_ok=True)
    with zipfile.ZipFile(path, "w", zipfile.ZIP_DEFLATED) as z:
        for name, (header, data) in members.items():
            z.writestr(name, "\n".join(["\t".join(header), *data]) + "\n")
    return path.as_uri()


# ---------------------------------------------------------------- BioGRID

def bg_line(**cells):
    return "\t".join(cells.get(c, "-") for c in biogrid.COLUMNS.values())


TP53_MDM2 = dict(entrez_gene_a="7157", entrez_gene_b="4193", biogrid_id_a="113010",
                 biogrid_id_b="110358", official_symbol_a="TP53", official_symbol_b="MDM2",
                 synonyms_a="BCC7|LFS1|P53|TRP53", experimental_system_type="physical",
                 organism_id_a="9606", organism_id_b="9606", source_database="BIOGRID")
BG_DATA = [
    bg_line(biogrid_interaction_id="253053", experimental_system="Affinity Capture-Western",
            publication_source="PUBMED:14612427", swissprot_a="P04637", **TP53_MDM2),
    # the same pair, system and paper again: the synthetic pair key would collide here
    bg_line(biogrid_interaction_id="253054", experimental_system="Affinity Capture-Western",
            publication_source="PUBMED:14612427", **TP53_MDM2),
    # a genetic interaction in yeast, from a preprint, whose A has no Entrez gene
    bg_line(biogrid_interaction_id="3000001", biogrid_id_a="4383900", entrez_gene_b="850504",
            biogrid_id_b="31107", experimental_system="Synthetic Lethality",
            experimental_system_type="genetic", publication_source="DOI:10.1101/2020.01.01.123456",
            organism_id_a="559292", organism_id_b="559292", source_database="BIOGRID"),
]


def bg_zip(tmp_path, version, data, header=None):
    return zipped(tmp_path / f"BIOGRID-ALL-{version}.tab3.zip",
                  {f"BIOGRID-ALL-{version}.tab3.txt": (header or list(biogrid.COLUMNS), data)})


def test_biogrid_raw_is_verbatim_and_whole(cat, tmp_path):
    url = bg_zip(tmp_path, "5.0.261", BG_DATA)
    assert biogrid.land_raw(cat, REL, url=url) == ("5.0.261", 3)

    raw = {r["biogrid_interaction_id"]: r for r in rows(cat, "raw.biogrid__interactions")}
    tp53 = raw["253053"]
    assert len(tp53) == len(biogrid.COLUMNS) + 2
    # unparsed strings, '|' lists unsplit, '-' is NULL, every organism lands
    assert (tp53["entrez_gene_a"], tp53["synonyms_a"], tp53["score"]) == ("7157", "BCC7|LFS1|P53|TRP53", None)
    assert raw["3000001"]["entrez_gene_a"] is None and raw["3000001"]["organism_id_a"] == "559292"
    assert {(r["biogrid_version"], r["landed_in"]) for r in raw.values()} == {("5.0.261", REL)}

    m = next(r for r in rows(cat, "provenance.release") if r["source"] == "biogrid")
    assert (m["version_method"], m["source_version"], m["row_count"]) == ("release_number", "5.0.261", 3)

    # re-landing replaces rather than appends
    biogrid.land_raw(cat, REL, url=url)
    assert len(rows(cat, "raw.biogrid__interactions")) == 3


def test_biogrid_refuses_a_changed_header_or_an_unversioned_name(cat, tmp_path):
    header = [*biogrid.COLUMNS][:5] + ["New Column"] + [*biogrid.COLUMNS][5:]
    with pytest.raises(SystemExit, match="New Column"):
        biogrid.land_raw(cat, REL, url=bg_zip(tmp_path, "5.0.999", [], header))
    with pytest.raises(SystemExit, match="version"):
        biogrid.land_raw(cat, REL, url=(tmp_path / "latest.zip").as_uri())


def test_biogrid_derives_interactions_in_its_own_id_space(cat, tmp_path):
    counts = biogrid.ingest(cat, REL, url=bg_zip(tmp_path, "5.0.261", BG_DATA))
    assert counts["annotation.interaction"]["written"] == 3

    got = {r["interaction_id"]: r for r in live(cat, "BIOGRID")}
    # the repeated pair+system+paper is two evidences, kept apart by BioGRID's id
    assert set(got) == {"253053", "253054", "3000001"}
    r = got["253053"]
    assert (r["interactor_a_namespace"], r["interactor_a_id"], r["interactor_b_namespace"],
            r["interactor_b_id"], r["taxon_id_a"], r["taxon_id_b"]) == ("ENTREZ", "7157", "ENTREZ", "4193", 9606, 9606)
    assert (r["detection_method"], r["detection_method_id"], r["interaction_type"], r["negative"],
            r["pubmed_id"], r["doi"]) == ("Affinity Capture-Western", None, "physical", False, "14612427", None)
    # no Entrez gene: BioGRID's own id, and the namespace says so; preprint: a DOI, no PMID
    g = got["3000001"]
    assert (g["interactor_a_namespace"], g["interactor_a_id"], g["interaction_type"],
            g["pubmed_id"], g["doi"]) == ("BIOGRID", "4383900", "genetic", None, "10.1101/2020.01.01.123456")


def test_biogrid_rerun_is_a_noop_and_a_later_release_retires_and_adds(cat, tmp_path):
    version, _ = biogrid.land_raw(cat, REL, url=bg_zip(tmp_path, "5.0.261", BG_DATA))
    biogrid.transform(cat, REL, version)
    c = biogrid.transform(cat, "2026.09", version)["annotation.interaction"]
    assert (c["written"], c["unchanged"]) == (0, 3)

    # 5.0.262 withdraws 253054 and adds one evidence
    added = bg_line(biogrid_interaction_id="3000002", experimental_system="Two-hybrid",
                    publication_source="PUBMED:1", **TP53_MDM2)
    biogrid.ingest(cat, "2026.10", url=bg_zip(tmp_path, "5.0.262", [BG_DATA[0], BG_DATA[2], added]))
    history = sorted((r["interaction_id"], r["valid_from"], r["valid_to"])
                     for r in rows(cat, "annotation.interaction"))
    assert history == [("253053", REL, None), ("253054", REL, "2026.10"),
                       ("3000001", REL, None), ("3000002", "2026.10", None)]
    # raw holds the latest release only
    assert {r["biogrid_version"] for r in rows(cat, "raw.biogrid__interactions")} == {"5.0.262"}


# ---------------------------------------------------------------- IntAct

def ia_line(**cells):
    return "\t".join(cells.get(c, "-") for c in intact.COLUMNS.values())


HUMAN = "taxid:9606(human)|taxid:9606(Homo sapiens)"
Y2H = dict(detection_methods='psi-mi:"MI:0018"(two hybrid)',
           interaction_types='psi-mi:"MI:0915"(physical association)',
           publication_ids='pubmed:10198631|imex:IM-1|doi:"doi:10.1016/S1097-2765(00)80456-6"')
PULLDOWN = dict(detection_methods='psi-mi:"MI:0096"(pull down)',
                interaction_types='psi-mi:"MI:0914"(association)',
                expansion_methods='psi-mi:"MI:1060"(spoke expansion)',
                interaction_ids="intact:EBI-200|imex:IM-2-1", publication_ids="pubmed:unassigned701|imex:IM-2",
                id_a="uniprotkb:P04637", taxid_a=HUMAN, negative="false")
IA_DATA = [
    ia_line(id_a="uniprotkb:P04637", id_b="uniprotkb:Q00987", taxid_a=HUMAN, taxid_b=HUMAN,
            interaction_ids="intact:EBI-100|imex:IM-1-1", confidence_values="intact-miscore:0.98",
            negative="false", **Y2H),
    # one n-ary pull-down, spoke-expanded: two rows under ONE interaction AC, the
    # second against a small molecule with no taxon
    ia_line(id_b="uniprotkb:Q00987-2", taxid_b=HUMAN, **PULLDOWN),
    ia_line(id_b='chebi:"CHEBI:15422"', **PULLDOWN),
    # ... and that pair again, differing only in a participant feature: one derived row
    ia_line(id_b='chebi:"CHEBI:15422"', features_a="binding site:1-10", **PULLDOWN),
    # autophosphorylation: no interactor B; synthesised in vitro (pseudo-taxon -1)
    ia_line(id_a="intact:EBI-999", taxid_a="taxid:-1(in vitro)|taxid:-1(In vitro)",
            detection_methods='psi-mi:"MI:0095"("proteinchip(r) on a surface")',
            interaction_types='psi-mi:"MI:0217"(phosphorylation reaction)',
            interaction_ids="intact:EBI-300", publication_ids="pubmed:2", negative="false"),
]
IA_NEGATIVE = [
    ia_line(id_a="uniprotkb:Q9NP97", id_b="uniprotkb:O54918-3", taxid_a=HUMAN,
            taxid_b="taxid:10090(mouse)|taxid:10090(Mus musculus)", interaction_ids="intact:EBI-526131",
            negative="true", **Y2H),
]


def ia_zip(tmp_path, version, data, negative=IA_NEGATIVE, header=None):
    header = header or list(intact.COLUMNS)
    return zipped(tmp_path / version / "psimitab" / "intact.zip",
                  {"intact.txt": (header, data), "intact_negative.txt": (header, negative)})


def test_intact_raw_is_verbatim_and_whole(cat, tmp_path):
    url = ia_zip(tmp_path, "2026-01-09", IA_DATA)
    # both members of the zip land: five positive rows and the negative one
    assert intact.land_raw(cat, REL, url=url) == ("2026-01-09", 6)

    raw = rows(cat, "raw.intact__mitab")
    assert len(raw[0]) == len(intact.COLUMNS) + 2
    tp53 = next(r for r in raw if r["interaction_ids"].startswith("intact:EBI-100"))
    # MITAB's '"' is content, not a CSV quote; cells stay unsplit; '-' is NULL
    assert tp53["detection_methods"] == 'psi-mi:"MI:0018"(two hybrid)'
    assert tp53["publication_ids"].endswith('doi:"doi:10.1016/S1097-2765(00)80456-6"')
    assert tp53["taxid_a"] == HUMAN and tp53["expansion_methods"] is None
    assert sorted(r["negative"] for r in raw) == ["false"] * 5 + ["true"]
    assert {(r["intact_version"], r["landed_in"]) for r in raw} == {("2026-01-09", REL)}

    m = next(r for r in rows(cat, "provenance.release") if r["source"] == "intact")
    assert (m["version_method"], m["source_version"], m["row_count"]) == ("release_number", "2026-01-09", 6)

    with pytest.raises(SystemExit, match="dated directory"):
        intact.land_raw(cat, REL, url="https://ftp.ebi.ac.uk/pub/databases/intact/current/psimitab/intact.zip")


def test_intact_derives_interactions_in_its_own_id_space(cat, tmp_path):
    intact.ingest(cat, REL, url=ia_zip(tmp_path, "2026-01-09", IA_DATA))
    got = {(r["interaction_id"], r["interactor_b_id"]): r for r in live(cat, "INTACT")}
    # six raw rows, five derived: the feature-only duplicate collapses
    assert set(got) == {("EBI-100", "Q00987"), ("EBI-200", "Q00987-2"), ("EBI-200", "CHEBI:15422"),
                        ("EBI-300", "EBI-999"), ("EBI-526131", "O54918-3")}

    r = got["EBI-100", "Q00987"]
    assert (r["interactor_a_namespace"], r["interactor_a_id"], r["interactor_b_namespace"],
            r["taxon_id_a"], r["taxon_id_b"]) == ("UNIPROT", "P04637", "UNIPROT", 9606, 9606)
    assert (r["detection_method_id"], r["detection_method"], r["interaction_type_id"],
            r["interaction_type"], r["expansion_method"], r["negative"]) == (
        "MI:0018", "two hybrid", "MI:0915", "physical association", None, False)
    assert (r["pubmed_id"], r["doi"]) == ("10198631", "10.1016/s1097-2765(00)80456-6")

    # the n-ary interaction: one AC, two rows, flagged; the small molecule keeps
    # its own namespace and has no taxon (not the protein's); no PMID assigned
    chebi = got["EBI-200", "CHEBI:15422"]
    assert (chebi["interactor_b_namespace"], chebi["taxon_id_a"], chebi["taxon_id_b"],
            chebi["expansion_method"], chebi["pubmed_id"]) == ("CHEBI", 9606, None, "spoke expansion", None)
    assert got["EBI-200", "Q00987-2"]["interactor_b_namespace"] == "UNIPROT"

    # a missing B repeats A; a pseudo-taxon is not an organism; a quoted label is unquoted
    auto = got["EBI-300", "EBI-999"]
    assert (auto["interactor_a_namespace"], auto["interactor_a_id"], auto["interactor_b_namespace"],
            auto["taxon_id_a"], auto["taxon_id_b"], auto["detection_method"]) == (
        "INTACT", "EBI-999", "INTACT", None, None, "proteinchip(r) on a surface")

    assert got["EBI-526131", "O54918-3"]["negative"] is True
    assert got["EBI-526131", "O54918-3"]["taxon_id_b"] == 10090


def test_intact_rerun_is_a_noop_and_a_later_release_retires_and_adds(cat, tmp_path):
    version, _ = intact.land_raw(cat, REL, url=ia_zip(tmp_path, "2026-01-09", IA_DATA))
    intact.transform(cat, REL, version)
    c = intact.transform(cat, "2026.09", version)["annotation.interaction"]
    assert (c["written"], c["unchanged"]) == (0, 5)

    # the next release re-maps one interactor (Q00987-2 -> Q00987-5): under this
    # key that is one row retired and one opened, with the AC unchanged
    later = [IA_DATA[0], IA_DATA[1].replace("Q00987-2", "Q00987-5"), *IA_DATA[2:]]
    intact.ingest(cat, "2026.10", url=ia_zip(tmp_path, "2026-05-01", later))
    history = sorted((r["interactor_b_id"], r["valid_from"], r["valid_to"])
                     for r in rows(cat, "annotation.interaction") if r["interaction_id"] == "EBI-200")
    assert history == [("CHEBI:15422", REL, None), ("Q00987-2", REL, "2026.10"),
                       ("Q00987-5", "2026.10", None)]


# ---------------------------------------------------------------- stacked

def test_biogrid_and_intact_do_not_retire_each_other(cat, tmp_path):
    """The flip-flop regression (ADR-0004): both write annotation.interaction, and
    a scope without the source would let each run retire the other's rows."""
    bg = bg_zip(tmp_path, "5.0.261", BG_DATA)
    ia = ia_zip(tmp_path, "2026-01-09", IA_DATA)
    biogrid.ingest(cat, REL, url=bg)
    intact.ingest(cat, REL, url=ia)
    assert (len(live(cat, "BIOGRID")), len(live(cat, "INTACT"))) == (3, 5)

    c = biogrid.ingest(cat, "2026.09", url=bg)["annotation.interaction"]
    assert len(live(cat, "INTACT")) == 5 and c["written"] == 0
    c = intact.ingest(cat, "2026.10", url=ia)["annotation.interaction"]
    assert len(live(cat, "BIOGRID")) == 3 and c["written"] == 0

    # TP53-MDM2 is there from both, each in its own identifier space, unmapped
    pairs = {(r["source"], r["interactor_a_id"], r["interactor_b_id"])
             for r in rows(cat, "annotation.interaction", row_filter="valid_to IS NULL")}
    assert {("BIOGRID", "7157", "4193"), ("INTACT", "P04637", "Q00987")} <= pairs


def test_every_column_is_documented_and_the_licences_travel(cat, tmp_path):
    """SPEC.md section B1; and MIT's notice must ride with every copy of BioGRID rows."""
    biogrid.ingest(cat, REL, url=bg_zip(tmp_path, "5.0.261", BG_DATA))
    intact.ingest(cat, REL, url=ia_zip(tmp_path, "2026-01-09", IA_DATA))
    for identifier in ("raw.biogrid__interactions", "raw.intact__mitab", "annotation.interaction"):
        table = cat.load_table(identifier)
        comment = table.properties.get("comment")
        assert comment, identifier
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"
        if "intact" not in identifier:
            assert "Copyright © 2005 Mike Tyers Lab" in comment and "Permission is hereby granted" in comment
        if "biogrid" not in identifier:
            assert "IntAct (EMBL-EBI), CC BY 4.0; del Toro et al. NAR 2022" in comment
