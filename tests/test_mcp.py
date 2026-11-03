"""bioconice.mcp: the SQL guard and release resolver, offline; one live smoke test.

Offline tests never touch the network. The live smoke test (marked `slow`, deselected
by default per pyproject.toml's addopts) is the scripted session issue #100 asks for:
list -> describe -> resolve -> query the TP53 citation recipe -> the README's numbers.
Run it with `uv run pytest -m slow`.
"""

import pytest

from bioconice import mcp


# --------------------------------------------------------------------------
# guard_sql
# --------------------------------------------------------------------------

@pytest.mark.parametrize("sql", [
    "SELECT 1",
    "select * from b.annotation.gene",
    "WITH a AS (SELECT 1) SELECT * FROM a",
    "  select 1  ",
    "SELECT 1;",
])
def test_guard_sql_allows_single_select_or_with(sql):
    mcp.guard_sql(sql)  # no raise


@pytest.mark.parametrize("sql", [
    "INSERT INTO x VALUES (1)",
    "UPDATE x SET a = 1",
    "DELETE FROM x",
    "ATTACH 'x.db' AS x",
    "COPY (SELECT 1) TO 'x.csv'",
    "PRAGMA database_list",
    "CREATE TABLE x (a int)",
    "DROP TABLE x",
    "INSTALL httpfs",
    "LOAD httpfs",
])
def test_guard_sql_rejects_forbidden_statements(sql):
    with pytest.raises(ValueError):
        mcp.guard_sql(sql)


def test_guard_sql_rejects_multi_statement():
    with pytest.raises(ValueError, match="multi-statement"):
        mcp.guard_sql("SELECT 1; SELECT 2")


def test_guard_sql_rejects_multi_statement_even_when_first_is_select():
    with pytest.raises(ValueError, match="multi-statement"):
        mcp.guard_sql("SELECT 1; DROP TABLE x")


def test_guard_sql_rejects_forbidden_keyword_inside_a_with():
    # Not valid SQL as a single expression either way, but the keyword scan
    # is defense in depth and must still catch it.
    with pytest.raises(ValueError):
        mcp.guard_sql("WITH a AS (SELECT 1) INSERT INTO x SELECT * FROM a")


# --------------------------------------------------------------------------
# resolve_release_impl, fed a manifest (no network)
# --------------------------------------------------------------------------

PROVENANCE = [{"release": "2026.09", "source": "ensembl", "source_version": "116"}]


def test_resolve_release_returns_the_id_its_pinned_url_and_provenance():
    result = mcp.resolve_release_impl(manifest={"release": "2026-11-01", "provenance": PROVENANCE})
    assert result == {"release": "2026-11-01",
                      "url": "https://data.bioconice.cancerdatasci.org/v2026-11-01",
                      "provenance": PROVENANCE}


def test_resolve_release_reads_the_pinned_manifest(monkeypatch):
    seen = []
    monkeypatch.setattr(mcp, "_manifest", lambda base: seen.append(base) or {"release": "2026-10-31"})
    assert mcp.resolve_release(release="2026-10-31")["url"].endswith("/v2026-10-31")
    assert mcp.resolve_release()["release"] == "2026-10-31"
    assert seen == ["https://data.bioconice.cancerdatasci.org/v2026-10-31", mcp.RELEASE_URL]


def test_a_pruned_release_points_at_the_hf_archive(monkeypatch):
    def gone(base):
        raise ValueError("HTTP 404")
    monkeypatch.setattr(mcp, "_manifest", gone)
    with pytest.raises(ValueError, match="hf download seandavis/bioconice"):
        mcp.resolve_release_impl(release="2026-01-01")


def test_resolve_release_refuses_a_malformed_id():
    with pytest.raises(ValueError, match="YYYY-MM-DD"):
        mcp.resolve_release_impl(release="2026.09")


# --------------------------------------------------------------------------
# Tool/resource schemas — registered, described for a model reader, and the
# "one mistake to avoid" point issue #100 asks for is actually present.
# --------------------------------------------------------------------------

@pytest.mark.anyio
async def test_tools_registered():
    tools = {t.name: t for t in await mcp.mcp.list_tools()}
    assert set(tools) == {"list_tables", "describe_table", "resolve_release", "query", "recipes"}
    for tool in tools.values():
        assert tool.description and len(tool.description) > 40, tool.name


@pytest.mark.anyio
async def test_resolve_release_takes_only_a_release_and_query_has_no_current():
    tools = {t.name: t for t in await mcp.mcp.list_tools()}
    assert set(tools["resolve_release"].inputSchema["properties"]) == {"release"}
    assert set(tools["query"].inputSchema["properties"]) == {"sql", "limit"}


@pytest.mark.anyio
async def test_docs_resource_registered():
    templates = await mcp.mcp.list_resource_templates()
    assert any(t.uriTemplate == "bioconice://docs/{table}" for t in templates)


@pytest.fixture
def anyio_backend():
    return "asyncio"


# --------------------------------------------------------------------------
# query_impl: row cap / truncation, offline against a query that needs no
# network (DuckDB's own generate_series, not the lake).
# --------------------------------------------------------------------------

def test_query_impl_rejects_bad_sql():
    with pytest.raises(ValueError):
        mcp.query_impl("DROP TABLE x")


def test_query_impl_rejects_bad_limit():
    with pytest.raises(ValueError):
        mcp.query_impl("SELECT 1", limit=0)
    with pytest.raises(ValueError):
        mcp.query_impl("SELECT 1", limit=mcp.MAX_LIMIT + 1)


