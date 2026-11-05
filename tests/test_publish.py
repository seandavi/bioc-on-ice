"""`bioconice publish`: a release bundle from the local sqlite warehouse, offline.

The bundle mechanics are cdsci.publish (cdsci-lake ADR-0027, tested there); these tests
cover what bioc adds: TABLES over the live catalog, current rows only, and the docs."""

import json
from pathlib import Path

import duckdb
import pytest

from bioconice import catalog, ensembl, publish

HERE = Path(__file__).parent
HUMAN = {"taxon_id": 9606, "assembly": "GRCh38.p14", "accession": "GCA_000001405.29"}
ONLY = ["annotation.gene", "annotation.exon", "raw.ensembl__gtf", "provenance.release"]


@pytest.fixture
def con(tmp_path, monkeypatch):
    """The warehouse after two Ensembl releases (so some rows are closed), as DuckDB `bioc`.

    The Ensembl tables publish from the lake since #183 G1 (test_ensembl.py checks those
    SELECTs); here they keep their Iceberg SELECTs, to exercise publish over row history."""
    monkeypatch.setattr(publish, "TABLES", [
        (s, t, publish._current(f"{s}.{t}", publish.DECLARED[f"{s}.{t}"])
         if f"{s}.{t}" in ensembl.RELEASE else sql) for s, t, sql in publish.TABLES])
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path / "warehouse"))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    cat = catalog()
    for release, gtf, ens in (("2026.08", "tiny.gtf", "116"), ("2026.10", "tiny_next.gtf", "117")):
        ensembl.land_raw(cat, release, "x", ens, url=str(HERE / gtf), info=HUMAN)
        ensembl.transform(cat, release, HUMAN, ens)
    c = duckdb.connect()
    c.execute("ATTACH ':memory:' AS bioc")
    for ns in cat.list_namespaces():
        c.execute(f"CREATE SCHEMA bioc.{ns[0]}")
        for ident in cat.list_tables(ns):
            arrow = cat.load_table(ident).scan().to_arrow()  # noqa: F841 (read by DuckDB)
            c.execute(f"CREATE TABLE bioc.{'.'.join(ident)} AS SELECT * FROM arrow")
    return c


def test_bundle_attaches_with_current_rows_and_docs(con, tmp_path):
    out = tmp_path / "release"
    m = publish.publish(con, out, out.as_uri(), ONLY)

    assert json.loads((out / "manifest.json").read_text()) == json.loads(json.dumps(m))
    assert m["manifest_version"] == 1 and len(m["release"]) == 10 and m["provenance"]
    current = {"annotation.gene": "WHERE valid_to IS NULL", "annotation.exon": "WHERE valid_to IS NULL",
               "raw.ensembl__gtf": "", "provenance.release": ""}
    want = {n: con.sql(f"SELECT count(*) FROM bioc.{n} {w}").fetchone()[0] for n, w in current.items()}
    # the second release closed rows: the bundle has current rows, not history
    assert want["annotation.gene"] < con.sql("SELECT count(*) FROM bioc.annotation.gene").fetchone()[0]

    db = duckdb.connect()
    db.execute(f"ATTACH '{out / publish.DB_FILE}' AS rel (READ_ONLY)")
    assert {n: db.sql(f"SELECT count(*) FROM rel.{n}").fetchone()[0] for n in current} == want
    assert {f"{t['schema']}.{t['table']}": t["row_count"] for t in m["tables"]} == want
    gene = next(t for t in m["tables"] if t["table"] == "gene")
    assert gene["bytes"] == (out / "annotation/gene.parquet").stat().st_size and len(gene["sha256"]) == 64
    # declared comments travel with the views
    view = db.sql("SELECT comment FROM duckdb_views() WHERE database_name = 'rel' "
                  "AND schema_name = 'annotation' AND view_name = 'exon'").fetchone()[0]
    assert view.startswith(publish.DECLARED["annotation.exon"].comment[:40])
    docs = dict(db.sql("SELECT column_name, comment FROM duckdb_columns() WHERE database_name = 'rel' "
                       "AND schema_name = 'annotation' AND table_name = 'exon'").fetchall())
    assert docs["end"]
    # releases carry no row history (#222)
    assert not {c["name"] for t in m["tables"] for c in t["columns"]} & set(publish.ROW_HISTORY)

    # restore.sql builds a local copy and checks it against manifest.json
    local = duckdb.connect()
    local.execute((out / "restore.sql").read_text())
    assert local.sql("SELECT count(*) FROM annotation.exon").fetchone()[0] == want["annotation.exon"]


def test_published_docs_never_mention_row_history():
    # manifest.json, views.sql and the MCP carry these docs; releases have no row history (#222)
    for schema, table, _ in publish.TABLES:
        comment, docs = publish._docs(f"{schema}.{table}")
        texts = [comment or ""] + [v for k, v in docs.items() if k not in publish.ROW_HISTORY]
        assert not [t for t in texts if any(c in t for c in publish.ROW_HISTORY)], f"{schema}.{table}"


def test_publish_refuses_an_earlier_runs_directory(con, tmp_path):
    # cdsci-publish's rule; scripts/release.sh clears an interrupted run's directory first
    out = tmp_path / "release"
    out.mkdir()
    (out / "manifest.json").write_text('{"tables": []}')
    with pytest.raises(ValueError, match="not empty"):
        publish.publish(con, out, out.as_uri(), ONLY)


def test_views_keep_the_https_url_before_it_is_uploaded(con, tmp_path):
    out = tmp_path / "release"
    publish.publish(con, out, "https://data.example.org/latest/", ["annotation.gene"])
    url = "https://data.example.org/latest/annotation/gene.parquet"
    assert f"read_parquet('{url}')" in (out / "views.sql").read_text()
    db = duckdb.connect(str(out / publish.DB_FILE), read_only=True)
    assert url in db.sql("SELECT sql FROM duckdb_views() WHERE view_name = 'gene'").fetchone()[0]
