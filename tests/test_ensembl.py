"""Raw ingest -> transform -> query, on a fixture. Run with `uv run pytest`."""

from pathlib import Path

import pytest

from bioconice import catalog, ensembl, schemas

GTF = Path(__file__).parent / "tiny.gtf"
HUMAN = {"taxon_id": 9606, "assembly": "GRCh38.p14", "accession": "GCA_000001405.29"}
MOUSE = {"taxon_id": 10090, "assembly": "GRCm39", "accession": "GCA_000001635.9"}
REL, ENS = "2026.08", "116"


def load(cat, info):
    ensembl.land_raw(cat, REL, "x", ENS, url=str(GTF), info=info)
    return ensembl.transform(cat, REL, info, ENS)


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def test_raw_is_verbatim(cat):
    load(cat, HUMAN)
    raw = rows(cat, "raw.ensembl_gtf")
    assert len(raw) == 11  # every data line, including CDS and five_prime_utr
    assert {r["feature"] for r in raw} == {"gene", "transcript", "exon", "CDS", "five_prime_utr"}
    # the attribute blob is kept whole, so attributes we do not parse survive
    assert any("ENSP00000269305" in r["attribute"] for r in raw)
    assert {r["ensembl_release"] for r in raw} == {ENS}


def test_derived_tables(cat):
    load(cat, HUMAN)

    genes = rows(cat, "annotation.gene")
    assert len(genes) == 2
    tp53 = next(g for g in genes if g["symbol"] == "TP53")
    assert (tp53["gene_id"], tp53["version"]) == ("ENSG00000141510", "18")
    assert next(g for g in genes if g["gene_id"] == "ENSG00000288825")["symbol"] is None

    tx = rows(cat, "annotation.transcript")
    assert len(tx) == 3
    assert sum(t["canonical"] for t in tx) == 2

    exons = sorted(rows(cat, "annotation.exon", row_filter="transcript_id = 'ENST00000269305'"),
                   key=lambda e: e["rank"])
    assert [e["rank"] for e in exons] == [1, 2]
    # rank 1 is the higher coordinate on the minus strand: ordering by position
    # would reverse the transcript
    assert exons[0]["start"] > exons[1]["start"]
    # exon 1 is 5' UTR, so no coding bounds; exon 2 is coding with phase 0
    assert (exons[0]["cds_start"], exons[0]["cds_phase"]) == (None, None)
    assert (exons[1]["cds_start"], exons[1]["cds_end"], exons[1]["cds_phase"]) == (7676521, 7676622, 0)

    assert [i["target_id"] for i in rows(cat, "annotation.identifier_mapping")] == ["TP53"]


def test_transform_reruns_from_raw_without_refetch(cat):
    load(cat, HUMAN)
    # no url, no network: everything transform needs is already landed
    ensembl.transform(cat, REL, HUMAN, ENS)
    assert len(rows(cat, "annotation.gene")) == 2
    assert len(rows(cat, "annotation.exon")) == 4


def test_species_are_independent(cat):
    load(cat, HUMAN)
    load(cat, MOUSE)
    genes = rows(cat, "annotation.gene")
    assert len(genes) == 4
    assert {g["taxon_id"] for g in genes} == {9606, 10090}
    assert len(rows(cat, "raw.ensembl_gtf")) == 22


def test_every_column_is_documented(cat):
    """SPEC.md section B1: a table whose columns lack doc does not ship."""
    load(cat, HUMAN)
    load_ncbi(cat, REL)   # every declared table, or the new ones go unchecked
    from pyiceberg.exceptions import NoSuchTableError
    for identifier in schemas.TABLES:
        try:
            table = cat.load_table(identifier)
        except NoSuchTableError:
            continue  # not every source is loaded in every test
        assert table.properties.get("comment"), identifier
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"


NEXT = Path(__file__).parent / "tiny_next.gtf"


def load_from(cat, info, gtf, release, ensembl_release):
    ensembl.land_raw(cat, release, "x", ensembl_release, url=str(gtf), info=info)
    return ensembl.transform(cat, release, info, ensembl_release)


