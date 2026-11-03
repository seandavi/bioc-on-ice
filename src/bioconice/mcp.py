"""MCP server for biocOnIce: hand a model the lake's own SQL, anonymously.

Issue #100. This is read-only by construction: DuckDB attaches the public
release's `bioconice.duckdb` READ_ONLY (ADR-0014, #193) — static files on R2,
nothing to write to and no credential to hold. The `query` tool's SQL guard is
a local line of defense against DuckDB-*local* side effects (ATTACH-ing another
database, COPY-ing to a file, LOAD-ing an extension).

Table comments, column docs and row counts are read from the release's own
`manifest.json`, so the server describes exactly what it queries. The release
does not carry business keys or identifier prefixes; those come from the
declarations in schemas.py, which the release was built from.

`bioconice-mcp` runs locally over stdio (for `uvx`/Claude Desktop/Claude
Code): queries execute on the user's machine, in DuckDB, against the public R2
release. There is no hosted endpoint — biocOnIce does not serve query compute.
"""

import functools
import json
import logging
import os
import re
import time
import urllib.error
import urllib.request

import duckdb
from mcp.server.fastmcp import FastMCP

from .schemas import TABLES as DECLARED

# A release folder: `latest/` by default, or a dated `v{YYYY-MM-DD}/` pin.
RELEASE_URL = os.environ.get("BIOCONICE_RELEASE_URL", "https://data.bioconice.cancerdatasci.org/latest").rstrip("/")
# Where the dated `v{release}/` folders sit, next to `latest/`.
SITE_URL = RELEASE_URL.rsplit("/", 1)[0]
HF_ARCHIVE = "hf download seandavis/bioconice --repo-type dataset --revision YYYY-MM"
ATTACH_SQL = f"ATTACH '{RELEASE_URL}/bioconice.duckdb' AS b (READ_ONLY)"
MAX_LIMIT = 10_000
DEFAULT_LIMIT = 1000

mcp = FastMCP(
    "bioconice",
    instructions=(
        "Bioconductor gene/transcript/exon annotation, NCBI Gene, iCite citations, "
        "OBO ontologies and dataset catalogs, served anonymously and read-only from "
        "the public biocOnIce release. Call list_tables() first to see what exists, then describe_table() on "
        "the ones you plan to join before writing SQL — the column docs say what a null "
        "means and which convention a coordinate uses, which you cannot guess correctly. "
        "A release is one dated dataset of current rows, with no row-history columns; "
        "for reproducible work, cite the pinned release URL resolve_release() returns."
    ),
)


# --------------------------------------------------------------------------
# The release's manifest.json — plain HTTP, read fresh each call: `latest/`
# moves with every publish, and the file is a few hundred KB.
# --------------------------------------------------------------------------

def _manifest(base=RELEASE_URL):
    url = f"{base}/manifest.json"
    # Cloudflare's WAF (error 1010) blocks urllib's default "Python-urllib/x.y"
    # User-Agent outright; any ordinary one satisfies it.
    req = urllib.request.Request(url, headers={"User-Agent": "bioconice-mcp"})
    try:
        with urllib.request.urlopen(req, timeout=30) as resp:
            return json.load(resp)
    except urllib.error.HTTPError as e:
        raise ValueError(f"HTTP {e.code} on {url}: {e.read().decode(errors='replace')[:500]}") from e
    except urllib.error.URLError as e:
        raise ValueError(f"could not reach {url}: {e.reason}") from e


def _split_table_ref(table):
    if "." not in table:
        raise ValueError(f"table must be 'namespace.table' (e.g. 'annotation.ncbi__gene'), got {table!r}")
    namespace, name = table.split(".", 1)
    return namespace, name


