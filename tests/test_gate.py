"""`bioconice gate`: a release bundle against the previous release, offline (#183 step 5).

Bundles are tiny Parquet + manifest.json in the `bioconice publish` layout, under tmp_path.
"""

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
        entries.append({"schema": schema, "table": table, "path": rel, "row_count": n,
                        "columns": [{"name": c[0], "type": c[1]} for c in cols]})
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
    assert "annotation.signature_taxon\n" in out and "  ok    key_unique" in out
    assert "  warn  contract_declared" in out and out.endswith("PASS\n")


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
    monkeypatch.setattr(gate, "gate", lambda release, reference: seen.append(reference) or True)
    assert _run(monkeypatch, "--release", str(tmp_path)) == 0
    assert seen == ["https://data.bioconice.cancerdatasci.org/latest"]


def test_a_contracted_table_is_not_also_exempt():
    contracted = {name for c in gate.CONTRACTS for name in c.tables}
    assert not contracted & gate.UNCONTRACTED_TABLES
