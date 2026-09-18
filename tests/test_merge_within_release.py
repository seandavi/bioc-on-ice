"""Corrections within the release under construction: reopen, replace, drop.

Uses the Ensembl fixtures: tiny.gtf and tiny_next.gtf differ by a few records.
"""

from pathlib import Path

import pytest

from bioconice import catalog, ensembl

HERE = Path(__file__).parent
INFO = {"taxon_id": 9606, "assembly": "GRCh38.p14", "accession": "GCA_000001405.29"}


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def load(cat, release, gtf):
    ensembl.land_raw(cat, release, "homo_sapiens", "116", url=str(HERE / gtf), info=INFO)
    return ensembl.transform(cat, release, INFO, "116")


def genes(cat):
    return {(r["gene_id"], r["valid_from"], r["valid_to"])
            for r in cat.load_table("annotation.gene").scan().to_arrow().to_pylist()}


def test_retired_then_reappearing_in_the_same_release_is_reopened(cat):
    load(cat, "2026.08", "tiny.gtf")
    before = genes(cat)
    load(cat, "2026.09", "tiny_next.gtf")          # some genes retired at 2026.09
    live = {g for g, vf, vt in genes(cat) if vt is None}
    retired = {g for g, vf, vt in genes(cat) if vt == "2026.09" and g not in live}
    assert retired, "fixture pair must retire at least one gene"
    counts = load(cat, "2026.09", "tiny.gtf")       # the same release, corrected
    # the retired rows are live again with their ORIGINAL valid_from; no [2026.08, 2026.09) history
    assert genes(cat) >= {(g, "2026.08", None) for g in retired}
    # nothing at all is closed at 2026.09 any more: retirements reopened, and the
    # changed-within-release genes are back to their single original version
    assert not {(g, vf, vt) for g, vf, vt in genes(cat) if vt == "2026.09"}
    assert genes(cat) == before
    assert counts["annotation.gene"]["reopened"] >= len(retired)


def test_opened_and_retired_in_the_same_release_leaves_nothing(cat):
    # tiny_next lacks one of tiny's genes, so tiny loaded after it opens a gene
    load(cat, "2026.08", "tiny_next.gtf")
    load(cat, "2026.09", "tiny.gtf")                 # a gene opens at 2026.09
    versions = {}
    for g, vf, vt in genes(cat):
        versions.setdefault(g, set()).add(vf)
    # genuinely new keys at 2026.09 — not changed genes, whose new version also opens there
    opened = {g for g, vfs in versions.items() if vfs == {"2026.09"}}
    assert opened
    load(cat, "2026.09", "tiny_next.gtf")            # it vanishes within the same release
    assert not {g for g, vf, vt in genes(cat) if g in opened}
    # and rerunning is a no-op
    counts = load(cat, "2026.09", "tiny_next.gtf")
    assert counts["annotation.gene"]["written"] == 0


def test_stored_zero_width_rows_are_dropped_on_the_next_merge(cat):
    """Rows written as [R, R) by the old merge are junk visible at no release; a merge cleans them."""
    load(cat, "2026.09", "tiny.gtf")
    t = cat.load_table("annotation.gene")
    import pyarrow as pa
    rows = t.scan().to_arrow().to_pylist()
    rows[0]["valid_to"] = "2026.09"                   # forge one zero-width row in storage
    rows[0]["gene_id"] = "ENSG_FORGED"
    t.append(pa.Table.from_pylist(rows[:1]).cast(t.schema().as_arrow()))
    assert ("ENSG_FORGED", "2026.09", "2026.09") in genes(cat)
    load(cat, "2026.09", "tiny.gtf")
    assert not {g for g, *_ in genes(cat) if g == "ENSG_FORGED"}