def _describe(t):
    """Everything describe_table/the docs resource need, for one manifest.json table entry."""
    declared = DECLARED.get(f"{t['schema']}.{t['table']}")
    props = declared.properties if declared else {}
    # Report the business key in column order, which is what a caller joins on.
    key = set(declared.business_key) if declared else set()
    business_key = [c["name"] for c in t["columns"] if c["name"] in key]
    columns = [{
        "name": c["name"],
        "type": c["type"],
        "doc": c["doc"],
        "in_business_key": c["name"] in key,
        "identifier_prefix": props.get(f"bioc.column.{c['name']}.prefix"),
        "coordinate_system": props.get(f"bioc.column.{c['name']}.coordinate_system"),
    } for c in t["columns"]]
    return {
        "namespace": t["schema"],
        "table": t["table"],
        "comment": t["comment"] or "",
        "business_key": business_key,
        "row_count": t["row_count"],
        "columns": columns,
    }


def _describe_ref(table):
    namespace, name = _split_table_ref(table)
    m = _manifest()
    t = next((t for t in m["tables"] if (t["schema"], t["table"]) == (namespace, name)), None)
    if t is None:
        raise ValueError(f"no table {table!r} in release {m['release']}; call list_tables() for the names")
    return _describe(t)


# --------------------------------------------------------------------------
# DuckDB, attached anonymously. One connection, made lazily and reused.
# --------------------------------------------------------------------------

_con = None


def _duck():
    global _con
    if _con is None:
        con = duckdb.connect()
        con.execute("INSTALL httpfs; LOAD httpfs;")
        con.execute(ATTACH_SQL)
        _con = con
    return _con


# --------------------------------------------------------------------------
# query()'s SQL guard: a single SELECT/WITH, no second statement, no DuckDB
# statement that has a local side effect (writing a file, attaching another
# database, loading an extension, changing a session setting).
# --------------------------------------------------------------------------

_FORBIDDEN = re.compile(
    r"\b(INSERT|UPDATE|DELETE|ATTACH|DETACH|COPY|PRAGMA|CREATE|DROP|ALTER|CALL|EXPORT|"
    r"IMPORT|INSTALL|LOAD|SET|RESET|VACUUM|CHECKPOINT|GRANT|REVOKE|TRUNCATE|MERGE|"
    r"EXECUTE|PREPARE)\b",
    re.IGNORECASE,
)
_FIRST_WORD = re.compile(r"^\s*(\w+)")


def guard_sql(sql):
    """Raise ValueError with a clear message, or return the query with its
    trailing semicolon (if any) stripped."""
    s = sql.strip()
    if s.endswith(";"):
        s = s[:-1].strip()
    if ";" in s:
        raise ValueError("multi-statement input is not allowed — run one query at a time.")
    first = _FIRST_WORD.match(s)
    word = first.group(1).upper() if first else ""
    if word not in ("SELECT", "WITH"):
        raise ValueError(f"only a single SELECT or WITH query is allowed (got {word or '(empty)'}).")
    bad = _FORBIDDEN.search(s)
    if bad:
        raise ValueError(f"disallowed keyword in query: {bad.group(0).upper()}")
    if not s:
        raise ValueError("empty query")
    return s


# --------------------------------------------------------------------------
# resolve_release()'s core: a release's own manifest.json, read fresh each call.
# --------------------------------------------------------------------------

def resolve_release_impl(release=None, manifest=None):
    """`manifest` is injectable for offline tests; live callers leave it None."""
    if release and not re.fullmatch(r"\d{4}-\d{2}-\d{2}(\.\d+)?", release):
        raise ValueError(f"a release id is YYYY-MM-DD[.N], e.g. '2026-11-01', got {release!r}")
    if manifest is None:
        try:
            manifest = _manifest(f"{SITE_URL}/v{release}" if release else RELEASE_URL)
        except ValueError as e:
            if not release:
                raise
            raise ValueError(f"no release {release} at {SITE_URL}/v{release}/ ({e}); older "
                             f"releases are pruned there but archived monthly: {HF_ARCHIVE}") from e
    return {
        "release": manifest["release"],
        "url": f"{SITE_URL}/v{manifest['release']}",
        # Every provenance.release row at build time: which source versions it holds.
        "provenance": manifest.get("provenance", []),
    }


