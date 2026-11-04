"""`bioconice gate`: a release bundle against the previous release, offline (#183 step 5).

Bundles are tiny Parquet + manifest.json in the `bioconice publish` layout, under tmp_path.
"""

import hashlib
import json
import sys

import duckdb
import pytest

from bioconice import gate
from bioconice.__main__ import main

SIGNATURE_TAXON = (
    "SELECT 'bsdb:' || (i // 3) || '/1/1' AS bsdb_id, (i % 3 + 1)::BIGINT AS member_index, "
    "'s' AS taxon_rank, 'Taxon ' || i AS taxon_name, i::BIGINT AS ncbitaxon_id, "
    "'k__x' AS taxon_lineage, '2' AS taxon_lineage_ids FROM range({n}) t(i)"
)
PROVENANCE = "SELECT 'bugsigdb' AS source, {n}::BIGINT AS row_count"


def _bundle(root, release, tables):
    """Write `tables` (name -> SELECT) as a release bundle under `root`."""
    con = duckdb.connect()
    entries = []
    for name, sql in tables.items():
        schema, table = name.split(".")
        rel = f"{schema}/{table}.parquet"
        (root / schema).mkdir(parents=True, exist_ok=True)
        n = con.execute(f"COPY ({sql}) TO '{root / rel}' (FORMAT parquet)").fetchone()[0]
        cols = con.execute(f"DESCRIBE SELECT * FROM read_parquet('{root / rel}')").fetchall()
        with (root / rel).open("rb") as f:
            sha256 = hashlib.file_digest(f, "sha256").hexdigest()
        entries.append({"schema": schema, "table": table, "path": rel, "row_count": n,
                        "sha256": sha256, "comment": None,
                        "columns": [{"name": c[0], "type": c[1], "doc": None} for c in cols]})
    (root / "manifest.json").write_text(json.dumps({"release": release, "tables": entries}))
    return root


def _run(monkeypatch, *argv):
    monkeypatch.setattr(sys, "argv", ["bioconice", "gate", *argv])
    with pytest.raises(SystemExit) as e:
        main()
    return e.value.code


@pytest.fixture
def reference(tmp_path):
    return _bundle(tmp_path / "v2026-10-31", "2026-10-31", {
        "annotation.signature_taxon": SIGNATURE_TAXON.format(n=30),
        "provenance.release": PROVENANCE.format(n=1)})


def test_grown_release_passes_with_exempt_table_warned(tmp_path, reference, monkeypatch, capsys):
    candidate = _bundle(tmp_path / "candidate", "2026-11-01", {
        "annotation.signature_taxon": SIGNATURE_TAXON.format(n=33),
        "provenance.release": PROVENANCE.format(n=1)})
    assert _run(monkeypatch, "--release", str(candidate), "--reference", str(reference)) == 0
    out = capsys.readouterr().out
    assert out.startswith(f"release 2026-11-01 against {reference} (release 2026-10-31)\n")
    assert "annotation.signature_taxon\n" in out and "  ok    key_unique" in out
    assert "  ok    manifest_row_count  (parquet 33, manifest 33)" in out
    assert "  warn  contract_declared" in out and out.endswith("PASS\n")
    # README queries name tables this bundle lacks: they fail, but only as warnings.
    assert "  warn  readme_query_line_" in out


def test_a_reference_not_older_than_the_release_is_refused(reference, monkeypatch, capsys):
    assert _run(monkeypatch, "--release", str(reference), "--reference", str(reference)) == 1
    out = capsys.readouterr().out
    assert "reference release 2026-10-31 is not older than 2026-10-31" in out
    assert "--reference https://data.bioconice.cancerdatasci.org/v<date>" in out


def test_a_manifest_row_count_that_disagrees_with_its_parquet_fails(
        tmp_path, reference, monkeypatch, capsys):
    candidate = _bundle(tmp_path / "candidate", "2026-11-01", {
        "annotation.signature_taxon": SIGNATURE_TAXON.format(n=30),
        "provenance.release": PROVENANCE.format(n=1)})
    m = json.loads((candidate / "manifest.json").read_text())
    m["tables"][0]["row_count"] = 31
    (candidate / "manifest.json").write_text(json.dumps(m))
    assert _run(monkeypatch, "--release", str(candidate), "--reference", str(reference)) == 1
    assert "  FAIL  manifest_row_count  (parquet 30, manifest 31)" in capsys.readouterr().out


