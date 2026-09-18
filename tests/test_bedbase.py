"""BEDbase: paging listings -> resource entries, on fixture pages. Run with `uv run pytest`.

Fixtures are real records trimmed from the live /v1/bed/list and /v1/bedset/list
responses (verified 2026-09-17), split across two 3-record "pages" the way the
live API pages at limit=100 — so these exercise the same UNNEST-across-files crawl
land_raw uses against the real endpoint. tiny_bedbase_bed_v2_* is a second crawl
built from the first by hand: one record's cell_type changes, one record vanishes,
one new record appears — the three transitions merge.merge has to get right.
"""

import json
from pathlib import Path

import pytest

from bioconice import bedbase, catalog

T = Path(__file__).parent
BED_P0, BED_P1 = str(T / "tiny_bedbase_bed_p0.json"), str(T / "tiny_bedbase_bed_p1.json")
BEDSET_P0, BEDSET_P1 = str(T / "tiny_bedbase_bedset_p0.json"), str(T / "tiny_bedbase_bedset_p1.json")
BED_V2_P0, BED_V2_P1 = str(T / "tiny_bedbase_bed_v2_p0.json"), str(T / "tiny_bedbase_bed_v2_p1.json")
BEDSET_V2_P0, BEDSET_V2_P1 = str(T / "tiny_bedbase_bedset_v2_p0.json"), str(T / "tiny_bedbase_bedset_v2_p1.json")
REL = "2026.09"


def ingest(cat, release, bed=(BED_P0, BED_P1), bedset=(BEDSET_P0, BEDSET_P1), limit=None):
    return bedbase.ingest(cat, release, limit=limit, bed_urls=list(bed), bedset_urls=list(bedset))


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def test_paging_lands_every_page_whole(cat):
    counts = ingest(cat, REL)
    assert counts["raw.bedbase__bed"] == 6
    assert counts["raw.bedbase__bedset"] == 6
    raw = {r["id"]: r for r in rows(cat, "raw.bedbase__bed")}
    assert len(raw) == 6
    # nested annotation.* survives, flattened with its prefix, verbatim
    assert raw["0000120fe8c5334bb0ce759dfcf06c3b"]["annotation_assay"] == "ATAC-seq"
    assert raw["0000120fe8c5334bb0ce759dfcf06c3b"]["annotation_global_sample_id"] == "geo:gsm4837486"
    m = next(r for r in rows(cat, "provenance.release") if r["source"] == "bedbase_bed")
    assert m["version_method"] == "retrieval_date"


def test_genome_digest_is_the_key_alias_is_a_label(cat):
    ingest(cat, REL)
    bf = {r["resource_id"]: r for r in rows(cat, "resource.bedbase__bedfile")}
    # a real record: Arabidopsis (taxon 3702) carries the alias 'hg18' — a human
    # assembly name. genome_digest is still the true, resolvable key.
    weird = bf["00180b1152501a388cd4281b2d2b2c99"]
    assert weird["genome_alias"] == "hg18"
    assert weird["genome_digest"] == "ieWVCws5MC2QFRKgH9QcN3u5_Y_3hPG6"
    assert weird["taxon_id"] == 3702
    # genome_digest is frequently absent even where the alias looks plausible
    assert bf["00044cc1b962dfa7a47b74efcd5fb373"]["genome_digest"] is None
    assert bf["00044cc1b962dfa7a47b74efcd5fb373"]["genome_alias"] == "hg38"


def test_duo_licence_is_carried_on_every_row(cat):
    ingest(cat, REL)
    bf = rows(cat, "resource.bedbase__bedfile")
    assert {r["license_id"] for r in bf} == {"DUO:0000042"}
    assert all(r["provider"] == "BEDbase" for r in bf)


def test_species_id_to_taxon_id_join(cat):
    ingest(cat, REL)
    bf = {r["resource_id"]: r for r in rows(cat, "resource.bedbase__bedfile")}
    assert bf["0000120fe8c5334bb0ce759dfcf06c3b"]["taxon_id"] == 9606
    assert bf["0000e6a889395d78ab9bf667326d8cff"]["taxon_id"] == 10090
    # a real co-infection record carries species_id '9606, 11676' — not one integer.
    # TRY_CAST makes this NULL rather than failing the whole ingest.
    assert bf["002f0837c40e1d93a9b2aae16d2ad5c6"]["taxon_id"] is None
    raw = {r["id"]: r for r in rows(cat, "raw.bedbase__bed")}
    assert raw["002f0837c40e1d93a9b2aae16d2ad5c6"]["annotation_species_id"] == "9606, 11676"


