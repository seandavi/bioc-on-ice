"""provenance.release is keyed (release, source, artifact) — issue #96."""

from pathlib import Path

import pyarrow as pa
import pytest
from pyiceberg.schema import Schema

from bioconice import catalog, ensembl, merge, ncbi, schemas

ID = "provenance.release"
HERE = Path(__file__).parent
HUMAN = {"taxon_id": 9606, "assembly": "GRCh38.p14", "accession": "GCA_000001405.29"}
MOUSE = {"taxon_id": 10090, "assembly": "GRCm39", "accession": "GCA_000001635.9"}


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def manifest(cat):
    return {(r["release"], r["source"], r["artifact"]): r
            for r in cat.load_table(ID).scan().to_arrow().to_pylist()}


def test_manifest_has_a_row_per_species_and_a_rerun_replaces_only_its_own(cat):
    ensembl.land_raw(cat, "2026.09", "homo_sapiens", "116", url=str(HERE / "tiny.gtf"), info=HUMAN)
    ensembl.land_raw(cat, "2026.09", "mus_musculus", "116", url=str(HERE / "tiny_next.gtf"), info=MOUSE)
    m = manifest(cat)
    assert set(m) == {("2026.09", "ensembl", "homo_sapiens"), ("2026.09", "ensembl", "mus_musculus")}
    assert {r["source_version"] for r in m.values()} == {"116"}
    assert {a: r["url"].rsplit("/", 1)[1] for (_, _, a), r in m.items()} == {
        "homo_sapiens": "tiny.gtf", "mus_musculus": "tiny_next.gtf"}
    assert m["2026.09", "ensembl", "homo_sapiens"]["row_count"] == 11

    mouse = m["2026.09", "ensembl", "mus_musculus"]
    ensembl.land_raw(cat, "2026.09", "homo_sapiens", "116", url=str(HERE / "tiny_next.gtf"), info=HUMAN)
    m = manifest(cat)
    assert len(m) == 2 and m["2026.09", "ensembl", "mus_musculus"] == mouse
    assert m["2026.09", "ensembl", "homo_sapiens"]["url"].endswith("tiny_next.gtf")


def test_manifest_has_a_row_per_ncbi_dump(cat):
    counts = ncbi.land_raw(cat, "2026.09", urls={n: str(HERE / f"tiny_{n}.tsv") for n in ncbi.COLUMNS})
    m = manifest(cat)
    assert set(m) == {("2026.09", "ncbi_gene", n) for n in ("gene_info", "gene2ensembl", "gene_history")}
    for (_, _, name), r in m.items():
        assert r["row_count"] == counts[f"raw.ncbi__{name}"] and r["url"].endswith(f"/{name}.gz")


def seed_legacy(cat, rows):
    """The table as it was before #96: no artifact column, keyed (release, source)."""
    d = schemas.TABLES[ID]
    old = Schema(*[f for f in d.schema.fields if f.field_id <= 8], identifier_field_ids=[1, 2])
    cat.create_namespace("provenance")
    table = cat.create_table(ID, schema=old)
    base = {"version_method": "retrieval_date", "retrieved_at": "2026-09-01T00:00:00+00:00",
            "source_version": "v", "checksum": None, "row_count": 1}
    table.append(pa.Table.from_pylist(
        [{**base, "release": rel, "source": s, "url": url} for rel, s, url in rows],
        schema=old.as_arrow()))


LEGACY = [
    ("2026.08", "ensembl", "https://ftp.ensembl.org/pub/release-116/gtf/homo_sapiens/Homo_sapiens.GRCh38.116.gtf.gz"),
    ("2026.08", "ncbi_gene", "https://ftp.ncbi.nlm.nih.gov/gene/DATA/"),
    ("2026.09", "ensembl", "https://ftp.ensembl.org/pub/release-116/gtf/zonotrichia_albicollis/Z.116.gtf.gz"),
    ("2026.09", "obo_cl", "http://purl.obolibrary.org/obo/cl.json"),
    ("2026.09", "bedbase_bed", "https://api.bedbase.org/v1/bed/list"),
    ("2026.09", "ncbi_gene_orthologs", "https://ftp.ncbi.nlm.nih.gov/gene/DATA/gene_orthologs.gz"),
    ("2026.09", "cellxgene_census", "s3://census/soma/"),
    ("2026.09", "hgnc", "https://example.org/hgnc_complete_set.txt"),
    ("2026.09", "gwas_catalog", "https://example.org/gwas/2026/09/01/"),
]


