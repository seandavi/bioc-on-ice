"""GWAS Catalog: land both files whole -> derive studies and associations.

The fixture is handcrafted at test time in the real files' dialect — headers
verbatim, no quoting, empty cells for missing, the associations inside a zip,
both under a dated release directory — so the test never touches the network.
"""

import zipfile

import pytest

from bioconice import catalog, gwas_catalog as gw

REL = "2026.08"
T2D = "http://purl.obolibrary.org/obo/MONDO_0005148"


def line(columns, **cells):
    return "\t".join(cells.get(c, "") for c in columns.values())


def assoc(**cells):
    return line(gw.ASSOCIATIONS, **cells)


TCF7L2 = dict(study_accession="GCST000001", pubmed_id="1", snps="rs7903146",
              strongest_snp_risk_allele="rs7903146-T", risk_allele_frequency="NR",
              chr_id="10", chr_pos="112998590", mapped_gene="TCF7L2", snp_gene_ids="ENSG00000148737",
              merged="0", snp_id_current="7903146", intergenic="0", context="intron_variant",
              mapped_trait="type 2 diabetes mellitus", mapped_trait_uri=T2D,
              study='A study of "daily maximum drinks"', cnv="N")
ASSOCS = [
    # below the smallest double: the text is kept, pvalue_mlog is the number
    assoc(**TCF7L2, p_value="3E-1315", pvalue_mlog="1314.52", or_beta="1.4"),
    # the same study reports the same SNP again, in a stratum
    assoc(**TCF7L2, p_value="2E-9", pvalue_mlog="8.7", p_value_text="(women)"),
    # the file repeats a row whole: raw keeps both, the derived table one
    assoc(**TCF7L2, p_value="2E-9", pvalue_mlog="8.7", p_value_text="(women)"),
    # SNP x SNP interaction: no single rsID, position or gene; trait left unmapped
    assoc(study_accession="GCST000001", pubmed_id="1", snps="rs3130453 x rs2249742",
          strongest_snp_risk_allele="rs3130453-? x rs2249742-?", chr_id="6 x 6",
          chr_pos="31157072 x 31272944", mapped_gene="CCHCR1 x HLA-C - USP8P1",
          p_value="4E-8", pvalue_mlog="7.4", merged="0"),
    # haplotype, intergenic, two traits from two ontologies in unsorted order
    assoc(study_accession="GCST000002", pubmed_id="2", snps="rs780094; rs2293571",
          strongest_snp_risk_allele="rs780094-?; rs2293571-?", chr_id="2;2",
          p_value="1E-8", pvalue_mlog="8.0", merged="0", intergenic="1",
          upstream_gene_id="ENSG00000084734", upstream_gene_distance="8190",
          mapped_trait="triglyceride measurement, body height",
          mapped_trait_uri="http://www.ebi.ac.uk/efo/EFO_0004530, "
                           "http://purl.obolibrary.org/obo/OBA_VT0001253"),
]
STUDIES = [
    line(gw.STUDIES, study_accession="GCST000001", pubmed_id="1", date="2024-02-20",
         study='A study of "daily maximum drinks"', association_count="4",
         replication_sample_size="NA", mapped_trait_uri=T2D,
         full_summary_statistics="yes", gxe="no", cohort="UKB|CHARGE"),
    line(gw.STUDIES, study_accession="GCST000002", pubmed_id="2", association_count="1",
         full_summary_statistics="no", gxe="no"),
    # no curated association at all, and a background trait
    line(gw.STUDIES, study_accession="GCST000003", pubmed_id="2", association_count="0",
         mapped_background_trait_uri="http://www.orpha.net/ORDO/Orphanet_1572, " + T2D,
         full_summary_statistics="no", gxe="yes"),
]


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path / "warehouse"))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def write(root, date, assocs=ASSOCS, studies=STUDIES, header=gw.ASSOCIATIONS):
    """A release directory laid out like the Catalog's: root/YYYY/MM/DD/ with both files."""
    d = root / date
    d.mkdir(parents=True)
    (zname, _), (sname, _) = gw.FILES.values()
    with zipfile.ZipFile(d / zname, "w") as z:
        z.writestr("associations.tsv", "\n".join(["\t".join(header), *assocs]) + "\n")
    (d / sname).write_text("\n".join(["\t".join(gw.STUDIES), *studies]) + "\n")
    return str(d)