def test_a_new_pubmed_table_passes_on_its_contract(tmp_path, reference, monkeypatch, capsys):
    candidate = _bundle(tmp_path / "candidate", "2026-11-01", {
        "annotation.signature_taxon": SIGNATURE_TAXON.format(n=30),
        "provenance.release": PROVENANCE.format(n=1),
        "annotation.pubmed__article": "SELECT i::VARCHAR AS pmid FROM range(5) t(i)"})
    assert _run(monkeypatch, "--release", str(candidate), "--reference", str(reference)) == 0
    assert "annotation.pubmed__article\n  ok    key_not_null" in capsys.readouterr().out


def test_duplicate_key_fails(tmp_path, reference, monkeypatch, capsys):
    candidate = _bundle(tmp_path / "candidate", "2026-11-01", {
        "annotation.signature_taxon": SIGNATURE_TAXON.format(n=30) + " UNION ALL "
        + SIGNATURE_TAXON.format(n=1),
        "provenance.release": PROVENANCE.format(n=1)})
    assert _run(monkeypatch, "--release", str(candidate), "--reference", str(reference)) == 1
    out = capsys.readouterr().out
    assert "  FAIL  key_unique  (duplicated keys: 1)" in out
    assert out.endswith("FAIL: 1 required check(s) failed\n")


def test_dropped_table_fails(tmp_path, reference, monkeypatch, capsys):
    candidate = _bundle(tmp_path / "candidate", "2026-11-01", {
        "annotation.signature_taxon": SIGNATURE_TAXON.format(n=30)})
    assert _run(monkeypatch, "--release", str(candidate), "--reference", str(reference)) == 1
    assert "provenance.release\n  FAIL  present" in capsys.readouterr().out


def test_reference_defaults_to_latest(tmp_path, monkeypatch):
    seen = []
    monkeypatch.setattr(gate, "gate", lambda release, reference, *allow: seen.append(reference) or True)
    assert _run(monkeypatch, "--release", str(tmp_path)) == 0
    assert seen == ["https://data.bioconice.cancerdatasci.org/latest"]


def test_a_contracted_table_is_not_also_exempt():
    contracted = {name for c in gate.CONTRACTS for name in c.tables}
    assert not contracted & gate.UNCONTRACTED_TABLES


@pytest.mark.parametrize("bad", ["0000-00-00", None])
def test_a_malformed_release_id_is_refused(tmp_path, reference, monkeypatch, capsys, bad):
    manifest = json.loads((reference / "manifest.json").read_text())
    (reference / "manifest.json").write_text(json.dumps({**manifest, "release": bad}))
    candidate = _bundle(tmp_path / "candidate", "2026-11-01", {
        "annotation.signature_taxon": SIGNATURE_TAXON.format(n=30),
        "provenance.release": PROVENANCE.format(n=1)})
    assert _run(monkeypatch, "--release", str(candidate), "--reference", str(reference)) == 1
    assert "release ids must be YYYY-MM-DD dates" in capsys.readouterr().out


def test_every_declared_table_with_a_key_gets_a_contract():
    """declared_contract maps every column type schemas.py uses, for the tables still to migrate."""
    for name, d in gate.DECLARED.items():
        if d.business_key:
            c = gate.declared_contract(name, license="test")
            assert c.primary_key == d.business_key
            assert [x.name for x in c.columns] == [
                f.name for f in d.schema.fields if f.name not in ("valid_from", "valid_to", "landed_in")]
    assert gate.declared_contract("raw.hgnc__complete_set", ("hgnc_id",)).columns[0].nullable is False


# #222: a table that drops its row-history columns, against one that still has them.
HISTORY = "SELECT i::VARCHAR AS pmid, '2026.09' AS valid_from, NULL::VARCHAR AS valid_to FROM range(5) t(i)"