def test_migrate_manifest_backfills_once_and_leaves_the_rest_alone(cat):
    seed_legacy(cat, LEGACY)
    before = cat.load_table(ID).scan().to_arrow().to_pylist()

    stats = merge.migrate_manifest(cat)
    assert stats == {"rows_before": 9, "backfilled": 7, "dropped_superseded": 0,
                     "rows_after": 9, "left_without_artifact": 2}
    m = manifest(cat)
    assert set(m) == {
        ("2026.08", "ensembl", "homo_sapiens"), ("2026.08", "ncbi_gene", None),
        ("2026.09", "ensembl", "zonotrichia_albicollis"), ("2026.09", "obo", "cl"),
        ("2026.09", "bedbase", "metadata"), ("2026.09", "ncbi_gene", "gene_orthologs"),
        ("2026.09", "cellxgene", "census"), ("2026.09", "hgnc", "complete_set"),
        ("2026.09", "gwas_catalog", None)}
    # nothing but source and artifact moved on any row
    keep = lambda r: {k: r[k] for k in before[0] if k != "source"}  # noqa: E731
    assert sorted(map(str, map(keep, m.values()))) == sorted(map(str, map(keep, before)))
    assert cat.load_table(ID).schema().identifier_field_ids == []

    # idempotent: a second run changes nothing and commits nothing
    snapshot = cat.load_table(ID).current_snapshot().snapshot_id
    assert merge.migrate_manifest(cat)["backfilled"] == 0
    assert cat.load_table(ID).current_snapshot().snapshot_id == snapshot and manifest(cat) == m


def test_migrate_manifest_drops_a_legacy_row_an_ingest_has_already_replaced(cat):
    seed_legacy(cat, [("2026.09", "obo_cl", "http://old")])
    merge.manifest(cat, "2026.09", "obo", "cl", "http://new", 5)   # an ingest after #96, before migrating
    stats = merge.migrate_manifest(cat)
    assert (stats["rows_before"], stats["dropped_superseded"], stats["rows_after"]) == (2, 1, 1)
    assert manifest(cat)["2026.09", "obo", "cl"]["url"] == "http://new"


# --- issue #117: checksum, HTTP validators, snapshot properties

def test_head_asks_nothing_of_a_local_path_and_survives_a_refusal(monkeypatch):
    def offline(*a, **kw):
        raise AssertionError("a local path must not reach the network")
    monkeypatch.setattr(merge.urllib.request, "urlopen", offline)
    none = {"etag": None, "last_modified": None}
    assert merge.head(str(HERE / "tiny.gtf")) == none == merge.head("s3://bucket/soma/")

    class Response:
        headers = {"ETag": '"5f3-abc"', "Last-Modified": "Thu, 04 Dec 2025 17:02:11 GMT"}

        def __enter__(self):
            return self

        def __exit__(self, *a):
            pass

    seen = []
    monkeypatch.setattr(merge.urllib.request, "urlopen",
                        lambda req, timeout: seen.append(req.get_method()) or Response())
    assert merge.head("https://example.org/f.gz") == {
        "etag": '"5f3-abc"', "last_modified": "Thu, 04 Dec 2025 17:02:11 GMT"}
    assert seen == ["HEAD"]

    def refused(req, timeout):
        raise OSError("405")
    monkeypatch.setattr(merge.urllib.request, "urlopen", refused)
    assert merge.head("https://example.org/f.gz") == none


def test_manifest_records_the_validators_verbatim(cat):
    merge.manifest(cat, "2026.09", "mane", "mane_summary", "https://x/f", 1,
                   etag='W/"it\'s"', last_modified="Thu, 04 Dec 2025 17:02:11 GMT")
    m = manifest(cat)["2026.09", "mane", "mane_summary"]
    assert (m["etag"], m["last_modified"], m["checksum"]) == (
        'W/"it\'s"', "Thu, 04 Dec 2025 17:02:11 GMT", None)


def test_sha256_is_of_the_file(tmp_path):
    (tmp_path / "f").write_bytes(b"abc")
    assert merge.sha256(tmp_path / "f") == (
        "ba7816bf8f01cfea414140de5dae2223b00361a396177a9cb410ff61f20015ad")


def test_every_snapshot_says_what_it_was_built_from(cat):
    gtf = str(HERE / "tiny.gtf")
    ensembl.land_raw(cat, "2026.09", "homo_sapiens", "116", url=gtf, info=HUMAN)
    ensembl.transform(cat, "2026.09", HUMAN, "116")

    raw = cat.load_table("raw.ensembl__gtf").current_snapshot()
    fetched = {"bioc.release": "2026.09", "bioc.source": "ensembl",
               "bioc.artifact": "homo_sapiens", "bioc.url": gtf}
    props = lambda s: {k: v for k, v in s.summary.additional_properties.items()  # noqa: E731
                       if k.startswith("bioc.")}
    assert props(raw) == fetched == props(cat.load_table(ID).current_snapshot())
    # a derived table names its input snapshot rather than a url: follow it to the raw one
    assert props(cat.load_table("annotation.gene").current_snapshot()) == {
        "bioc.release": "2026.09", "bioc.source": "ensembl",
        "bioc.input.raw.ensembl__gtf": str(raw.snapshot_id)}