# --------------------------------------------------------------------------
# query()'s execution: wrap the caller's query once. This is what gives a
# universal row cap and truncated flag to ANY query.
# --------------------------------------------------------------------------

def query_impl(sql, limit=DEFAULT_LIMIT):
    inner = guard_sql(sql)
    if not isinstance(limit, int) or not (0 < limit <= MAX_LIMIT):
        raise ValueError(f"limit must be an integer between 1 and {MAX_LIMIT}")
    con = _duck()
    wrapped = f"SELECT * FROM ({inner}) AS _bioconice_q LIMIT {limit + 1}"
    try:
        cur = con.execute(wrapped)
    except duckdb.Error as e:
        raise ValueError(str(e)) from e
    columns = [d[0] for d in cur.description]
    rows = cur.fetchall()
    truncated = len(rows) > limit
    rows = rows[:limit]
    return {
        "columns": columns,
        "rows": [list(r) for r in rows],
        "row_count": len(rows),
        "truncated": truncated,
    }


# --------------------------------------------------------------------------
# recipes(): fixed, worked SQL. Values are filled in with the TP53 / CL /
# UBERON examples this repo already verified live (README, issue #100); swap
# the literal ids for another gene/cell-type/taxon.
# --------------------------------------------------------------------------

RECIPES = [
    {
        "name": "gene_to_citers",
        "description": (
            "A gene's PubMed papers (NCBI gene2pubmed), then everyone who cites those papers "
            "(iCite's citation graph). Swap gene_id/taxon_id for another gene."
        ),
        "sql": (
            "WITH gene_papers AS (\n"
            "  SELECT pubmed_id FROM b.annotation.ncbi__gene_pubmed\n"
            "  WHERE gene_id = '7157' AND taxon_id = 9606  -- TP53, human\n"
            ")\n"
            "SELECT COUNT(*) AS citing_edges\n"
            "FROM b.annotation.icite__citation c\n"
            "JOIN gene_papers g ON c.cited_pmid = g.pubmed_id;"
        ),
    },
    {
        "name": "datasets_by_cell_type_rollup",
        "description": (
            "Datasets with any subtype of a cell type, by walking the ontology's is_a closure "
            "with a recursive CTE rather than listing every descendant CL term by hand. Example: "
            "human blood datasets carrying any T cell (CL:0000084) subtype."
        ),
        "sql": (
            "WITH RECURSIVE descendants(term_id) AS (\n"
            "  SELECT 'CL:0000084'  -- T cell\n"
            "  UNION\n"
            "  SELECT r.subject_id\n"
            "  FROM b.ontology.relationship r\n"
            "  JOIN descendants d ON r.object_id = d.term_id\n"
            "  WHERE r.predicate = 'is_a' AND r.ontology = 'cl'\n"
            ")\n"
            "SELECT DISTINCT rr.resource_id\n"
            "FROM b.resource.resource_relationship rr\n"
            "JOIN descendants d ON rr.target_id = d.term_id\n"
            "-- resource_relationship.resource_id is the dataset VERSION id, not dataset_id\n"
            "JOIN b.resource.cellxgene__dataset ds ON ds.dataset_version_id = rr.resource_id\n"
            "WHERE rr.relationship = 'has_cell_type'\n"
            "  AND ds.taxon_id = 9606\n"
            "  AND EXISTS (\n"
            "    SELECT 1 FROM b.resource.resource_relationship t\n"
            "    WHERE t.resource_id = rr.resource_id AND t.relationship = 'has_tissue'\n"
            "      AND t.target_id = 'UBERON:0000178'  -- blood\n"
            "  );"
        ),
    },
    {
        "name": "gene_models",
        "description": (
            "A species' gene/transcript/exon models, TxDb-style. For an earlier release, "
            "point the server at its pinned URL (resolve_release(release=...))."
        ),
        "sql": (
            "SELECT g.gene_id, g.symbol, t.transcript_id, e.rank, e.start, e.end\n"
            "FROM b.annotation.gene g\n"
            "JOIN b.annotation.transcript t USING (gene_id, taxon_id, source)\n"
            "JOIN b.annotation.exon e USING (transcript_id, taxon_id, source)\n"
            "WHERE g.taxon_id = 9606 AND g.source = 'ENSEMBL'\n"
            "ORDER BY g.gene_id, t.transcript_id, e.rank;"
        ),
    },
    {
        "name": "taxon_coverage",
        "description": (
            "How much of the catalog exists for one taxon — a quick sanity "
            "check before building a bigger query for a species you have not used here yet."
        ),
        "sql": (
            "SELECT 'annotation.ncbi__gene' AS table_name, COUNT(*) AS rows\n"
            "FROM b.annotation.ncbi__gene WHERE taxon_id = 9606\n"
            "UNION ALL\n"
            "SELECT 'annotation.gene', COUNT(*) FROM b.annotation.gene\n"
            "WHERE taxon_id = 9606\n"
            "UNION ALL\n"
            "SELECT 'annotation.ncbi__gene_pubmed', COUNT(*) FROM b.annotation.ncbi__gene_pubmed\n"
            "WHERE taxon_id = 9606;"
        ),
    },
]


