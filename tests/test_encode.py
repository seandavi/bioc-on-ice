"""ENCODE: land both report.tsv inventories whole -> derive experiment and file
resource rows and their relationships. Run with `uv run pytest`.

Offline throughout: `experiments=` / `files=` hand land_raw an already-written
report, which skips the download and the portal `total` request. The fixtures
are written inline in report.tsv's real dialect (verified live 2026-09-18): a
'<timestamp>\\t<request URL>' comment line, a display-title header, CRLF line
ends, no quoting, lists ','-joined.
"""

import pytest

from bioconice import catalog, encode

REL = "2026.09"

EXPERIMENTS = [
    {"accession": "ENCSR000AAA", "status": "released", "assay_term_name": "ChIP-seq",
     # literal quotes and a comma: the portal writes them bare, and they must survive
     "description": '"K562, rep1 and rep2"',
     "biosample_ontology.term_id": "EFO:0002067", "biosample_ontology.term_name": "K562",
     "replicates.library.biosample.organism.taxon_id": "9606",
     "replicates.library.biosample.organism.scientific_name": "Homo sapiens",
     "target.label": "H3K27ac", "target.genes.geneid": "653604,126961,333932",
     "assembly": "hg19,GRCh38", "dbxrefs": "GEO:GSE1"},
    {"accession": "ENCSR000BBB", "status": "archived", "assay_term_name": "DNase-seq",
     "biosample_ontology.term_id": "UBERON:0002107", "biosample_ontology.term_name": "liver",
     "replicates.library.biosample.organism.taxon_id": "10090"},
    {"accession": "ENCSR000CCC", "status": "released", "assay_term_name": "RNA-seq",
     "biosample_ontology.term_id": "NTR:0000001",
     # a mixed-species experiment: two taxa, so taxon_id is NULL rather than one of them
     "replicates.library.biosample.organism.taxon_id": "9606,10090"},
]
FILES = [
    {"accession": "ENCFF000AAA", "title": "ENCFF000AAA", "status": "released",
     "dataset": "/experiments/ENCSR000AAA/", "file_format": "bed", "file_type": "bed narrowPeak",
     "assembly": "GRCh38", "file_size": "2390755432666", "md5sum": "abc",
     "href": "/files/ENCFF000AAA/@@download/ENCFF000AAA.bed.gz",
     "cloud_metadata.url": "https://encode-public.s3.amazonaws.com/2020/ENCFF000AAA.bed.gz",
     "s3_uri": "s3://encode-public/2020/ENCFF000AAA.bed.gz", "no_file_available": "False",
     "derived_from": "/files/ENCFF000BBB/,/files/GRCh38_EBV.chrom.sizes/", "preferred_default": "True"},
    {"accession": "ENCFF000BBB", "title": "ENCFF000BBB", "status": "released",
     "dataset": "/experiments/ENCSR000AAA/", "file_format": "bam", "file_size": "10",
     "href": "/files/ENCFF000BBB/@@download/ENCFF000BBB.bam", "no_file_available": "False"},
    # a file of an Annotation, a dataset type this source does not land
    {"accession": "ENCFF000CCC", "title": "ENCFF000CCC", "status": "released",
     "dataset": "/annotations/ENCSR999ZZZ/", "file_format": "bigBed", "no_file_available": "False",
     "href": "/files/ENCFF000CCC/@@download/ENCFF000CCC.bigBed"},
    # no ENCODE accession at all: keyed by title, restricted, no public object
    {"external_accession": "SRR1270455", "title": "SRR1270455", "status": "released",
     "dataset": "/experiments/ENCSR000BBB/", "file_format": "sra", "no_file_available": "True",
     "restricted": "True", "href": "/files/SRR1270455/@@download/SRR1270455.sra"},
]


def report(path, type_, fields, records, date="2026-09-18"):
    url = encode.report_url(type_, fields).replace("report.tsv", "report/") + "&limit=all"
    lines = [f"{date} 21:20:23.241549\t{url}", "\t".join(f"Title of {f}" for f in fields)]
    lines += ["\t".join(r.get(f, "") for f in fields) for r in records]
    path.write_bytes(("\r\n".join(lines) + "\r\n").encode())
    return str(path)


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path / "wh"))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def ingest(cat, tmp_path, release=REL, experiments=EXPERIMENTS, files=FILES, date="2026-09-18"):
    return encode.ingest(
        cat, release,
        report(tmp_path / "experiment.tsv", "Experiment", encode.EXPERIMENT_FIELDS, experiments, date),
        report(tmp_path / "file.tsv", "File", encode.FILE_FIELDS, files, date))