def test_query_impl_truncates_and_flags_it(monkeypatch):
    # Route the module's connection at a bare in-memory DuckDB — no ATTACH,
    # no network — so this stays offline while exercising the real wrapping/
    # truncation logic in query_impl.
    import duckdb
    monkeypatch.setattr(mcp, "_duck", lambda: duckdb.connect())

    result = mcp.query_impl("SELECT * FROM range(2000)", limit=1000)
    assert result["row_count"] == 1000
    assert result["truncated"] is True

    result = mcp.query_impl("SELECT * FROM range(5)", limit=1000)
    assert result["row_count"] == 5
    assert result["truncated"] is False


# --------------------------------------------------------------------------
# Scripted session, offline — acceptance criterion 5's "recorded responses"
# half: list -> describe -> resolve -> query the TP53 recipe, against a
# fixture REST catalog and a tiny in-memory DuckDB standing in for the lake.
# The live half of the same session is test_live_scripted_session below.
# --------------------------------------------------------------------------

# A trimmed but real-shaped slice of the release's manifest.json.
_FAKE_MANIFEST = {
    "release": "2026-10-31",
    "tables": [{
        "schema": "annotation", "table": "ncbi__gene_pubmed", "row_count": 3,
        "comment": "Gene-to-publication links.",
        "columns": [
            {"name": "gene_id", "type": "VARCHAR", "doc": "NCBI Entrez GeneID, e.g. 7157."},
            {"name": "taxon_id", "type": "INTEGER",
             "doc": "NCBI taxonomy id of the organism, e.g. 9606 for human."},
            {"name": "pubmed_id", "type": "VARCHAR",
             "doc": "PubMed id (PMID) of a publication discussing this gene."},
        ],
    }],
}


def _fake_lake_con():
    """A tiny in-memory stand-in for the lake, catalog-aliased 'b' like the real ATTACH."""
    import duckdb
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS b")
    con.execute("CREATE SCHEMA b.annotation")
    con.execute(
        "CREATE TABLE b.annotation.ncbi__gene_pubmed "
        "(gene_id VARCHAR, taxon_id INTEGER, pubmed_id VARCHAR)"
    )
    con.execute("INSERT INTO b.annotation.ncbi__gene_pubmed VALUES "
                "('7157', 9606, 'p1'), ('7157', 9606, 'p2'), ('999', 9606, 'p3')")
    con.execute("CREATE TABLE b.annotation.icite__citation "
                "(citing_pmid VARCHAR, cited_pmid VARCHAR)")
    con.execute("INSERT INTO b.annotation.icite__citation VALUES "
                "('c1', 'p1'), ('c2', 'p1'), ('c3', 'p2')")
    return con


def test_scripted_session_offline(monkeypatch):
    monkeypatch.setattr(mcp, "_manifest", lambda base=None: _FAKE_MANIFEST)
    monkeypatch.setattr(mcp, "_duck", _fake_lake_con)

    tables = mcp.list_tables()
    assert tables == [{
        "namespace": "annotation", "table": "ncbi__gene_pubmed",
        "comment": "Gene-to-publication links.", "row_count": 3,
        "business_key": ["gene_id", "taxon_id", "pubmed_id"],
    }]

    described = mcp.describe_table("annotation.ncbi__gene_pubmed")
    assert described["business_key"] == ["gene_id", "taxon_id", "pubmed_id"]
    assert all(c["doc"] for c in described["columns"])
    assert described["columns"][0]["identifier_prefix"] == "ncbigene"
    with pytest.raises(ValueError, match="list_tables"):
        mcp.describe_table("annotation.nope")

    resolved = mcp.resolve_release()
    assert resolved["url"] == "https://data.bioconice.cancerdatasci.org/v2026-10-31"

    citers = mcp.query(next(r["sql"] for r in mcp.recipes() if r["name"] == "gene_to_citers"))
    assert citers["rows"][0][0] == 3  # c1, c2 cite p1; c3 cites p2


# --------------------------------------------------------------------------
# Live smoke test — the scripted session from issue #100 acceptance
# criterion 5, run against the real public endpoint. `uv run pytest -m slow`.
# --------------------------------------------------------------------------

@pytest.mark.slow
def test_live_scripted_session():
    tables = mcp.list_tables()
    assert len(tables) > 20
    assert any(t["namespace"] == "annotation" and t["table"] == "ncbi__gene" for t in tables)

    described = mcp.describe_table("annotation.ncbi__gene")
    assert described["business_key"] == ["gene_id", "taxon_id"]
    assert all(c["doc"] for c in described["columns"])

    resolved = mcp.resolve_release()
    assert resolved["url"].endswith(f"/v{resolved['release']}") and resolved["provenance"]

    papers = mcp.query(
        "SELECT COUNT(*) AS n FROM b.annotation.ncbi__gene_pubmed "
        "WHERE gene_id='7157' AND taxon_id=9606"
    )
    assert papers["rows"][0][0] == 20_400

    citers = mcp.query(next(r["sql"] for r in mcp.recipes() if r["name"] == "gene_to_citers"))
    assert citers["rows"][0][0] == 1_085_204


def test_tool_calls_are_logged_as_json(caplog):
    import json, logging
    from bioconice import mcp as m

    @m._logged
    def fake_tool(x: int = 1):
        return [1, 2, 3]

    with caplog.at_level(logging.INFO, logger="bioconice.mcp"):
        assert fake_tool(x=5) == [1, 2, 3]
    rec = json.loads(caplog.records[-1].getMessage())
    assert rec["tool"] == "fake_tool" and rec["args"] == {"x": "5"} and rec["rows"] == 3 and rec["ok"] is True

    @m._logged
    def failing():
        raise ValueError("boom")

    with caplog.at_level(logging.INFO, logger="bioconice.mcp"), pytest.raises(ValueError):
        failing()
    assert json.loads(caplog.records[-1].getMessage())["error"].startswith("ValueError")