def _with_history(tmp_path, candidate_pubmed):
    reference = _bundle(tmp_path / "ref", "2026-10-31", {
        "annotation.pubmed__article": HISTORY, "provenance.release": PROVENANCE.format(n=1)})
    candidate = _bundle(tmp_path / "candidate", "2026-11-01", {
        "annotation.pubmed__article": candidate_pubmed,
        "provenance.release": PROVENANCE.format(n=1)})
    return ["--release", str(candidate), "--reference", str(reference)], candidate


def test_an_allowed_table_may_drop_row_history(tmp_path, monkeypatch, capsys):
    argv, candidate = _with_history(tmp_path, "SELECT i::VARCHAR AS pmid FROM range(5) t(i)")
    assert _run(monkeypatch, *argv, "--allow-schema-change", "annotation.pubmed__article") == 0
    out = capsys.readouterr().out
    assert "  ok    columns_unchanged  (schema change allowed: dropped valid_from, valid_to)" in out
    assert "annotation.pubmed__article drops valid_from, valid_to" in out
    assert json.loads((candidate / gate.GATE_FILE).read_text())["schema_changes"] == {
        "annotation.pubmed__article": ["valid_from", "valid_to"]}


def test_dropping_row_history_from_an_unlisted_table_fails(tmp_path, monkeypatch, capsys):
    argv, _ = _with_history(tmp_path, "SELECT i::VARCHAR AS pmid FROM range(5) t(i)")
    assert _run(monkeypatch, *argv, "--allow-schema-change", "provenance.release") == 1
    assert "  FAIL  columns_unchanged  (differs:" in capsys.readouterr().out


def test_another_column_change_fails_even_when_listed(tmp_path, monkeypatch, capsys):
    argv, _ = _with_history(tmp_path, "SELECT i::VARCHAR AS pmid, 1 AS extra FROM range(5) t(i)")
    assert _run(monkeypatch, *argv, "--allow-schema-change", "annotation.pubmed__article") == 1
    assert "  FAIL  columns_unchanged  (differs:" in capsys.readouterr().out


# #183: a lake-migrated table whose text column became typed.
def _retyped(tmp_path, n=5):
    return _with_history(tmp_path, f"SELECT i AS pmid FROM range({n}) t(i)")


def test_an_allowed_column_change_passes_and_is_recorded(tmp_path, monkeypatch, capsys):
    argv, candidate = _retyped(tmp_path)
    assert _run(monkeypatch, *argv, "--allow-column-change", "annotation.pubmed__article") == 0
    out = capsys.readouterr().out
    detail = "removed valid_from VARCHAR, valid_to VARCHAR; retyped pmid VARCHAR -> BIGINT"
    assert f"  ok    columns_unchanged  (column change allowed: {detail})" in out
    assert f"column change allowed (owner approval, #183): annotation.pubmed__article: {detail}" in out
    assert json.loads((candidate / gate.GATE_FILE).read_text())["column_changes"] == {
        "annotation.pubmed__article": {
            "added": {}, "removed": {"valid_from": "VARCHAR", "valid_to": "VARCHAR"},
            "retyped": {"pmid": {"from": "VARCHAR", "to": "BIGINT"}}, "reordered": False}}


def test_a_column_change_of_an_unlisted_table_fails(tmp_path, monkeypatch, capsys):
    argv, candidate = _retyped(tmp_path)
    assert _run(monkeypatch, *argv, "--allow-column-change", "provenance.release") == 1
    assert "  FAIL  columns_unchanged  (differs:" in capsys.readouterr().out
    assert not (candidate / gate.GATE_FILE).exists()


def test_an_allowed_column_change_still_fails_row_bounds(tmp_path, monkeypatch, capsys):
    argv, _ = _retyped(tmp_path, n=50)
    assert _run(monkeypatch, *argv, "--allow-column-change", "annotation.pubmed__article") == 1
    out = capsys.readouterr().out
    assert "  ok    columns_unchanged  (column change allowed:" in out
    assert "  FAIL  row_count_within_bounds  (50 rows vs previous 5" in out


def test_column_diff_records_added_renamed_and_reordered():
    assert gate.column_diff([("a", "INTEGER"), ("b", "VARCHAR"), ("c", "DATE")],
                            [("b", "VARCHAR"), ("a", "INTEGER"), ("d", "DATE")]) == {
        "added": {"d": "DATE"}, "removed": {"c": "DATE"}, "retyped": {}, "reordered": True}