@pytest.fixture
def url(tmp_path):
    return write(tmp_path / "releases", "2026/09/15")


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def test_raw_is_verbatim_and_whole(cat, url):
    version, counts = gw.land_raw(cat, REL, url=url)
    assert version == "2026-09-15"
    assert counts == {"raw.gwas_catalog__associations": 5, "raw.gwas_catalog__studies": 3}

    raw = rows(cat, "raw.gwas_catalog__associations")
    assert len(raw[0]) == len(gw.ASSOCIATIONS) + 2
    assert {(r["gwas_catalog_release"], r["landed_in"]) for r in raw} == {(version, REL)}
    top = next(r for r in raw if r["p_value"] == "3E-1315")
    # no quoting in this dialect: a '"' is data, and so are 'NR' and 'N'
    assert top["study"] == 'A study of "daily maximum drinks"'
    assert (top["risk_allele_frequency"], top["cnv"]) == ("NR", "N")
    # unparsed strings, an empty cell is NULL, and the multi-valued cell is unsplit
    assert top["or_beta"] == "1.4" and top["p_value_text"] is None
    assert [r["mapped_trait_uri"] for r in raw if r["study_accession"] == "GCST000002"] == [
        "http://www.ebi.ac.uk/efo/EFO_0004530, http://purl.obolibrary.org/obo/OBA_VT0001253"]
    assert {r["replication_sample_size"] for r in rows(cat, "raw.gwas_catalog__studies")} == {"NA", None}

    # re-landing the same release replaces it rather than appending
    gw.land_raw(cat, REL, url=url)
    assert len(rows(cat, "raw.gwas_catalog__associations")) == 5


def test_manifest_and_the_newest_dated_directory(cat, url, tmp_path):
    write(tmp_path / "releases", "2026/10/02")
    write(tmp_path / "releases", "2025/12/20")
    newest = gw.latest(str(tmp_path / "releases"))
    assert newest.endswith("/2026/10/02/")

    gw.land_raw(cat, REL, url=newest)
    m = {r["artifact"]: r for r in rows(cat, "provenance.release")
         if r["source"] == "gwas_catalog"}
    assert {a: (r["version_method"], r["source_version"], r["row_count"], r["url"])
            for a, r in m.items()} == {
        "associations": ("release_number", "2026-10-02", 5,
                         newest + "gwas-catalog-associations_ontology-annotated-full.zip"),
        "studies": ("release_number", "2026-10-02", 3,
                    newest + "gwas-catalog-download-studies-v1.0.3.1.txt")}


def test_a_changed_header_fails_before_landing(cat, tmp_path):
    header = [c for c in gw.ASSOCIATIONS if c != "CNV"]
    with pytest.raises(SystemExit, match="CNV"):
        gw.land_raw(cat, REL, url=write(tmp_path, "2026/01/01", header=header))


