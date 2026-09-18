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
# resolve_release_impl, fed a small fixture provenance table (no network)
# --------------------------------------------------------------------------

PROVENANCE_ROWS = [
    ("2026.08", "ensembl", "116", "release_number"),
    ("2026.08", "ncbi_gene", "2026-08-07", "retrieval_date"),
    ("2026.09", "ensembl", "116", "release_number"),
    ("2026.09", "icite", "2026-08", "release_number"),
    ("2026.09", "ncbi_gene", "2026-09-12", "retrieval_date"),
]


def test_resolve_release_by_ensembl_shortcut_picks_the_latest_matching_release():
    result = mcp.resolve_release_impl(ensembl=116, rows=PROVENANCE_ROWS)
    assert result["release"] == "2026.09"
    assert result["predicate"] == (
        "valid_from <= '2026.09' AND (valid_to IS NULL OR valid_to > '2026.09')"
    )
    assert result["provenance"] == {
        "source": "ensembl", "source_version": "116", "version_method": "release_number",
    }


def test_resolve_release_unknown_version_lists_known_ones():
    with pytest.raises(ValueError, match=r"known ensembl versions: \['116'\]"):
        mcp.resolve_release_impl(ensembl=999, rows=PROVENANCE_ROWS)


def test_resolve_release_unknown_source_lists_known_sources():
    with pytest.raises(ValueError, match="known sources"):
        mcp.resolve_release_impl(source="bogus", rows=PROVENANCE_ROWS)


def test_resolve_release_by_explicit_release_id():
    result = mcp.resolve_release_impl(release="2026.08", rows=PROVENANCE_ROWS)
    assert result["release"] == "2026.08"
    assert len(result["provenance"]) == 2  # ensembl + ncbi_gene rows at that release


def test_resolve_release_unknown_release_id():
    with pytest.raises(ValueError, match="unknown release"):
        mcp.resolve_release_impl(release="1999.01", rows=PROVENANCE_ROWS)


def test_resolve_release_icite_shortcut():
    result = mcp.resolve_release_impl(icite="2026-08", rows=PROVENANCE_ROWS)
    assert result["release"] == "2026.09"


def test_resolve_release_needs_something():
    with pytest.raises(ValueError, match="pass release="):
        mcp.resolve_release_impl(rows=PROVENANCE_ROWS)


def test_resolve_release_rejects_two_shortcuts_at_once():
    with pytest.raises(ValueError, match="only one of"):
        mcp.resolve_release_impl(ensembl=116, icite="2026-08", rows=PROVENANCE_ROWS)


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
async def test_query_and_resolve_release_descriptions_warn_about_valid_to():
    tools = {t.name: t for t in await mcp.mcp.list_tools()}
    assert "valid_to" in tools["query"].description
    assert "valid_to" in tools["resolve_release"].description


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

# A trimmed but real-shaped slice of what GET .../namespaces/<ns>/tables/<t>
# actually returns (recorded from the live endpoint on 2026-09-18).
_FAKE_TABLE_METADATA = {
    "current-schema-id": 0,
    "default-spec-id": 0,
    "partition-specs": [{"spec-id": 0, "fields": []}],
    "schemas": [{
        "schema-id": 0,
        "identifier-field-ids": [3, 2, 1],
        "fields": [
            {"id": 1, "name": "gene_id", "type": "string", "required": True,
             "doc": "NCBI Entrez GeneID, e.g. 7157."},
            {"id": 2, "name": "taxon_id", "type": "int", "required": True,
             "doc": "NCBI taxonomy id of the organism, e.g. 9606 for human."},
            {"id": 3, "name": "pubmed_id", "type": "string", "required": True,
             "doc": "PubMed id (PMID) of a publication discussing this gene."},
            {"id": 4, "name": "valid_from", "type": "string", "required": True, "doc": "..."},
            {"id": 5, "name": "valid_to", "type": "string", "required": False, "doc": "..."},
        ],
    }],
    "properties": {"comment": "Gene-to-publication links.", "bioc.column.gene_id.prefix": "ncbigene"},
    "snapshots": [{"summary": {"total-records": "3"}}],
}


def _fake_rest_get(path):
    if path == "namespaces":
        return {"namespaces": [["annotation"]]}
    if path == "namespaces/annotation/tables":
        return {"identifiers": [{"namespace": ["annotation"], "name": "ncbi__gene_pubmed"}]}
    if path == "namespaces/annotation/tables/ncbi__gene_pubmed":
        return {"metadata": _FAKE_TABLE_METADATA}
    raise AssertionError(f"unexpected REST path in offline fixture: {path}")


def _fake_lake_con():
    """A tiny in-memory stand-in for the lake, catalog-aliased 'b' like the real ATTACH."""
    import duckdb
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS b")
    con.execute("CREATE SCHEMA b.provenance")
    con.execute(
        "CREATE TABLE b.provenance.release "
        "(release VARCHAR, source VARCHAR, source_version VARCHAR, version_method VARCHAR)"
    )
    con.execute("INSERT INTO b.provenance.release VALUES "
                "('2026.09', 'ensembl', '116', 'release_number')")
    con.execute("CREATE SCHEMA b.annotation")
    con.execute(
        "CREATE TABLE b.annotation.ncbi__gene_pubmed "
        "(gene_id VARCHAR, taxon_id INTEGER, pubmed_id VARCHAR, valid_to VARCHAR)"
    )
    con.execute("INSERT INTO b.annotation.ncbi__gene_pubmed VALUES "
                "('7157', 9606, 'p1', NULL), ('7157', 9606, 'p2', NULL), ('999', 9606, 'p3', NULL)")
    con.execute("CREATE TABLE b.annotation.icite__citation "
                "(citing_pmid VARCHAR, cited_pmid VARCHAR, valid_to VARCHAR)")
    con.execute("INSERT INTO b.annotation.icite__citation VALUES "
                "('c1', 'p1', NULL), ('c2', 'p1', NULL), ('c3', 'p2', NULL)")
    return con


def test_scripted_session_offline(monkeypatch):
    monkeypatch.setattr(mcp, "_rest_get", _fake_rest_get)
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

    resolved = mcp.resolve_release(ensembl="116")
    assert resolved["release"] == "2026.09"

    citers = mcp.query(
        "WITH gene_papers AS ("
        "  SELECT pubmed_id FROM b.annotation.ncbi__gene_pubmed"
        "  WHERE gene_id='7157' AND taxon_id=9606 AND valid_to IS NULL"
        ") SELECT COUNT(*) AS n FROM b.annotation.icite__citation c"
        " JOIN gene_papers g ON c.cited_pmid = g.pubmed_id WHERE c.valid_to IS NULL"
    )
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

    resolved = mcp.resolve_release(ensembl="116")
    assert resolved["release"] >= "2026.09"

    papers = mcp.query(
        "SELECT COUNT(*) AS n FROM b.annotation.ncbi__gene_pubmed "
        "WHERE gene_id='7157' AND taxon_id=9606 AND valid_to IS NULL"
    )
    assert papers["rows"][0][0] == 20_400

    citers = mcp.query(
        "WITH gene_papers AS ("
        "  SELECT pubmed_id FROM b.annotation.ncbi__gene_pubmed"
        "  WHERE gene_id='7157' AND taxon_id=9606 AND valid_to IS NULL"
        ") SELECT COUNT(*) AS n FROM b.annotation.icite__citation c"
        " JOIN gene_papers g ON c.cited_pmid = g.pubmed_id WHERE c.valid_to IS NULL"
    )
    assert citers["rows"][0][0] == 1_085_204