# --------------------------------------------------------------------------
# Tool-call log: one JSON line per call — tool, arguments (truncated),
# milliseconds, rows returned, error. Silent unless the caller configures a
# handler for the `bioconice.mcp` logger (never on stdout: that is the stdio channel).
# --------------------------------------------------------------------------

_log = logging.getLogger("bioconice.mcp")


def _logged(fn):
    @functools.wraps(fn)
    def wrapper(*args, **kwargs):
        t0 = time.time()
        rec = {"tool": fn.__name__, "args": {k: repr(v)[:200] for k, v in kwargs.items()}}
        try:
            out = fn(*args, **kwargs)
        except Exception as e:
            rec.update(ms=round((time.time() - t0) * 1000), ok=False, error=f"{type(e).__name__}: {e}"[:300])
            _log.info(json.dumps(rec))
            raise
        rec.update(ms=round((time.time() - t0) * 1000), ok=True)
        if isinstance(out, (list, tuple)):
            rec["rows"] = len(out)
        elif isinstance(out, dict) and isinstance(out.get("rows"), list):
            rec["rows"] = len(out["rows"])
            rec["truncated"] = bool(out.get("truncated"))
        _log.info(json.dumps(rec))
        return out
    return wrapper


# --------------------------------------------------------------------------
# MCP tools
# --------------------------------------------------------------------------

@mcp.tool()
@_logged
def list_tables(namespace: str = "") -> list[dict]:
    """List every table in the public biocOnIce release (optionally one namespace).

    Call this FIRST, before describe_table or query — it is the map of what
    exists: namespace, table, its comment, its current row count, and its
    business key (what a join or a merge uses to identify one record). Pass
    `namespace` (e.g. "annotation") to narrow it; leave it empty for
    everything in the release.
    The one mistake to avoid: table names outside `annotation.*` are dotted
    with the namespace, e.g. pass `describe_table("ontology.term")`, not
    just `"term"`.
    """
    out = []
    for t in _manifest()["tables"]:
        if namespace and t["schema"] != namespace:
            continue
        d = _describe(t)
        out.append({k: d[k] for k in ("namespace", "table", "comment", "row_count", "business_key")})
    return out


@mcp.tool()
@_logged
def describe_table(table: str) -> dict:
    """Full column-level description of one table, from the release's manifest.

    Call this before writing any SQL against a table you have not used yet —
    the per-column `doc` says what a null means, what authority an id belongs
    to, and which coordinate convention a number uses, none of which the name
    or type tells you. `table` is `"namespace.table"`, e.g.
    `"annotation.ncbi__gene"` (get valid names from list_tables).
    The one mistake to avoid: an identifier column's `identifier_prefix` is a
    Bioregistry prefix, not a hint about which OTHER table to join to — use
    `business_key` for joins.
    """
    return _describe_ref(table)