def test_derives_studies_and_associations(cat, url):
    counts = gw.ingest(cat, REL, url=url)
    assert counts["raw.gwas_catalog__associations"] == 5

    studies = {s["study_accession"]: s for s in rows(cat, "clinical.gwas_catalog__study")}
    assert set(studies) == {"GCST000001", "GCST000002", "GCST000003"}
    s1 = studies["GCST000001"]
    assert (s1["publication_date"], s1["association_count"], s1["full_summary_statistics"],
            s1["gxe"], s1["mapped_trait_ids"]) == ("2024-02-20", 4, True, False, ["MONDO:0005148"])
    # the IRIs become CURIEs in ontology.term's own form, Orphanet's included
    assert studies["GCST000003"]["mapped_background_trait_ids"] == ["MONDO:0005148", "Orphanet:1572"]
    assert studies["GCST000002"]["mapped_trait_ids"] is None

    assocs = rows(cat, "clinical.gwas_catalog__association")
    # the whole-row duplicate collapses; the stratified repeat of the SNP does not
    assert len(assocs) == 4 and len({a["association_key"] for a in assocs}) == 4
    tcf = sorted((a for a in assocs if a["snp_ids"] == ["rs7903146"]), key=lambda a: a["p_value"])
    assert [(a["p_value"], a["p_value_text"]) for a in tcf] == [("2E-9", "(women)"), ("3E-1315", None)]
    assert (tcf[1]["pvalue_mlog"], tcf[1]["or_beta"], tcf[1]["merged"], tcf[1]["intergenic"]) == (
        1314.52, 1.4, False, False)
    assert tcf[1]["snp_gene_ids"] == ["ENSG00000148737"] and tcf[1]["valid_from"] == REL

    by_snps = {a["snps"]: a for a in assocs}
    inter = by_snps["rs3130453 x rs2249742"]
    assert inter["snp_ids"] == ["rs2249742", "rs3130453"] and inter["chr_pos"] == "31157072 x 31272944"
    assert inter["mapped_trait_ids"] is None and inter["intergenic"] is None
    hap = by_snps["rs780094; rs2293571"]
    assert hap["snp_ids"] == ["rs2293571", "rs780094"]
    # sorted CURIEs; an id that is not PREFIX_digits stays an IRI, as ontology.term has it
    assert hap["mapped_trait_ids"] == ["EFO:0004530", "http://purl.obolibrary.org/obo/OBA_VT0001253"]
    assert (hap["upstream_gene_distance"], hap["snp_gene_ids"]) == (8190, None)


def test_rerun_is_a_noop_and_a_later_release_retires_adds_and_reversions(cat, url, tmp_path):
    version, _ = gw.land_raw(cat, REL, url=url)
    gw.transform(cat, REL, version)
    counts = gw.transform(cat, "2026.09", version)
    assert {k: (c["written"], c["unchanged"]) for k, c in counts.items()} == {
        "clinical.gwas_catalog__study": (0, 3), "clinical.gwas_catalog__association": (0, 4)}

    # a later release drops the interaction, adds an association, and remaps the
    # haplotype's trait — the Catalog's mapping changed, not what was curated
    later = [a for a in ASSOCS if " x " not in a]
    later[-1] = later[-1].replace("EFO_0004530", "EFO_0004531")
    later.append(assoc(study_accession="GCST000002", pubmed_id="2", snps="rs671",
                       strongest_snp_risk_allele="rs671-A", p_value="5E-20", pvalue_mlog="19.3"))
    gw.ingest(cat, "2026.10", url=write(tmp_path / "releases", "2026/10/02", assocs=later))

    history = sorted((a["snps"], a["mapped_trait_ids"] and a["mapped_trait_ids"][0],
                      a["valid_from"], a["valid_to"])
                     for a in rows(cat, "clinical.gwas_catalog__association")
                     if a["snps"] != "rs7903146")
    assert history == [
        ("rs3130453 x rs2249742", None, REL, "2026.10"),
        ("rs671", None, "2026.10", None),
        ("rs780094; rs2293571", "EFO:0004530", REL, "2026.10"),
        ("rs780094; rs2293571", "EFO:0004531", "2026.10", None),
    ]
    # the remapped association kept its key: two versions of one association
    hap = [a for a in rows(cat, "clinical.gwas_catalog__association") if a["snps"].startswith("rs780094")]
    assert len({a["association_key"] for a in hap}) == 1
    # both Catalog releases are still in raw
    assert {r["gwas_catalog_release"] for r in rows(cat, "raw.gwas_catalog__associations")} == {
        "2026-09-15", "2026-10-02"}


def test_every_column_is_documented(cat, url):
    """SPEC.md section B1, for the four tables this source adds."""
    gw.ingest(cat, REL, url=url)
    for identifier in (*gw.FILES, "clinical.gwas_catalog__study", "clinical.gwas_catalog__association"):
        table = cat.load_table(identifier)
        assert "EMBL-EBI Terms of Use" in table.properties.get("comment", ""), identifier
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"
