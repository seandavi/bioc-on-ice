"""WikiPathways: land every species GMT whole -> derive pathways and membership.

The fixture is handcrafted at test time in the real files' dialect — ragged
tab-separated lines, no header, no quoting, the four-field packed set name —
so the test never touches the network.
"""

import pytest

from bioconice import catalog, wikipathways

REL = "2026.08"


def gmt(date, wp, title, species, *genes):
    return "\t".join([f"{title}%WikiPathways_{date}%{wp}%{species}",
                      f"https://www.wikipathways.org/instance/{wp}", *genes])


def release(root, date, files):
    """A directory shaped like current/gmt/: one file per species, dated by name."""
    d = root / date
    d.mkdir()
    for species, lines in files.items():
        (d / f"wikipathways-{date}-gmt-{species.replace(' ', '_')}.gmt").write_text(
            "".join(line + "\n" for line in lines))
    return str(d)


def september(date="20260910", extra=()):
    return {
        "Homo sapiens": [
            # 2678 is drawn twice in the real WP100, and listed twice
            gmt(date, "WP100", "Glutathione metabolism", "Homo sapiens", "2687", "2678", "2678"),
            gmt(date, "WP106", "Alanine and aspartate metabolism", "Homo sapiens", "2806", "435"),
            *extra,
        ],
        # a one-line file: the smallest species still lands
        "Zea mays": [gmt(date, "WP5421", "Anthocyanins in purple corn", "Zea mays",
                         "542166", "100276821")],
    }


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path / "wh"))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


@pytest.fixture
def gmts(tmp_path):
    return release(tmp_path, "20260910", september())


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def test_raw_is_verbatim_and_whole(cat, gmts):
    version, n = wikipathways.land_raw(cat, REL, url=gmts)
    assert (version, n) == ("20260910", 3)

    raw = {(r["species"], r["line_number"]): r for r in rows(cat, "raw.wikipathways__gmt")}
    assert set(raw) == {("Homo_sapiens", 1), ("Homo_sapiens", 2), ("Zea_mays", 1)}
    wp100 = raw["Homo_sapiens", 1]
    # the packed name is unparsed; the genes are the rest of the line, repeats and all
    assert wp100["name"] == "Glutathione metabolism%WikiPathways_20260910%WP100%Homo sapiens"
    assert wp100["description"] == "https://www.wikipathways.org/instance/WP100"
    assert wp100["genes"] == "2687\t2678\t2678"
    assert wp100["file_name"] == "wikipathways-20260910-gmt-Homo_sapiens.gmt"
    assert {(r["wikipathways_version"], r["landed_in"]) for r in raw.values()} == {(version, REL)}

    # re-landing the same version replaces it rather than appending
    wikipathways.land_raw(cat, REL, url=gmts)
    assert len(rows(cat, "raw.wikipathways__gmt")) == 3


def test_manifest_records_the_release_date(cat, gmts):
    wikipathways.land_raw(cat, REL, url=gmts)
    m = next(r for r in rows(cat, "provenance.release") if r["source"] == "wikipathways")
    assert (m["version_method"], m["source_version"], m["row_count"], m["url"]) == (
        "release_number", "20260910", 3, gmts)


def test_a_directory_mixing_releases_fails_before_landing(cat, tmp_path):
    d = release(tmp_path, "20260910", september())
    (tmp_path / "20260910" / "wikipathways-20260810-gmt-Sus_scrofa.gmt").write_text("")
    with pytest.raises(SystemExit, match="exactly one release"):
        wikipathways.land_raw(cat, REL, url=d)


