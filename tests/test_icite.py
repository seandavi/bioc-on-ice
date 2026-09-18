"""iCite: land whole -> derive publication + per-snapshot metrics, on a tiny CSV.

Offline: the fixture stands in for the extracted icite_metadata.csv, so the
Figshare resolution and the 14 GB download are not exercised here. Run with
`uv run pytest`.
"""

from pathlib import Path

import pytest

from bioconice import catalog, icite

CSV = str(Path(__file__).parent / "tiny_icite.csv")
REL = "2026.09"


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def test_raw_is_verbatim_text_and_whole(cat):
    counts = icite.ingest(cat, REL, snapshot="2026-08", csv=CSV)
    assert counts["raw.icite__metadata [2026-08]"] == 3
    raw = {r["pmid"]: r for r in rows(cat, "raw.icite__metadata")}
    # quotes and commas survive the stated dialect; lists stay as text
    assert raw["2000000"]["title"] == 'A "quoted" title'
    assert raw["1000000"]["cited_by"] == "2000000 3000000"
    assert raw["3000000"]["authors"] is None
    # verbatim: nothing is normalised on the way in
    assert raw["1000000"]["doi"] == "https://doi.org/10.1000/ONE "
    assert raw["3000000"]["title"] == "  Editorial note "
    assert {r["snapshot"] for r in raw.values()} == {"2026-08"}
    m = next(r for r in rows(cat, "provenance.release") if r["source"] == "icite")
    assert (m["source_version"], m["version_method"]) == ("2026-08", "release_number")


def test_publication_is_typed_and_metrics_are_per_snapshot(cat):
    icite.ingest(cat, REL, snapshot="2026-08", csv=CSV)
    pub = {r["pmid"]: r for r in rows(cat, "annotation.icite__publication")}
    assert pub["1000000"]["year"] == 2001
    # DOI: resolver prefix and case and whitespace gone; a non-DOI is NULL, not kept
    assert pub["1000000"]["doi"] == "10.1000/one"
    assert pub["3000000"]["doi"] is None
    assert pub["3000000"]["title"] == "Editorial note"
    assert pub["1000000"]["is_research_article"] is True
    assert pub["2000000"]["doi"] is None and pub["2000000"]["is_clinical"] is True
    met = {r["pmid"]: r for r in rows(cat, "annotation.icite__metrics")}
    assert met["1000000"]["citation_count"] == 120
    assert met["2000000"]["relative_citation_ratio"] is None  # under two years: provisional
    assert met["2000000"]["provisional"] is True
    assert {r["snapshot"] for r in met.values()} == {"2026-08"}


def test_citation_graph_is_exploded_and_sharded(cat):
    counts = icite.ingest(cat, REL, snapshot="2026-08", csv=CSV)
    edges = {(r["citing_pmid"], r["cited_pmid"]) for r in rows(cat, "annotation.icite__citation")}
    assert edges == {("2000000", "1000000"), ("3000000", "1000000"), ("3000000", "2000000")}
    assert all(r["shard"] == int(r["cited_pmid"]) % icite.SHARDS
               for r in rows(cat, "annotation.icite__citation"))
    written = sum(c["written"] for k, c in counts.items() if k.startswith("annotation.icite__citation"))
    assert written == 3
    # a cited paper need not have citers of its own: 3000000 cites and is not cited
    assert {r["cited_pmid"] for r in rows(cat, "annotation.icite__citation")} == {"1000000", "2000000"}


def test_next_snapshot_keeps_old_metrics_and_versions_only_changed_papers(cat, tmp_path):
    icite.ingest(cat, REL, snapshot="2026-08", csv=CSV)
    # next month: one paper's citations grew, its title is unchanged; one paper vanished
    nxt = tmp_path / "next.csv"
    lines = Path(CSV).read_text().splitlines()
    lines[1] = lines[1].replace(",120,4.8,", ",130,5.1,")
    nxt.write_text("\n".join(lines[:3]) + "\n")
    counts = icite.ingest(cat, "2026.10", snapshot="2026-09", csv=str(nxt))

    # publication: nothing changed for the two that remain; the third is retired
    assert counts["annotation.icite__publication"]["unchanged"] == 2
    assert counts["annotation.icite__publication"]["retired"] == 1
    live = rows(cat, "annotation.icite__publication", row_filter="valid_to IS NULL")
    assert {r["pmid"] for r in live} == {"1000000", "2000000"}

    # metrics: both snapshots coexist, keyed by snapshot; nothing retired
    met = rows(cat, "annotation.icite__metrics")
    assert sorted((r["pmid"], r["snapshot"], r["citation_count"]) for r in met
                  if r["pmid"] == "1000000") == [("1000000", "2026-08", 120), ("1000000", "2026-09", 130)]
    assert all(r["valid_to"] is None for r in met)
    # edges: 3000000's record is gone, so the two edges it asserted are retired;
    # 2000000 -> 1000000 carries forward untouched
    cit = [c for k, c in counts.items() if k.startswith("annotation.icite__citation")]
    assert sum(c.get("retired", 0) for c in cit) == 2 and sum(c["unchanged"] for c in cit) == 1
    live = rows(cat, "annotation.icite__citation", row_filter="valid_to IS NULL")
    assert {(r["citing_pmid"], r["cited_pmid"]) for r in live} == {("2000000", "1000000")}
    # raw holds the latest snapshot only
    assert {r["snapshot"] for r in rows(cat, "raw.icite__metadata")} == {"2026-09"}


def test_unknown_flag_vocabulary_fails_loudly(cat, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text(Path(CSV).read_text().replace(",Nature,True,", ",Nature,Maybe,"))
    with pytest.raises(Exception, match="is_research_article: unexpected Maybe"):
        icite.ingest(cat, REL, snapshot="2026-08", csv=str(bad))


def test_invariant_violation_fails_before_any_write(cat, tmp_path):
    bad = tmp_path / "bad.csv"
    bad.write_text(Path(CSV).read_text().replace(",Nature,True,", ",Nature,True,").replace(",2001,", ",3001,"))
    with pytest.raises(ValueError, match=r"year is plausible \(1 rows\)"):
        icite.ingest(cat, REL, snapshot="2026-08", csv=str(bad))
    assert "annotation.icite__publication" not in {".".join(t) for ns in cat.list_namespaces()
                                                   for t in cat.list_tables(ns)}