def test_merge_versions_changes_rather_than_overwriting(cat):
    load(cat, HUMAN)                                    # release 2026.08, Ensembl 116
    genes = rows(cat, "annotation.gene")
    assert {g["valid_from"] for g in genes} == {REL}
    assert all(g["valid_to"] is None for g in genes)

    # same data, later release: nothing written at all
    counts = load_from(cat, HUMAN, GTF, "2026.09", ENS)
    assert counts["annotation.gene"]["written"] == 0
    assert counts["annotation.gene"]["unchanged"] == 2

    # next upstream release: TP53 version bumped 18 -> 19, the lncRNA gone
    counts = load_from(cat, HUMAN, NEXT, "2026.10", "117")
    # one changed record costs two rows: the closed old version and the new one
    assert counts["annotation.gene"]["changed"] == 1
    assert counts["annotation.gene"]["superseded"] == 1
    assert counts["annotation.gene"]["retired"] == 1

    tp53 = sorted((g for g in rows(cat, "annotation.gene")
                   if g["gene_id"] == "ENSG00000141510"), key=lambda g: g["valid_from"])
    assert len(tp53) == 2
    # the old attribute value survives — this is what Type 1 destroyed
    assert (tp53[0]["version"], tp53[0]["valid_from"], tp53[0]["valid_to"]) == ("18", REL, "2026.10")
    assert (tp53[1]["version"], tp53[1]["valid_from"], tp53[1]["valid_to"]) == ("19", "2026.10", None)

    current = rows(cat, "annotation.gene", row_filter="valid_to IS NULL")
    assert [g["gene_id"] for g in current] == ["ENSG00000141510"]


def test_point_in_time_returns_the_attribute_of_that_release(cat):
    """The defect that motivated ADR-0006: PIT must reconstruct values, not just rows."""
    load(cat, HUMAN)
    load_from(cat, HUMAN, NEXT, "2026.10", "117")
    tp53 = [g for g in rows(cat, "annotation.gene") if g["gene_id"] == "ENSG00000141510"]
    assert [g["version"] for g in pit(tp53, REL)] == ["18"]
    assert [g["version"] for g in pit(tp53, "2026.10")] == ["19"]


def test_manifest_records_what_the_release_was_built_from(cat):
    load(cat, HUMAN)
    m = rows(cat, "provenance.release")
    assert len(m) == 1
    assert (m[0]["release"], m[0]["source"], m[0]["source_version"]) == (REL, "ensembl", ENS)
    assert m[0]["version_method"] == "release_number"
    assert m[0]["row_count"] == 11 and m[0]["retrieved_at"].startswith("20")


def pit(rows, release):
    """SPEC's point-in-time predicate."""
    return [r for r in rows
            if r["valid_from"] <= release
            and (r["valid_to"] is None or r["valid_to"] > release)]


def test_resurrection_is_a_new_version(cat):
    load_from(cat, HUMAN, GTF, "2026.08", "116")   # lncRNA present
    load_from(cat, HUMAN, NEXT, "2026.09", "117")  # lncRNA gone
    load_from(cat, HUMAN, GTF, "2026.10", "118")   # lncRNA back

    lnc = [r for r in rows(cat, "annotation.gene") if r["gene_id"] == "ENSG00000288825"]
    # two records, not one revived record
    assert len(lnc) == 2
    assert sorted((r["valid_from"], r["valid_to"]) for r in lnc) == [
        ("2026.08", "2026.09"), ("2026.10", None)]

    # the invariant that actually matters: one live row per business key
    assert len([r for r in lnc if r["valid_to"] is None]) == 1

    # disjoint intervals, so point-in-time still resolves to one row per release
    assert len(pit(lnc, "2026.08")) == 1
    assert len(pit(lnc, "2026.09")) == 0   # genuinely absent that release
    assert len(pit(lnc, "2026.10")) == 1


def test_duplicate_incoming_keys_are_rejected(cat):
    """The invariant is ours to enforce — Iceberg declares it and checks nothing."""
    import pyarrow as pa
    from pyiceberg.expressions import EqualTo
    from bioconice import merge

    load(cat, HUMAN)
    gene = cat.load_table("annotation.gene")
    cols = [f.name for f in gene.schema().fields if f.name not in ("valid_from", "valid_to")]
    one = gene.scan(row_filter="gene_id = 'ENSG00000141510'").to_arrow().select(cols)
    doubled = pa.concat_tables([one, one])          # same business key twice

    with pytest.raises(ValueError, match="more than one live row"):
        merge.merge(cat, "annotation.gene", doubled, "2026.11", EqualTo("taxon_id", 9606))


NCBI_URLS = {name: str(Path(__file__).parent / f"tiny_{name}.tsv")
             for name in ("gene2ensembl", "gene_info", "gene_history")}


def load_ncbi(cat, release, taxa=(9606,)):
    from bioconice import ncbi
    counts = ncbi.land_raw(cat, release, urls=NCBI_URLS)
    for taxon in taxa:
        ncbi.transform(cat, release, taxon)
    return counts