def test_derives_pathways_and_membership(cat, gmts):
    counts = wikipathways.ingest(cat, REL, url=gmts)
    assert counts["raw.wikipathways__gmt"] == 3

    pathways = {p["pathway_id"]: p for p in rows(cat, "annotation.wikipathways__pathway")}
    assert set(pathways) == {"WP100", "WP106", "WP5421"}
    corn = pathways["WP5421"]
    assert (corn["taxon_id"], corn["name"], corn["species"], corn["url"]) == (
        4577, "Anthocyanins in purple corn", "Zea mays",
        "https://www.wikipathways.org/instance/WP5421")
    assert corn["valid_from"] == REL and corn["valid_to"] is None

    members = {(m["pathway_id"], m["taxon_id"], m["gene_id"])
               for m in rows(cat, "annotation.wikipathways__gene_pathway")}
    assert members == {
        ("WP100", 9606, "2687"), ("WP100", 9606, "2678"),  # the repeat is one membership
        ("WP106", 9606, "2806"), ("WP106", 9606, "435"),
        ("WP5421", 4577, "542166"), ("WP5421", 4577, "100276821"),
    }
    assert len(rows(cat, "annotation.wikipathways__gene_pathway")) == 6


@pytest.mark.parametrize("line, match", [
    (gmt("20260910", "WP1", "T", "Felis catus", "1"), "no taxon id declared for species Felis catus"),
    (gmt("20260810", "WP1", "T", "Homo sapiens", "1"), "mismatched set name"),   # stale date
    ("just a name\thttps://example.org\t1", "unparseable or mismatched set name"),
    (gmt("20260910", "WP1", "T", "Homo sapiens", "ENSG00000141510"), "not an Entrez GeneID"),
])
def test_what_transform_cannot_interpret_fails_loudly(cat, tmp_path, line, match):
    species = "Felis catus" if "Felis" in line else "Homo sapiens"
    version, _ = wikipathways.land_raw(cat, REL, url=release(tmp_path, "20260910", {species: [line]}))
    with pytest.raises(Exception, match=match):
        wikipathways.transform(cat, REL, version)


def test_rerun_is_a_noop_and_a_later_release_retires_and_adds(cat, gmts, tmp_path):
    version, _ = wikipathways.land_raw(cat, REL, url=gmts)
    wikipathways.transform(cat, REL, version)
    counts = wikipathways.transform(cat, "2026.09", version)
    assert {k: (c["written"], c["unchanged"]) for k, c in counts.items()} == {
        "annotation.wikipathways__pathway": (0, 3),
        "annotation.wikipathways__gene_pathway": (0, 6)}

    # October: WP106 is withdrawn, WP9999 is new, WP100 loses a gene and gains one
    october = september("20261010", extra=[
        gmt("20261010", "WP9999", "A new pathway", "Homo sapiens", "7157")])
    october["Homo sapiens"][0] = gmt("20261010", "WP100", "Glutathione metabolism",
                                     "Homo sapiens", "2687", "2876")
    del october["Homo sapiens"][1]
    wikipathways.ingest(cat, "2026.10", url=release(tmp_path, "20261010", october))

    history = sorted((p["pathway_id"], p["valid_from"], p["valid_to"])
                     for p in rows(cat, "annotation.wikipathways__pathway"))
    assert history == [("WP100", REL, None), ("WP106", REL, "2026.10"),
                       ("WP5421", REL, None), ("WP9999", "2026.10", None)]
    wp100 = sorted((m["gene_id"], m["valid_from"], m["valid_to"])
                   for m in rows(cat, "annotation.wikipathways__gene_pathway")
                   if m["pathway_id"] == "WP100")
    assert wp100 == [("2678", REL, "2026.10"), ("2687", REL, None), ("2876", "2026.10", None)]
    # raw accumulates versions
    assert {r["wikipathways_version"] for r in rows(cat, "raw.wikipathways__gmt")} == {
        "20260910", "20261010"}


def test_every_column_is_documented(cat, gmts):
    """SPEC.md section B1, for the three tables this source adds."""
    wikipathways.ingest(cat, REL, url=gmts)
    for identifier in ("raw.wikipathways__gmt", "annotation.wikipathways__pathway",
                       "annotation.wikipathways__gene_pathway"):
        table = cat.load_table(identifier)
        assert table.properties.get("comment"), identifier
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"
