"""OBO ontologies: land whole -> derive term + relationship, on a tiny OBO Graphs JSON fixture.

Offline: tiny_cl.json stands in for a real release download. Run with `uv run pytest`.
"""

import json
from pathlib import Path

import pytest

from bioconice import catalog, obo, schemas

FIXTURE = Path(__file__).parent / "tiny_cl.json"
REL = "2026.09"


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def rows(cat, identifier, **kw):
    return cat.load_table(identifier).scan(**kw).to_arrow().to_pylist()


def test_registry_matches_schemas(cat):
    """schemas._OBO_ONTOLOGIES can't import obo.REGISTRY (circular); they must still agree."""
    assert set(obo.REGISTRY) == {n for n, _ in schemas._OBO_ONTOLOGIES}
    for name, licence in schemas._OBO_ONTOLOGIES:
        assert obo.REGISTRY[name][1] == licence
    assert "hpo" not in obo.REGISTRY  # licence review first, per issue #83


def test_raw_is_landed_whole_as_nodes_and_edges(cat):
    counts = obo.ingest(cat, REL, "cl", url=str(FIXTURE))
    assert counts["raw.obo__cl [http://purl.obolibrary.org/obo/cl/releases/2024-01-01/cl.json]"] == 9
    raw = rows(cat, "raw.obo__cl")
    assert sum(r["kind"] == "node" for r in raw) == 5
    assert sum(r["kind"] == "edge" for r in raw) == 4
    assert {r["release_version"] for r in raw} == {
        "http://purl.obolibrary.org/obo/cl/releases/2024-01-01/cl.json"}
    m = next(r for r in rows(cat, "provenance.release") if r["source"] == "obo_cl")
    assert m["version_method"] == "release_number"
    assert m["source_version"] == "http://purl.obolibrary.org/obo/cl/releases/2024-01-01/cl.json"


def test_term_synonyms_and_obsolete_with_replaced_by(cat):
    obo.ingest(cat, REL, "cl", url=str(FIXTURE))
    term = {r["term_id"]: r for r in rows(cat, "ontology.term")}
    assert len(term) == 5  # exactly count(nodes) in the fixture — acceptance criterion 3

    t_cell = term["CL:0000084"]
    assert t_cell["synonyms"] == "T lymphocyte|T-cell"
    assert t_cell["obsolete"] is False and t_cell["replaced_by"] is None

    obsolete = term["CL:9999999"]
    assert obsolete["obsolete"] is True
    assert obsolete["replaced_by"] == "CL:0000000"  # kept, never dropped
    assert term["CL:0000000"]["synonyms"] is None


def test_relationship_is_a_and_part_of(cat):
    obo.ingest(cat, REL, "cl", url=str(FIXTURE))
    rel = rows(cat, "ontology.relationship")
    assert {(r["subject_id"], r["predicate"], r["object_id"]) for r in rel} == {
        ("CL:0000624", "is_a", "CL:0000084"),
        ("CL:0000084", "is_a", "CL:0000542"),
        ("CL:0000542", "is_a", "CL:0000000"),
        ("CL:0000624", "part_of", "CL:0000542"),
    }
    assert {r["ontology"] for r in rel} == {"cl"}


def test_is_a_closure_reaches_cell(cat):
    """Acceptance criterion 4, in miniature: CL:0000624 -> CL:0000084 -> CL:0000542 -> CL:0000000."""
    obo.ingest(cat, REL, "cl", url=str(FIXTURE))
    import duckdb
    con = duckdb.connect()
    con.register("rel", cat.load_table("ontology.relationship").scan().to_arrow())
    closure = con.sql("""
        WITH RECURSIVE c AS (
            SELECT object_id FROM rel WHERE subject_id = 'CL:0000624' AND predicate = 'is_a'
          UNION
            SELECT r.object_id FROM c JOIN rel r ON c.object_id = r.subject_id AND r.predicate = 'is_a'
        )
        SELECT object_id FROM c
    """).fetchall()
    reached = {r[0] for r in closure}
    assert {"CL:0000084", "CL:0000542", "CL:0000000"} <= reached


def test_rerun_is_idempotent(cat):
    obo.ingest(cat, REL, "cl", url=str(FIXTURE))
    counts = obo.ingest(cat, "2026.10", "cl", url=str(FIXTURE))
    assert counts["ontology.term"]["written"] == 0
    assert counts["ontology.term"]["unchanged"] == 5
    assert counts["ontology.relationship"]["written"] == 0
    assert counts["ontology.relationship"]["unchanged"] == 4
    live = rows(cat, "ontology.term", row_filter="valid_to IS NULL")
    assert {r["valid_from"] for r in live} == {REL}


def test_renamed_term_opens_a_contiguous_version(cat, tmp_path):
    """A term renamed upstream: two rows, valid_to of the first == valid_from of the second."""
    obo.ingest(cat, REL, "cl", url=str(FIXTURE))

    doc = json.loads(FIXTURE.read_text())
    graph = doc["graphs"][0]
    graph["meta"]["version"] = "http://purl.obolibrary.org/obo/cl/releases/2024-06-01/cl.json"
    lymphocyte = next(n for n in graph["nodes"] if n["id"].endswith("CL_0000542"))
    lymphocyte["lbl"] = "lymphocyte (renamed)"
    renamed = tmp_path / "renamed_cl.json"
    renamed.write_text(json.dumps(doc))

    counts = obo.ingest(cat, "2026.10", "cl", url=str(renamed))
    assert counts["ontology.term"]["changed"] == 1
    assert counts["ontology.term"]["unchanged"] == 4

    versions = sorted(
        (r["name"], r["valid_from"], r["valid_to"])
        for r in rows(cat, "ontology.term") if r["term_id"] == "CL:0000542")
    assert versions == [
        ("lymphocyte", REL, "2026.10"),
        ("lymphocyte (renamed)", "2026.10", None),
    ]


def test_every_column_is_documented(cat):
    """SPEC.md section B1, for the tables this source creates."""
    obo.ingest(cat, REL, "cl", url=str(FIXTURE))
    for identifier in ("raw.obo__cl", "ontology.term", "ontology.relationship"):
        table = cat.load_table(identifier)
        assert table.properties.get("comment"), identifier
        for f in table.schema().fields:
            assert f.doc, f"{identifier}.{f.name} has no doc"


def test_landing_no_rows_fails_loudly(cat, tmp_path):
    empty = tmp_path / "empty.json"
    empty.write_text(json.dumps({"graphs": [{"meta": {"version": "x"}, "nodes": [], "edges": []}]}))
    with pytest.raises(SystemExit, match="yielded no nodes or edges"):
        obo.land_raw(cat, REL, "cl", url=str(empty))