def test_rerun_is_idempotent(cat):
    ingest(cat, REL)
    counts = ingest(cat, REL)
    assert counts["resource.bedbase__bedfile"] == {"written": 0, "unchanged": 6}
    assert counts["resource.bedbase__bedset"] == {"written": 0, "unchanged": 6}
    assert len(rows(cat, "resource.bedbase__bedfile")) == 6


def test_next_crawl_versions_changed_rows_and_retires_vanished_ones(cat):
    ingest(cat, REL)
    counts = ingest(cat, "2026.10", bed=(BED_V2_P0, BED_V2_P1), bedset=(BEDSET_V2_P0, BEDSET_V2_P1))

    bf_counts = counts["resource.bedbase__bedfile"]
    assert bf_counts["new"] == 1
    assert bf_counts["changed"] == 1
    assert bf_counts["retired"] == 1
    assert bf_counts["unchanged"] == 4

    bf = rows(cat, "resource.bedbase__bedfile")
    live = {r["resource_id"]: r for r in bf if r["valid_to"] is None}
    assert len(live) == 6
    # the vanished record no longer has a live row
    assert "002f0837c40e1d93a9b2aae16d2ad5c6" not in live
    # the changed record has a new version with the new attribute...
    assert live["0000e6a889395d78ab9bf667326d8cff"]["cell_type"] == "liver"
    assert live["0000e6a889395d78ab9bf667326d8cff"]["valid_from"] == "2026.10"
    # ...and its prior version is closed, not overwritten (full Type 2, ADR-0006)
    closed = [r for r in bf if r["resource_id"] == "0000e6a889395d78ab9bf667326d8cff" and r["valid_to"]]
    assert len(closed) == 1 and closed[0]["valid_from"] == "2026.09" and closed[0]["valid_to"] == "2026.10"
    # the new record appeared
    assert live["000fc1a9fe480fd465d1652cd1779802"]["valid_from"] == "2026.10"

    bs_counts = counts["resource.bedbase__bedset"]
    assert bs_counts["retired"] == 1
    live_bs = {r["resource_id"] for r in rows(cat, "resource.bedbase__bedset") if r["valid_to"] is None}
    assert "gse170350" not in live_bs
    assert "gse231035" in live_bs


def test_bounded_crawl_never_retires_outside_the_slice_it_saw(cat):
    ingest(cat, REL)  # full crawl: 6 live bedfiles
    # a --limit run that only re-fetches page0: must not retire the other 3
    counts = ingest(cat, "2026.10", bed=(BED_P0,), bedset=(BEDSET_P0,), limit=3)
    assert counts["raw.bedbase__bed"] == 3
    assert "retired" not in counts["resource.bedbase__bedfile"]
    live = [r for r in rows(cat, "resource.bedbase__bedfile") if r["valid_to"] is None]
    assert len(live) == 6


def test_pagination_drift_duplicate_is_deduped_not_a_merge_error(cat, tmp_path):
    """BEDbase's listing is offset-paginated against a live, mutating catalog: a
    record can land twice across two pages of the same crawl (verified live
    2026-09-17, see the module docstring). Raw keeps both rows verbatim; the
    derived resource table must still produce exactly one, not fail the
    uniqueness CHECK.
    """
    p0 = json.loads(Path(BEDSET_P0).read_text())
    dup = p0["results"][0]
    drifted_p1 = {"count": 4, "limit": 3, "offset": 3, "results": [dup]}
    drifted_p1_path = tmp_path / "drifted_p1.json"
    drifted_p1_path.write_text(json.dumps(drifted_p1))

    counts = ingest(cat, REL, bedset=(BEDSET_P0, str(drifted_p1_path)))
    assert counts["raw.bedbase__bedset"] == 4  # both copies landed verbatim
    live = [r for r in rows(cat, "resource.bedbase__bedset") if r["resource_id"] == dup["id"]]
    assert len(live) == 1


def test_every_column_is_documented(cat):
    """SPEC.md section B1: a table whose columns lack doc does not ship."""
    ingest(cat, REL)
    from pyiceberg.exceptions import NoSuchTableError
    from bioconice import schemas
    for identifier in schemas.TABLES:
        try:
            table = cat.load_table(identifier)
        except NoSuchTableError:
            continue
        assert table.properties.get("comment"), identifier
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"
