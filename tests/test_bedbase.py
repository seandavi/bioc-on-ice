"""BEDbase: Parquet snapshot -> resource entries, on fixture records. Run with `uv run pytest`.

Fixtures are real records trimmed from the live /v1/bed/list and /v1/bedset/list
responses (verified 2026-09-17), kept as reviewable JSON; `snapshot` reshapes them
into the three Parquet files of BEDbase's monthly export (column names and types
verified against the 2026-09-01 snapshot). tiny_bedbase_bed_v2_* is a second
snapshot built from the first by hand: one record's cell_type changes, one record
vanishes, one new record appears — the three transitions merge.merge has to get right.
"""

from pathlib import Path

import duckdb
import pytest

from bioconice import bedbase, catalog

T = Path(__file__).parent
REL = "2026.09"
MEMBERS = [("gse34610", "0000120fe8c5334bb0ce759dfcf06c3b"), ("gse34610", "0000e6a889395d78ab9bf667326d8cff"),
           ("gse33600", "0000120fe8c5334bb0ce759dfcf06c3b")]


def snapshot(directory, v="", members=MEMBERS):
    """The three export files, built from the JSON fixture pages of version `v`."""
    directory.mkdir(exist_ok=True)
    con = duckdb.connect(config={"TimeZone": "UTC"})
    paths = {k: str(directory / f"{k}.parquet") for k in bedbase.FILES}

    def pages(kind):
        return [str(T / f"tiny_bedbase_{kind}{v}_p{i}.json") for i in (0, 1)]

    con.sql(f"""COPY (
        SELECT r.* EXCLUDE (annotation, submission_date, last_update_date),
               r.submission_date::TIMESTAMPTZ AS submission_date,
               r.last_update_date::TIMESTAMPTZ AS last_update_date,
               a.* EXCLUDE (organism, description), a.organism AS species_name,
               true AS indexed, false AS file_indexed
        FROM (SELECT r, r.annotation AS a
              FROM (SELECT unnest(results) AS r FROM read_json({pages('bed')})))
    ) TO '{paths["metadata"]}'""")
    con.sql(f"""COPY (
        SELECT r.id, r.name, r.description, r.summary::VARCHAR AS summary,
               r.submission_date::TIMESTAMPTZ AS submission_date,
               r.last_update_date::TIMESTAMPTZ AS last_update_date, r.md5sum,
               NULL::VARCHAR AS bedset_means, NULL::VARCHAR AS bedset_standard_deviation,
               NULL::VARCHAR AS bedset_stats, r.bedfile_count, r.author, r.source, true AS processed
        FROM (SELECT unnest(results) AS r FROM read_json({pages('bedset')}))
    ) TO '{paths["bedsets"]}'""")
    con.execute("CREATE TABLE m (bedset_id VARCHAR, bedfile_id VARCHAR)")
    con.executemany("INSERT INTO m VALUES (?, ?)", members)
    con.sql(f"COPY m TO '{paths['bedset_membership']}'")
    return paths


def ingest(cat, release, tmp_path, v=""):
    return bedbase.ingest(cat, release, paths=snapshot(tmp_path / f"snap{v}", v))


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def test_snapshot_lands_whole(cat, tmp_path):
    counts = ingest(cat, REL, tmp_path)
    assert counts["raw.bedbase__bed"] == 6
    assert counts["raw.bedbase__bedset"] == 6
    assert counts["raw.bedbase__bedset_membership"] == 3
    raw = {r["id"]: r for r in rows(cat, "raw.bedbase__bed")}
    one = raw["0000120fe8c5334bb0ce759dfcf06c3b"]
    # the snapshot's bare sample-level columns land under their annotation_ names
    assert one["annotation_assay"] == "ATAC-seq"
    assert one["annotation_organism"] == "Homo sapiens"
    assert one["annotation_global_sample_id"] == "geo:gsm4837486"
    # TIMESTAMPTZ lands as exactly the text the API printed, so a re-land is no change
    assert one["submission_date"] == "2025-05-22T12:02:50.145308Z"
    assert one["indexed"] is True
    m = next(r for r in rows(cat, "provenance.release") if r["source"] == "bedbase_bed")
    assert m["version_method"] == "retrieval_date"  # local paths: no index, no snapshot date