@mcp.tool()
@_logged
def resolve_release(release: str = "") -> dict:
    """The release id and pinned URL to cite, with the source versions it holds.

    A biocOnIce release is one dated dataset of current rows, `YYYY-MM-DD`;
    tables carry no row history. Leave `release` empty for the release this
    server reads (`latest/` unless BIOCONICE_RELEASE_URL pins one), or pass a
    release id to check it is still online. Returns `release`, `url` (the
    pinned `v<release>/` folder: point BIOCONICE_RELEASE_URL, or a DuckDB
    ATTACH of `<url>/bioconice.duckdb`, at it to reproduce a result) and
    `provenance` (every source, artifact and source_version the release was
    built from).
    The one mistake to avoid: `latest/` moves with every publish; cite the
    pinned `url`, not `latest/`. Old dated folders are pruned; the error then
    names the monthly Hugging Face archive.
    """
    return resolve_release_impl(release=release or None)


@mcp.tool()
@_logged
def query(sql: str, limit: int = DEFAULT_LIMIT) -> dict:
    """Run one read-only SQL query against the public biocOnIce release.

    Only a single `SELECT` or `WITH ... SELECT` is accepted — no
    INSERT/UPDATE/DELETE/ATTACH/COPY/PRAGMA/CREATE and no second statement
    after a `;`; anything else is rejected with a plain-English reason before
    it reaches DuckDB. Results are capped at `limit` rows (default 1000, max
    10000), and going over sets `truncated: true` in the response rather than
    silently dropping rows. Every row is current in this release; there are
    no `valid_from`/`valid_to` columns to filter on.
    The one mistake to avoid: tables are named `b.<namespace>.<table>`
    (e.g. `b.annotation.gene`), not bare `<namespace>.<table>`.
    """
    return query_impl(sql, limit=limit)


@mcp.tool()
@_logged
def recipes() -> list[dict]:
    """A fixed list of worked, runnable SQL for the catalog's common joins.

    Call this when you know roughly what question you want to ask (gene to
    citing literature, dataset by cell type, a species' gene models, one
    taxon's coverage) but not the exact join path — each entry is real SQL
    that has been run against the live data, with the literal ids from a
    verified example (TP53, CL:0000084, UBERON:0000178, taxon 9606) that you
    swap for your own. Prefer adapting one of these over writing a multi-table
    join from scratch, particularly the ontology rollup: `is_a` closures need
    a recursive CTE, and getting that wrong either drops all the descendant
    terms or joins to CL:0000000 (the ontology root) and returns everything.
    """
    return RECIPES


@mcp.resource("bioconice://docs/{table}")
def docs_resource(table: str) -> str:
    """The same description describe_table() returns, as prose, plus the
    ATTACH snippet every client needs before it can run anything."""
    d = _describe_ref(table)
    lines = [
        f"# {d['namespace']}.{d['table']}",
        "",
        d["comment"],
        "",
        f"Business key: {', '.join(d['business_key']) or '(none)'}",
        f"Row count: {d['row_count']}",
        "",
        "## Columns",
    ]
    for c in d["columns"]:
        flags = []
        if c["in_business_key"]:
            flags.append("business key")
        if c["identifier_prefix"]:
            flags.append(f"bioregistry:{c['identifier_prefix']}")
        if c["coordinate_system"]:
            flags.append(c["coordinate_system"])
        flag_str = f" ({', '.join(flags)})" if flags else ""
        lines.append(f"- `{c['name']}` {c['type']}{flag_str}: {c['doc'] or '(no doc)'}")
    lines += [
        "",
        "## Connecting",
        "```sql",
        f"{ATTACH_SQL};",
        "```",
        "No account, no token — anonymous read is public. See README.md's \"Query it\" section "
        "for the canonical TxDb-style join example.",
    ]
    return "\n".join(lines)


def main():
    """Entry point for the `bioconice-mcp` console script: stdio, which is what
    `uvx` / Claude Desktop / Claude Code expect."""
    mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