def maps(cat, **kw):
    """Live NCBI cross-references as {(source_namespace, target_namespace): [target_id]}."""
    out = {}
    for r in rows(cat, "annotation.identifier_mapping",
                  row_filter="valid_to IS NULL AND source = 'NCBI'", **kw):
        out.setdefault((r["source_namespace"], r["target_namespace"]), []).append(r["target_id"])
    return {k: sorted(v) for k, v in out.items()}


def test_two_sources_share_a_table_without_retiring_each_other(cat):
    """The flip-flop case: a taxon-only scope would make these alternate forever."""
    load(cat, HUMAN)              # Ensembl writes ENSEMBL->SYMBOL
    load_ncbi(cat, "2026.09")     # NCBI writes ENSEMBL->ENTREZ and the gene_info xrefs

    live = rows(cat, "annotation.identifier_mapping", row_filter="valid_to IS NULL")
    by_source = {}
    for r in live:
        by_source.setdefault(r["source"], []).append(r)
    assert sorted(by_source) == ["Ensembl", "NCBI"]
    assert [r["target_id"] for r in by_source["Ensembl"]] == ["TP53"]
    assert maps(cat)[("ENSEMBL", "ENTREZ")] == ["100302278", "7157"]

    # re-running Ensembl must not retire NCBI's rows either — the other direction
    n_ncbi = len(by_source["NCBI"])
    ensembl.transform(cat, "2026.10", HUMAN, ENS)
    still = rows(cat, "annotation.identifier_mapping",
                 row_filter="valid_to IS NULL AND source = 'NCBI'")
    assert len(still) == n_ncbi

    # and the manifest records two axes: a release number and a retrieval date
    man = {m["source"]: m for m in rows(cat, "provenance.release")}
    assert man["ensembl"]["version_method"] == "release_number"
    assert man["ncbi_gene"]["version_method"] == "retrieval_date"


def test_gene_info_and_gene2ensembl_merge_together(cat):
    """Both feed identifier_mapping; two merges into one scope would flip-flop."""
    load_ncbi(cat, "2026.09")
    m = maps(cat)
    # gene2ensembl's contribution survives alongside gene_info's
    assert m[("ENSEMBL", "ENTREZ")] == ["100302278", "7157"]
    assert m[("ENTREZ", "SYMBOL")] == ["MIR1244-1", "REG-17-1", "TP53"]
    assert m[("ENTREZ", "ALIAS")] == ["BCC7", "BMFS5", "LFS1", "MIRN1244", "P53", "TRP53",
                                      "mir-1244-1"]
    # dbXrefs: split at the FIRST colon, so HGNC's prefixed id survives whole
    assert m[("ENTREZ", "HGNC")] == ["HGNC:11998", "HGNC:35297"]
    # NCBI abbreviates the authority MIM; it is stored under the authority's name
    assert m[("ENTREZ", "OMIM")] == ["191170"]
    assert ("ENTREZ", "MIM") not in m
    # re-running is a no-op, not a churn of retire-and-reassert
    counts = load_ncbi(cat, "2026.10")
    from bioconice import ncbi
    assert ncbi.transform(cat, "2026.10", 9606)["annotation.identifier_mapping"]["written"] == 0
    assert counts["raw.ncbi_gene_info"] == 5


def test_raw_is_landed_whole_and_only_transform_is_scoped(cat):
    """Raw is not a function of what we derive: mouse lands even deriving only human."""
    load_ncbi(cat, "2026.09", taxa=(9606,))

    assert {r["taxon_id"] for r in rows(cat, "raw.ncbi_gene_info")} == {9606, 10090}
    assert {r["taxon_id"] for r in rows(cat, "raw.ncbi_gene_history")} == {9606, 10090}
    # ...but only the taxon we transformed is derived
    assert {g["taxon_id"] for g in rows(cat, "annotation.ncbi_gene")} == {9606}

    # deriving mouse later needs no re-fetch, and does not disturb human
    from bioconice import ncbi
    ncbi.transform(cat, "2026.10", 10090)
    live = rows(cat, "annotation.ncbi_gene", row_filter="valid_to IS NULL")
    assert {g["taxon_id"] for g in live} == {9606, 10090}
    assert {g["valid_from"] for g in live if g["taxon_id"] == 9606} == {"2026.09"}