def test_download_refuses_a_checksum_mismatch(tmp_path, monkeypatch):
    src = tmp_path / "f.parquet"
    src.write_bytes(b"not what the index promised")
    monkeypatch.setattr(bedbase, "_get", lambda url: open(src, "rb"))
    entry = {"file_path": "https://data2.bedbase.org/snapshot/f.parquet", "checksum": "0" * 64}
    (tmp_path / "out").mkdir()
    with pytest.raises(ValueError, match="sha256"):
        bedbase._download(entry, tmp_path / "out")


def test_membership_is_a_relationship(cat, tmp_path):
    ingest(cat, REL, tmp_path)
    members = {(r["target_id"], r["resource_id"]) for r in rows(cat, "resource.resource_relationship")
               if r["relationship"] == "member_of_bedset"}
    assert members == set(MEMBERS)


def test_genome_digest_is_the_key_alias_is_a_label(cat, tmp_path):
    ingest(cat, REL, tmp_path)
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


def test_duo_licence_is_carried_on_every_row(cat, tmp_path):
    ingest(cat, REL, tmp_path)
    bf = rows(cat, "resource.bedbase__bedfile")
    assert {r["license_id"] for r in bf} == {"DUO:0000042"}
    assert all(r["provider"] == "BEDbase" for r in bf)


def test_species_id_to_taxon_id_join(cat, tmp_path):
    ingest(cat, REL, tmp_path)
    bf = {r["resource_id"]: r for r in rows(cat, "resource.bedbase__bedfile")}
    assert bf["0000120fe8c5334bb0ce759dfcf06c3b"]["taxon_id"] == 9606
    # accession lists are sorted lists, and the joinable form is one relationship row each
    assert bf["0000120fe8c5334bb0ce759dfcf06c3b"]["sample_id"] == ["geo:gsm4837486"]
    assert bf["0000120fe8c5334bb0ce759dfcf06c3b"]["experiment_id"] == ["geo:gse159673", "geo:gse159675"]
    rel = {(r["relationship"], r["target_id"]) for r in rows(cat, "resource.resource_relationship")
           if r["resource_id"] == "0000120fe8c5334bb0ce759dfcf06c3b"
           and r["relationship"].startswith("derived_from")}
    assert rel == {("derived_from_sample", "geo:gsm4837486"),
                   ("derived_from_experiment", "geo:gse159673"), ("derived_from_experiment", "geo:gse159675")}
    assert {r["source"] for r in rows(cat, "resource.resource_relationship")} == {"bedbase"}
    assert bf["0000e6a889395d78ab9bf667326d8cff"]["taxon_id"] == 10090
    # a real co-infection record carries species_id '9606, 11676' — not one integer.
    # TRY_CAST makes this NULL rather than failing the whole ingest.
    assert bf["002f0837c40e1d93a9b2aae16d2ad5c6"]["taxon_id"] is None
    raw = {r["id"]: r for r in rows(cat, "raw.bedbase__bed")}
    assert raw["002f0837c40e1d93a9b2aae16d2ad5c6"]["annotation_species_id"] == "9606, 11676"


def test_rerun_is_idempotent(cat, tmp_path):
    ingest(cat, REL, tmp_path)
    counts = ingest(cat, REL, tmp_path)
    assert counts["resource.bedbase__bedfile"] == {"written": 0, "unchanged": 6}
    assert counts["resource.bedbase__bedset"] == {"written": 0, "unchanged": 6}
    assert len(rows(cat, "resource.bedbase__bedfile")) == 6


def test_next_snapshot_versions_changed_rows_and_retires_vanished_ones(cat, tmp_path):
    ingest(cat, REL, tmp_path)
    counts = ingest(cat, "2026.10", tmp_path, v="_v2")

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


def test_every_column_is_documented(cat, tmp_path):
    """SPEC.md section B1: a table whose columns lack doc does not ship."""
    ingest(cat, REL, tmp_path)
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
