"""SPEC.md acceptance criteria, sections A-C, against the live deployment — anonymously.

Every test is `slow` (deselected by default): `uv run pytest -m slow tests/acceptance`.
Everything goes through the icegate endpoint (C1) with no credential: SELECTs, metadata
GETs, and one unauthenticated write probe that must be refused. Criteria that need the
write key or a browser are `pytest.skip` stubs naming what blocks them, and criteria the
live deployment does not meet are `xfail(strict=True)`, so the report shows the gaps
instead of hiding them — and a strict xfail starts failing the day the gap closes.

The releases named here (2026.08 closed, 2026.09 current) are the two the catalog holds.
"""

import json
import urllib.error
import urllib.request
from pathlib import Path

import duckdb
import pytest

from bioconice import mcp  # reused for its anonymous REST metadata reader only

pytestmark = pytest.mark.slow

EARLIER, LATER = "2026.08", "2026.09"
QUESTIONS = json.loads((Path(__file__).parent / "questions.json").read_text())["questions"]
UA = {"User-Agent": "bioconice-acceptance"}  # Cloudflare's WAF blocks urllib's default


def at(release, alias=""):
    a = f"{alias}." if alias else ""
    return f"{a}valid_from <= '{release}' AND ({a}valid_to IS NULL OR {a}valid_to > '{release}')"


@pytest.fixture(scope="module")
def con():
    """C2, DuckDB: the README's ATTACH verbatim — an endpoint, a warehouse name, no token."""
    con = duckdb.connect()
    con.execute("INSTALL iceberg; LOAD iceberg; INSTALL httpfs; LOAD httpfs;")
    con.execute(f"ATTACH '{mcp.WAREHOUSE}' AS bioc (TYPE ICEBERG, ENDPOINT '{mcp.ENDPOINT}', "
                "AUTHORIZATION_TYPE 'none')")
    return con


@pytest.fixture(scope="module")
def pyi():
    """C2, PyIceberg: stock REST config. Built directly rather than via load_catalog so no
    ~/.pyiceberg.yaml or PYICEBERG_* catalog entry — and no token in one — is merged in.
    `auth: noop` is required: PyIceberg's default legacy-OAuth2 manager gets a 401."""
    from pyiceberg.catalog.rest import RestCatalog
    return RestCatalog("acceptance_anonymous", uri=mcp.ENDPOINT, warehouse=mcp.WAREHOUSE,
                       auth={"type": "noop"})


@pytest.fixture(scope="module")
def tables():
    """Every table's live description, read off anonymous REST metadata."""
    return [mcp._describe(ns, t) for ns in mcp._namespaces() for t in mcp._table_names(ns)]


# --------------------------------------------------------------------------
# B4 — the question set's reference answers still hold
# --------------------------------------------------------------------------

@pytest.mark.parametrize("q", QUESTIONS, ids=[q["id"] for q in QUESTIONS])
def test_b4_reference_sql_returns_expected(con, q):
    rows = [list(r) for r in con.execute("\n".join(q["sql"])).fetchall()]
    assert sorted(rows, key=repr) == sorted(q["expected"], key=repr)


def test_b4_question_set_is_well_formed():
    assert len({q["id"] for q in QUESTIONS}) == len(QUESTIONS) >= 10
    for q in QUESTIONS:
        assert q["question"] and q["exercises"] and q["expected"], q["id"]
        assert "bioc." not in q["question"], f"{q['id']}: the question must not leak table names"


def test_b4_agent_writes_the_sql():
    pytest.skip("B4 proper — an LLM agent with catalog access only, answering questions.json — needs an "
                "agent harness and a model key; #12 delivers the version-controlled set and its answers.")


# --------------------------------------------------------------------------
# A — versioning and point-in-time
# --------------------------------------------------------------------------

@pytest.mark.parametrize("table,where", [
    ("annotation.ncbi__gene", "taxon_id = 9606"),   # real upstream churn between the two NCBI dumps
    ("annotation.gene", "taxon_id = 9606 AND source = 'ENSEMBL'"),
])
def test_a1_point_in_time_equals_what_the_release_returned_when_current(con, table, where):
    # The Iceberg snapshot from when EARLIER was current is the witness; the validity
    # predicate on today's table must reproduce its current view row for row.
    # ponytail: human only — all-taxa is a 72M-row EXCEPT; widen if a taxon-specific bug appears.
    snap = con.execute(f"""
        SELECT snapshot_id FROM iceberg_snapshots(bioc.{table})
        WHERE timestamp_ms < (SELECT MIN(retrieved_at[:19]::TIMESTAMP) FROM bioc.provenance.release
                              WHERE release = '{LATER}')
        ORDER BY sequence_number DESC LIMIT 1""").fetchone()
    if snap is None:
        pytest.skip(f"no {table} snapshot from before {LATER} survives (expired — A3 ran); "
                    "the pinned 2026.08 answers in questions.json still guard the row set")
    then = f"SELECT * EXCLUDE (valid_from, valid_to) FROM bioc.{table} AT (VERSION => {snap[0]}) WHERE {where} AND valid_to IS NULL"
    now = f"SELECT * EXCLUDE (valid_from, valid_to) FROM bioc.{table} WHERE {where} AND {at(EARLIER)}"
    n, missing, extra = con.execute(
        f"SELECT (SELECT COUNT(*) FROM ({then})), (SELECT COUNT(*) FROM ({then} EXCEPT {now})), "
        f"(SELECT COUNT(*) FROM ({now} EXCEPT {then}))").fetchone()
    assert n > 0 and (missing, extra) == (0, 0)


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="A1/A2 premise unmet live: releases 2026.08 and 2026.09 were both cut "
                   "from Ensembl 116, so no two Ensembl releases exist to compare (NCBI Gene did change; "
                   "the tests around this one run on it)")