def test_raw_is_landed_whole_and_verbatim(cat, tmp_path):
    counts = ingest(cat, tmp_path)
    assert counts["raw.encode__experiment"] == 3 and counts["raw.encode__file"] == 4
    exp = {r["accession"]: r for r in rows(cat, "raw.encode__experiment")}
    # every status landed; quotes and commas are data, lists stay joined
    assert exp["ENCSR000BBB"]["status"] == "archived"
    assert exp["ENCSR000AAA"]["description"] == '"K562, rep1 and rep2"'
    assert exp["ENCSR000AAA"]["target_genes_geneid"] == "653604,126961,333932"
    assert exp["ENCSR000AAA"]["retrieval_date"] == "2026-09-18"   # from the report's comment line
    assert exp["ENCSR000BBB"]["target_label"] is None
    raw_files = {r["title"]: r for r in rows(cat, "raw.encode__file")}
    assert raw_files["SRR1270455"]["accession"] is None
    assert raw_files["ENCFF000AAA"]["file_size"] == "2390755432666"


def test_manifest_rows(cat, tmp_path):
    ingest(cat, tmp_path)
    m = {r["artifact"]: r for r in rows(cat, "provenance.release") if r["source"] == "encode"}
    assert (m["experiment"]["source_version"], m["experiment"]["version_method"],
            m["experiment"]["row_count"]) == ("2026-09-18", "retrieval_date", 3)
    assert m["file"]["row_count"] == 4
    assert m["file"]["url"] == encode.report_url("File", encode.FILE_FIELDS)


def test_experiment_rows(cat, tmp_path):
    ingest(cat, tmp_path)
    exp = {r["resource_id"]: r for r in rows(cat, "resource.encode__experiment")}
    assert set(exp) == {"encode:ENCSR000AAA", "encode:ENCSR000BBB", "encode:ENCSR000CCC"}
    a = exp["encode:ENCSR000AAA"]
    assert (a["taxon_id"], a["biosample_term_id"], a["target_label"]) == (9606, "EFO:0002067", "H3K27ac")
    assert a["target_gene_ids"] == ["126961", "333932", "653604"]   # sorted
    assert a["assemblies"] == ["GRCh38", "hg19"]
    assert a["portal_uri"] == "https://www.encodeproject.org/experiments/ENCSR000AAA/"
    assert exp["encode:ENCSR000CCC"]["taxon_id"] is None
    assert exp["encode:ENCSR000BBB"]["target_gene_ids"] is None


def test_file_rows_carry_typed_uris(cat, tmp_path):
    ingest(cat, tmp_path)
    f = {r["resource_id"]: r for r in rows(cat, "resource.encode__file")}
    a = f["encode:ENCFF000AAA"]
    assert a["size"] == 2390755432666 and a["md5sum"] == "abc" and a["assembly"] == "GRCh38"
    assert a["https_uri"] == "https://www.encodeproject.org/files/ENCFF000AAA/@@download/ENCFF000AAA.bed.gz"
    assert a["s3_uri"] == "s3://encode-public/2020/ENCFF000AAA.bed.gz"
    assert (a["dataset_id"], a["dataset_type"]) == ("encode:ENCSR000AAA", "experiments")
    assert a["derived_from"] == ["encode:ENCFF000BBB", "encode:GRCh38_EBV.chrom.sizes"]
    assert a["preferred_default"] is True and a["restricted"] is None
    assert f["encode:ENCFF000CCC"]["dataset_type"] == "annotations"
    sra = f["encode:SRR1270455"]   # keyed by title where there is no accession
    assert sra["restricted"] is True and sra["no_file_available"] is True and sra["s3_uri"] is None
    assert sra["https_uri"] is None   # href is published even so; it would not resolve


def test_relationship_rows(cat, tmp_path):
    ingest(cat, tmp_path)
    rel = rows(cat, "resource.resource_relationship")
    assert all(r["source"] == "encode" for r in rel)
    got = {(r["resource_id"], r["relationship"], r["target_id"]) for r in rel}
    assert got == {
        ("encode:ENCFF000AAA", "part_of_dataset", "encode:ENCSR000AAA"),
        ("encode:ENCFF000BBB", "part_of_dataset", "encode:ENCSR000AAA"),
        ("encode:ENCFF000CCC", "part_of_dataset", "encode:ENCSR999ZZZ"),
        ("encode:SRR1270455", "part_of_dataset", "encode:ENCSR000BBB"),
        ("encode:ENCSR000AAA", "has_biosample", "EFO:0002067"),
        ("encode:ENCSR000BBB", "has_biosample", "UBERON:0002107"),
        ("encode:ENCSR000CCC", "has_biosample", "NTR:0000001"),
        ("encode:ENCSR000AAA", "has_target_gene", "ncbigene:126961"),
        ("encode:ENCSR000AAA", "has_target_gene", "ncbigene:333932"),
        ("encode:ENCSR000AAA", "has_target_gene", "ncbigene:653604"),
    }