def test_ncbi_gene_carries_the_attributes_ensembl_cannot(cat):
    load_ncbi(cat, "2026.09")
    genes = {g["gene_id"]: g for g in rows(cat, "annotation.ncbi_gene")}

    tp53 = genes["7157"]
    assert tp53["description"] == "tumor protein p53"      # OrgDb GENENAME
    assert tp53["map_location"] == "17p13.1"               # OrgDb MAP
    assert tp53["gene_type"] == "protein-coding"           # NCBI's vocabulary, not Ensembl's
    assert tp53["symbol"] == "TP53"

    # NEWENTRY is NCBI's placeholder record, not a gene
    assert "100000000" not in genes
    assert "NEWENTRY" not in maps(cat)[("ENTREZ", "SYMBOL")]
    # biological-region records are kept — gene_type is what distinguishes them
    assert genes["110006319"]["gene_type"] == "biological-region"


def test_gene_history_is_landed_but_not_interpreted(cat):
    """Supersession modelling is still open (#15 item 4); the tombstones are here anyway."""
    load_ncbi(cat, "2026.09")
    hist = {h["discontinued_gene_id"]: h for h in rows(cat, "raw.ncbi_gene_history")}
    assert len(hist) == 3
    # a real GeneID means "merged into"; NCBI's '-' means retired with no successor
    assert hist["11337"]["gene_id"] == "7157"
    assert hist["5555"]["gene_id"] is None
    # nothing derives from it, so no annotation table mentions a discontinued id
    assert "11337" not in {g["gene_id"] for g in rows(cat, "annotation.ncbi_gene")}


BSDB = Path(__file__).parent / "tiny_bugsigdb.csv"


def test_bugsigdb_lands_verbatim(cat):
    from bioconice import bugsigdb
    n = bugsigdb.land_raw(cat, REL, version="v1.3.1", url=str(BSDB))
    assert n == 2
    rows = {r["bsdb_id"]: r for r in rows_of(cat)}
    a = rows["bsdb:83/1/1"]

    # the banner line is parsed for in-band provenance, not landed as data
    assert a["export_timestamp"] == "2026-04-24_00:41_UTC"
    assert a["bugsigdb_version"] == "v1.3.1" and a["landed_in"] == REL

    # 'NA' is BugSigDB's missing marker and reads as NULL, like NCBI's '-'
    assert a["keywords"] is None
    assert rows["bsdb:83/1/2"]["shannon"] is None

    # member lists keep BOTH separators — splitting on '|' alone would turn one
    # taxon into its whole lineage
    assert ";" in a["ncbi_taxonomy_ids"] and "|" in a["ncbi_taxonomy_ids"]
    first_member = a["ncbi_taxonomy_ids"].split(";")[0]
    assert first_member.split("|")[-1] == "40214"      # the curated taxon
    assert first_member.split("|")[0] == "3379134"     # top of its lineage
    assert a["metaphlan_taxon_names"].startswith("k__")

    # upstream 'Source' is renamed, since `source` means asserter elsewhere here
    assert a["source_in_paper"] and "source" not in a


def rows_of(cat):
    return cat.load_table("raw.bugsigdb_full_dump").scan().to_arrow().to_pylist()


def test_bugsigdb_relands_a_tag_idempotently(cat):
    """A tag is immutable, so re-landing it must replace rather than duplicate."""
    from bioconice import bugsigdb
    bugsigdb.land_raw(cat, REL, version="v1.3.1", url=str(BSDB))
    bugsigdb.land_raw(cat, "2026.09", version="v1.3.1", url=str(BSDB))
    assert len(rows_of(cat)) == 2

    # a different tag accumulates alongside it rather than replacing it
    bugsigdb.land_raw(cat, "2026.09", version="v1.3.0", url=str(BSDB))
    assert len(rows_of(cat)) == 4
    assert {r["bugsigdb_version"] for r in rows_of(cat)} == {"v1.3.0", "v1.3.1"}


def test_bugsigdb_manifest_uses_the_release_tag_not_a_date(cat):
    """BugSigDB publishes citable tags, so recording a retrieval date would lose information."""
    from bioconice import bugsigdb
    bugsigdb.land_raw(cat, REL, version="v1.3.1", url=str(BSDB))
    m = next(r for r in rows(cat, "provenance.release") if r["source"] == "bugsigdb")
    assert (m["source_version"], m["version_method"]) == ("v1.3.1", "release_number")
    assert m["row_count"] == 2


def test_landing_a_url_with_no_rows_fails_loudly(cat, tmp_path):
    """Otherwise a bad URL leaves the previous landing in place and reports success."""
    from bioconice import ncbi
    empty = tmp_path / "empty.tsv"
    empty.write_text("#tax_id\tGeneID\tDiscontinued_GeneID\tDiscontinued_Symbol\tDiscontinue_Date\n")
    with pytest.raises(SystemExit, match="yielded no rows"):
        ncbi._land(cat, "2026.09", "gene_history", url=str(empty))