def test_a1_two_releases_cut_from_two_ensembl_releases(con):
    n = con.execute("SELECT COUNT(DISTINCT source_version) FROM bioc.provenance.release "
                    "WHERE source = 'ensembl'").fetchone()[0]
    assert n >= 2


def test_a2_retired_rows_leave_the_later_view_and_stay_in_the_earlier(con):
    # The mechanism, on what upstream really retired between the releases: gene-publication
    # links NCBI dropped. "Current view" is pinned to LATER so a future release that re-adds
    # a link cannot move this.
    t = "bioc.annotation.ncbi__gene_pubmed"
    retired, in_earlier, still_there = con.execute(f"""
        SELECT COUNT(*), COUNT(*) FILTER (WHERE r.valid_from <= '{EARLIER}'), COUNT(c.gene_id)
        FROM {t} r
        LEFT JOIN {t} c ON c.taxon_id = 9606 AND c.gene_id = r.gene_id AND c.pubmed_id = r.pubmed_id
                       AND {at(LATER, 'c')}
        WHERE r.taxon_id = 9606 AND r.valid_to = '{LATER}'""").fetchone()
    assert retired > 0
    assert in_earlier == retired and still_there == 0


@pytest.mark.xfail(strict=True, raises=AssertionError, reason="A2 as written needs a GENE retired upstream between the releases; "
                   "none exists live — Ensembl is 116 in both, and NCBI discontinued no gene between its "
                   "2026-08-07 and 2026-09-12 dumps (every closed ncbi__gene row was a symbol/description edit)")
def test_a2_a_gene_retired_upstream_exists(con):
    # Retired = closed at LATER with no successor version opened at LATER (an edit closes one row and opens another).
    retired = 0
    for table, key in [("gene", "gene_id, taxon_id, source"), ("ncbi__gene", "gene_id, taxon_id")]:
        retired += con.execute(f"""
            SELECT COUNT(*) FROM (SELECT {key} FROM bioc.annotation.{table} WHERE valid_to = '{LATER}') r
            ANTI JOIN (SELECT {key} FROM bioc.annotation.{table} WHERE valid_from = '{LATER}') c
            USING ({key})""").fetchone()[0]
    assert retired > 0


def test_a3_expire_snapshots_then_point_in_time_is_unchanged():
    pytest.skip("needs the write key: expiring snapshots is destructive, to be done on a scratch table — #10")


def test_a4_reingesting_unchanged_source_writes_nothing():
    pytest.skip("needs the write key: a re-ingest is a write — #10")


def test_a5_history_grows_with_churn_not_with_releases(con):
    # ponytail: rows, not bytes — bytes on R2 need a bucket listing and orphan-file accounting,
    # which anonymous read cannot do. Two releases of human NCBI genes must cost ~1x, not 2x.
    total, one_release = con.execute(
        f"SELECT COUNT(*), COUNT(*) FILTER (WHERE {at(EARLIER)}) FROM bioc.annotation.ncbi__gene "
        "WHERE taxon_id = 9606").fetchone()
    assert one_release > 0 and total < 1.1 * one_release


def test_a6_every_release_row_carries_url_timestamp_and_version(con):
    n, bad = con.execute("""
        SELECT COUNT(*), COUNT(*) FILTER (WHERE url IS NULL OR retrieved_at IS NULL OR version_method IS NULL
                                          OR (source_version IS NULL AND version_method <> 'unavailable'))
        FROM bioc.provenance.release""").fetchone()
    assert n > 0 and bad == 0


def test_a6_etag_or_last_modified_is_recorded(con):
    # No longer an expected failure (#117). The live table gains the columns the first time
    # code from #117 writes to it — `bioconice migrate-manifest`, or any ingest — so until
    # then this fails, and says what is missing. Values follow with each source's next ingest.
    cols = {r[0] for r in con.execute("DESCRIBE bioc.provenance.release").fetchall()}
    assert {"etag", "last_modified"} <= cols