def test_rerun_is_a_noop(cat, tmp_path):
    ingest(cat, tmp_path)
    counts = ingest(cat, tmp_path)
    for t in ("resource.encode__experiment", "resource.encode__file", "resource.resource_relationship"):
        assert counts[t]["written"] == 0, t
    assert len(rows(cat, "resource.encode__file")) == 4


def test_second_crawl_retires_changes_and_adds(cat, tmp_path):
    ingest(cat, tmp_path)
    experiments = [dict(EXPERIMENTS[0]), dict(EXPERIMENTS[1], status="revoked"),   # CCC gone
                   {"accession": "ENCSR000DDD", "status": "released",
                    "biosample_ontology.term_id": "CL:0000084"}]
    counts = ingest(cat, tmp_path, release="2026.10", experiments=experiments, files=FILES[:3],
                    date="2026-10-02")
    c = counts["resource.encode__experiment"]
    assert (c["new"], c["changed"], c["retired"], c["unchanged"]) == (1, 1, 1, 1)
    assert counts["resource.encode__file"]["retired"] == 1

    live = {r["resource_id"]: r for r in rows(cat, "resource.encode__experiment", row_filter="valid_to IS NULL")}
    assert set(live) == {"encode:ENCSR000AAA", "encode:ENCSR000BBB", "encode:ENCSR000DDD"}
    assert live["encode:ENCSR000BBB"]["status"] == "revoked"
    assert live["encode:ENCSR000BBB"]["valid_from"] == "2026.10"
    gone = next(r for r in rows(cat, "resource.encode__experiment") if r["accession"] == "ENCSR000CCC")
    assert (gone["valid_from"], gone["valid_to"]) == (REL, "2026.10")

    rel = {(r["resource_id"], r["target_id"]): r["valid_to"] for r in rows(cat, "resource.resource_relationship")}
    assert rel[("encode:ENCSR000CCC", "NTR:0000001")] == "2026.10"
    assert rel[("encode:SRR1270455", "encode:ENCSR000BBB")] == "2026.10"
    assert rel[("encode:ENCSR000DDD", "CL:0000084")] is None
    # raw holds the latest crawl only
    assert {r["retrieval_date"] for r in rows(cat, "raw.encode__experiment")} == {"2026-10-02"}


def test_another_writers_relationships_are_untouched(cat, tmp_path):
    """BEDbase's rows target these accessions; ENCODE's scope must never retire them."""
    import pyarrow as pa
    from pyiceberg.expressions import EqualTo
    from bioconice import merge
    bed = pa.Table.from_pylist([{"resource_id": "bed1", "relationship": "derived_from_experiment",
                                 "target_id": "encode:ENCSR000AAA", "source": "bedbase"}])
    merge.merge(cat, "resource.resource_relationship", bed, REL, EqualTo("source", "bedbase"))
    ingest(cat, tmp_path)
    ingest(cat, tmp_path, release="2026.10", experiments=EXPERIMENTS[:1], files=FILES[:1])
    rel = [r for r in rows(cat, "resource.resource_relationship") if r["source"] == "bedbase"]
    assert len(rel) == 1 and rel[0]["valid_to"] is None
    # and it resolves: the point of this source
    assert rel[0]["target_id"] in {r["resource_id"] for r in rows(cat, "resource.encode__experiment")}


def test_short_download_fails_before_landing(cat, tmp_path):
    path = report(tmp_path / "file.tsv", "File", encode.FILE_FIELDS, FILES)
    assert encode._verify(path, encode.FILE_FIELDS, total=4) == "2026-09-18"
    with pytest.raises(SystemExit, match="4 rows but the portal reports 5"):
        encode._verify(path, encode.FILE_FIELDS, total=5)


def test_a_report_with_other_fields_fails_loudly(cat, tmp_path):
    """The read is positional, so a report that is not ours column for column must not land."""
    path = report(tmp_path / "e.tsv", "Experiment", encode.EXPERIMENT_FIELDS[:-1], EXPERIMENTS)
    with pytest.raises(SystemExit, match="possible_controls"):
        encode.land_raw(cat, REL, experiments=path, files=path)
    assert "raw.encode__experiment" not in {".".join(t) for ns in cat.list_namespaces()
                                            for t in cat.list_tables(ns)}


def test_every_column_is_documented(cat, tmp_path):
    ingest(cat, tmp_path)
    for identifier in ("raw.encode__experiment", "raw.encode__file",
                       "resource.encode__experiment", "resource.encode__file"):
        table = cat.load_table(identifier)
        assert "citing-encode" in table.properties["comment"], identifier
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"