# --------------------------------------------------------------------------
# B — self-description
# --------------------------------------------------------------------------

@pytest.mark.xfail(strict=True, raises=AssertionError, reason="B1 fails live: raw.metadata and annotation.signature_taxon were not "
                   "created from schemas.py — no table comment and no doc on any column")
def test_b1_every_namespace_table_and_column_is_documented(tables):
    missing = [ns for ns in mcp._namespaces()
               if not mcp._rest_get(f"namespaces/{ns}")["properties"].get("comment")]
    for d in tables:
        name = f"{d['namespace']}.{d['table']}"
        missing += [name] if not d["comment"] else []
        missing += [f"{name}.{c['name']}" for c in d["columns"] if not c["doc"]]
    assert not missing


def test_b2_descriptions_reach_python_as_arrow_field_metadata(pyi):
    # Only the catalog endpoint and a stock client; no biocOnIce code touches this table.
    table = pyi.load_table("provenance.release")
    arrow = table.scan().to_arrow()
    assert all(arrow.schema.field(n).metadata.get(b"doc") for n in arrow.schema.names)
    assert table.properties["comment"]
    assert pyi.load_namespace_properties("provenance")["comment"]


def test_b2_c2_r_client():
    pytest.skip("B2 and C2 name R as well: needs an R with the duckdb/arrow packages, which this host "
                "lacks — no issue tracks it yet (#10)")


def test_b3_declared_prefixes_resolve_and_coordinates_are_one_based_inclusive(tables):
    # ponytail: checks every prefix that IS declared. Which undeclared columns are "identifier
    # columns" (ontology.term.term_id, resource_id, ...) is not machine-decidable from metadata.
    cols = [(f"{d['namespace']}.{d['table']}.{c['name']}", c) for d in tables for c in d["columns"]]
    prefixes = {c["identifier_prefix"] for _, c in cols if c["identifier_prefix"]}
    assert prefixes
    for p in sorted(prefixes):
        req = urllib.request.Request(f"https://bioregistry.io/api/registry/{p}", headers=UA)
        with urllib.request.urlopen(req, timeout=30) as resp:  # 404 raises: prefix does not resolve
            assert resp.status == 200, p
    coords = {n: c["coordinate_system"] for n, c in cols
              if c["coordinate_system"] or c["name"] in ("start", "end", "cds_start", "cds_end")}
    assert coords and set(coords.values()) == {"1-based-inclusive"}, coords


# --------------------------------------------------------------------------
# C — access through icegate
# --------------------------------------------------------------------------

def test_c1_the_gateway_announces_itself_not_the_backend():
    req = urllib.request.Request(f"{mcp.ENDPOINT}/v1/config?warehouse={mcp.WAREHOUSE}", headers=UA)
    with urllib.request.urlopen(req, timeout=30) as resp:
        overrides = json.load(resp)["overrides"]
    assert overrides["uri"] == mcp.ENDPOINT and overrides["prefix"] == mcp.WAREHOUSE


def test_c2_pyiceberg_and_duckdb_read_the_same_rows_with_stock_config(con, pyi):
    n = con.execute("SELECT COUNT(*) FROM bioc.provenance.release").fetchone()[0]
    assert n > 0 and pyi.load_table("provenance.release").scan().to_arrow().num_rows == n


def test_c3_duckdb_wasm_in_a_browser():
    pytest.skip("blocked: browser DuckDB-WASM / icegate CORS — #116")


def test_c4_anonymous_read_covers_every_public_namespace(con):
    for ns in mcp._namespaces():
        table = mcp._table_names(ns)[0]
        con.execute(f"SELECT * FROM bioc.{ns}.{table} LIMIT 1").fetchall()  # raises if refused


def test_c4_anonymous_write_is_refused():
    # The one write-shaped request in this suite. No Authorization header, and an empty body:
    # were the gateway ever to let it through, the backend would reject it (400) rather than create anything.
    req = urllib.request.Request(f"{mcp.ENDPOINT}/v1/{mcp.WAREHOUSE}/namespaces", data=b"{}", method="POST",
                                 headers={**UA, "Content-Type": "application/json"})
    with pytest.raises(urllib.error.HTTPError) as refused:
        urllib.request.urlopen(req, timeout=30)
    assert refused.value.code in (401, 403)


def test_c4_write_is_refused_whatever_key_is_presented():
    pytest.skip("needs a real key to present; this suite never holds a credential — #10")


def test_c5_table_data_comes_from_object_storage_via_vended_credentials():
    loaded = mcp._rest_get("namespaces/provenance/tables/release")
    keys = set(loaded["config"])  # keys only: never let the vended values reach a test report
    assert {"s3.access-key-id", "s3.secret-access-key", "s3.endpoint"} <= keys
    assert loaded["metadata-location"].startswith("s3://")
    gateway_host = mcp.ENDPOINT.split("//")[1]
    assert (gateway_host in loaded["config"]["s3.endpoint"]) is False
