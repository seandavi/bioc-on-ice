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

Two transports, one implementation: `bioconice-mcp` runs stdio by default
(for `uvx`/Claude Desktop/Claude Code); set `BIOCONICE_MCP_TRANSPORT=streamable-http`
(what `mcp/` runs in the Cloudflare Container) to serve the same tools over
HTTP instead.
"""

import argparse
import functools
import json
import logging
import os
import re
import sys
import time
import urllib.error
import urllib.request

import duckdb
from mcp.server.fastmcp import FastMCP
from mcp.server.transport_security import TransportSecuritySettings
from starlette.requests import Request
from starlette.responses import JSONResponse

from .schemas import TABLES as DECLARED

# A release folder: `latest/` by default, or a dated `v{YYYY-MM-DD}/` pin.
RELEASE_URL = os.environ.get("BIOCONICE_RELEASE_URL", "https://data.bioconice.cancerdatasci.org/latest").rstrip("/")
ATTACH_SQL = f"ATTACH '{RELEASE_URL}/bioconice.duckdb' AS b (READ_ONLY)"
MAX_LIMIT = 10_000
DEFAULT_LIMIT = 1000

# The streamable-HTTP transport refuses requests whose Host it was not told about
# (DNS-rebinding protection, HTTP 421). Behind Traefik the Host is the public
# hostname, so it must be listed; BIOCONICE_MCP_ALLOWED_HOSTS adds more.
ALLOWED_HOSTS = ["localhost:*", "127.0.0.1:*", "bioconice-mcp.cancerdatasci.org",
                 *filter(None, os.environ.get("BIOCONICE_MCP_ALLOWED_HOSTS", "").split(","))]

mcp = FastMCP(

    "bioconice",
    instructions=(
        "Bioconductor gene/transcript/exon annotation, NCBI Gene, iCite citations, "
        "OBO ontologies and dataset catalogs, served anonymously and read-only from "
        "the public biocOnIce release. Call list_tables() first to see what exists, then describe_table() on "
        "the ones you plan to join before writing SQL — the column docs say what a null "
        "means and which convention a coordinate uses, which you cannot guess correctly. "
        "The release holds current rows only (`valid_to` is always NULL), so it cannot "
        "answer 'as of an earlier release' questions; that history lives in the frozen "
        "Iceberg archive, not here."
    ),
    transport_security=TransportSecuritySettings(
        allowed_hosts=ALLOWED_HOSTS,
        allowed_origins=[f"https://{h}" for h in ALLOWED_HOSTS if ":*" not in h] + ["http://localhost:*"])
)


# --------------------------------------------------------------------------
# The release's manifest.json — plain HTTP, read fresh each call: `latest/`
# moves with every publish, and the file is a few hundred KB.
# --------------------------------------------------------------------------

def _manifest():
    url = f"{RELEASE_URL}/manifest.json"
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
# resolve_release()'s core: provenance.release is small, so read it fresh
# each call rather than caching a release manifest that could go stale.
# --------------------------------------------------------------------------

_SHORTCUT_SOURCE = {"ensembl": "ensembl", "icite": "icite", "ncbi_date": "ncbi_gene"}


def _point_in_time_predicate(release):
    return f"valid_from <= '{release}' AND (valid_to IS NULL OR valid_to > '{release}')"


def _provenance_rows():
    con = _duck()
    # ponytail: the first release predates provenance.release.artifact (#176); NULL until it lands.
    cols = {r[0] for r in con.execute("DESCRIBE b.provenance.release").fetchall()}
    artifact = "artifact" if "artifact" in cols else "NULL AS artifact"
    return con.execute(
        f"SELECT release, source, source_version, version_method, {artifact} "
        "FROM b.provenance.release ORDER BY ALL"
    ).fetchall()


def resolve_release_impl(release=None, ensembl=None, icite=None, ncbi_date=None,
                          source=None, source_version=None, rows=None):
    """`rows` is injectable for offline tests; live callers leave it None."""
    shortcuts = [(k, v) for k, v in (("ensembl", ensembl), ("icite", icite), ("ncbi_date", ncbi_date))
                 if v is not None]
    if source is None and shortcuts:
        if len(shortcuts) > 1:
            raise ValueError("pass only one of ensembl=/icite=/ncbi_date=/source=")
        name, value = shortcuts[0]
        source = _SHORTCUT_SOURCE[name]
        source_version = str(value)

    rows = _provenance_rows() if rows is None else rows

    if release is not None:
        known_releases = sorted({r[0] for r in rows})
        if release not in known_releases:
            raise ValueError(f"unknown release {release!r}; known releases: {known_releases}")
        matches = [r for r in rows if r[0] == release]
        return {
            "release": release,
            "predicate": _point_in_time_predicate(release),
            # One row per (source, artifact): a source read file by file, or
            # species by species (ensembl), has several at one release.
            "provenance": [
                {"source": r[1], "artifact": r[4], "source_version": r[2], "version_method": r[3]}
                for r in matches
            ],
        }

    if source is None:
        raise ValueError(
            "pass release=<biocOnIce release>, one of ensembl=/icite=/ncbi_date=, "
            "or source=+source_version="
        )

    matches = [r for r in rows if r[1] == source and (source_version is None or r[2] == str(source_version))]
    if not matches:
        known_versions = sorted({r[2] for r in rows if r[1] == source})
        if not known_versions:
            known_sources = sorted({r[1] for r in rows})
            raise ValueError(f"unknown source {source!r}; known sources: {known_sources}")
        raise ValueError(f"no release has {source}={source_version!r}; known {source} versions: {known_versions}")

    best = max(matches, key=lambda r: r[0])
    return {
        "release": best[0],
        "predicate": _point_in_time_predicate(best[0]),
        "provenance": {"source": best[1], "source_version": best[2], "version_method": best[3]},
    }


# --------------------------------------------------------------------------
# query()'s execution: wrap the caller's query once. This is what gives a
# universal row cap and truncated flag to ANY query, and it is deliberately
# the whole of what current=true does — it filters the result on
# `valid_to IS NULL`, it does not rewrite the inner query table-by-table
# (that needs a real SQL parser, which is more machinery than a read-only
# demo tool earns; see the tool description below for the one mistake this
# causes).
# --------------------------------------------------------------------------

def query_impl(sql, limit=DEFAULT_LIMIT, current=False):
    inner = guard_sql(sql)
    if not isinstance(limit, int) or not (0 < limit <= MAX_LIMIT):
        raise ValueError(f"limit must be an integer between 1 and {MAX_LIMIT}")
    con = _duck()
    where = " WHERE valid_to IS NULL" if current else ""
    wrapped = f"SELECT * FROM ({inner}) AS _bioconice_q{where} LIMIT {limit + 1}"
    try:
        cur = con.execute(wrapped)
    except duckdb.Error as e:
        msg = str(e)
        if current and "valid_to" in msg.lower():
            raise ValueError(
                "current=true filters the result on `valid_to IS NULL` — it does not rewrite "
                "your query, so `valid_to` must be one of the columns your SELECT list returns."
            ) from e
        raise ValueError(msg) from e
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
            "  WHERE gene_id = '7157' AND taxon_id = 9606 AND valid_to IS NULL  -- TP53, human\n"
            ")\n"
            "SELECT COUNT(*) AS citing_edges\n"
            "FROM b.annotation.icite__citation c\n"
            "JOIN gene_papers g ON c.cited_pmid = g.pubmed_id\n"
            "WHERE c.valid_to IS NULL;"
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
            "  WHERE r.predicate = 'is_a' AND r.ontology = 'cl' AND r.valid_to IS NULL\n"
            ")\n"
            "SELECT DISTINCT rr.resource_id\n"
            "FROM b.resource.resource_relationship rr\n"
            "JOIN descendants d ON rr.target_id = d.term_id\n"
            "-- resource_relationship.resource_id is the dataset VERSION id, not dataset_id\n"
            "JOIN b.resource.cellxgene__dataset ds ON ds.dataset_version_id = rr.resource_id AND ds.valid_to IS NULL\n"
            "WHERE rr.relationship = 'has_cell_type' AND rr.valid_to IS NULL\n"
            "  AND ds.taxon_id = 9606\n"
            "  AND EXISTS (\n"
            "    SELECT 1 FROM b.resource.resource_relationship t\n"
            "    WHERE t.resource_id = rr.resource_id AND t.relationship = 'has_tissue'\n"
            "      AND t.target_id = 'UBERON:0000178' AND t.valid_to IS NULL  -- blood\n"
            "  );"
        ),
    },
    {
        "name": "gene_models_at_release",
        "description": (
            "A species' gene/transcript/exon models as of a specific biocOnIce release — resolve "
            "the release with resolve_release() first, then splice its predicate in for each table. "
            "The public release holds current rows only, so this returns the models still "
            "current that already existed then, not ones retired since."
        ),
        "sql": (
            "SELECT g.gene_id, g.symbol, t.transcript_id, e.rank, e.start, e.end\n"
            "FROM b.annotation.gene g\n"
            "JOIN b.annotation.transcript t USING (gene_id, taxon_id, source)\n"
            "JOIN b.annotation.exon e USING (transcript_id, taxon_id, source)\n"
            "WHERE g.taxon_id = 9606 AND g.source = 'ENSEMBL'\n"
            "  AND g.valid_from <= '2026.09' AND (g.valid_to IS NULL OR g.valid_to > '2026.09')\n"
            "  AND t.valid_from <= '2026.09' AND (t.valid_to IS NULL OR t.valid_to > '2026.09')\n"
            "  AND e.valid_from <= '2026.09' AND (e.valid_to IS NULL OR e.valid_to > '2026.09')\n"
            "ORDER BY g.gene_id, t.transcript_id, e.rank;"
        ),
    },
    {
        "name": "taxon_coverage",
        "description": (
            "How much of the catalog exists for one taxon, current rows only — a quick sanity "
            "check before building a bigger query for a species you have not used here yet."
        ),
        "sql": (
            "SELECT 'annotation.ncbi__gene' AS table_name, COUNT(*) AS rows\n"
            "FROM b.annotation.ncbi__gene WHERE taxon_id = 9606 AND valid_to IS NULL\n"
            "UNION ALL\n"
            "SELECT 'annotation.gene', COUNT(*) FROM b.annotation.gene\n"
            "WHERE taxon_id = 9606 AND valid_to IS NULL\n"
            "UNION ALL\n"
            "SELECT 'annotation.ncbi__gene_pubmed', COUNT(*) FROM b.annotation.ncbi__gene_pubmed\n"
            "WHERE taxon_id = 9606 AND valid_to IS NULL;"
        ),
    },
]


# --------------------------------------------------------------------------
# Tool-call log: one JSON line per call on stdout — tool, arguments (truncated),
# milliseconds, rows returned, error. Traefik logs the request; this logs what
# the request meant. Read with `docker logs bioconice-mcp`, or ship it.
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
def resolve_release(
    release: str = "",
    ensembl: str = "",
    icite: str = "",
    ncbi_date: str = "",
    source: str = "",
    source_version: str = "",
) -> dict:
    """Resolve a biocOnIce release id and its point-in-time SQL predicate.

    Every versioned table's rows carry `valid_from`/`valid_to` in release
    coordinates (e.g. '2026.09'), never in wall-clock time; this tool is how
    you turn "Ensembl 116" or "the icite 2026-08 snapshot" into both the
    release id AND the exact `valid_from <= R AND (valid_to IS NULL OR
    valid_to > R)` predicate to paste into a `query()` WHERE clause. Pass
    exactly one of: `release` (a biocOnIce id to validate and get the
    predicate for), one of `ensembl=`/`icite=`/`ncbi_date=` (shortcuts for the
    common sources), or `source=`+`source_version=` for any other row of
    `provenance.release` (obo, cellxgene, bugsigdb, ...). `source` is the
    provider; which ontology, species or file a row is about is that table's
    `artifact` column, which resolving by `release` lists. An unknown
    version errors back with the list of ones that exist.
    The one mistake to avoid: the public release holds current rows only
    (`valid_to` is always NULL), so the predicate for a PAST release returns
    only the rows still current today, not the rows as they stood then; true
    point-in-time history lives in the frozen Iceberg archive (ADR-0013/0014).
    """
    return resolve_release_impl(
        release=release or None,
        ensembl=ensembl or None,
        icite=icite or None,
        ncbi_date=ncbi_date or None,
        source=source or None,
        source_version=source_version or None,
    )


@mcp.tool()
@_logged
def query(sql: str, limit: int = DEFAULT_LIMIT, current: bool = False) -> dict:
    """Run one read-only SQL query against the public biocOnIce release.

    Only a single `SELECT` or `WITH ... SELECT` is accepted — no
    INSERT/UPDATE/DELETE/ATTACH/COPY/PRAGMA/CREATE and no second statement
    after a `;`; anything else is rejected with a plain-English reason before
    it reaches DuckDB. Results are capped at `limit` rows (default 1000, max
    10000), and going over sets `truncated: true` in the response rather than
    silently dropping rows. Tables carry `valid_from`/`valid_to`
    (see resolve_release); pass `current=true` to have this tool filter the
    result on `valid_to IS NULL` for you, but only when your SELECT list
    already includes `valid_to` — this does not parse or rewrite your query,
    it filters the OUTPUT, so `valid_to` has to be one of the columns you
    asked for.
    The one mistake to avoid: tables are named `b.<namespace>.<table>`
    (e.g. `b.annotation.gene`); the release holds current rows only, so a
    `valid_to IS NULL` filter is harmless but changes nothing.
    """
    return query_impl(sql, limit=limit, current=current)


@mcp.tool()
@_logged
def recipes() -> list[dict]:
    """A fixed list of worked, runnable SQL for the catalog's common joins.

    Call this when you know roughly what question you want to ask (gene to
    citing literature, dataset by cell type, gene models at a release, one
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
        f"Business key: {', '.join(d['business_key']) or '(none — this table has no versioned rows)'}",
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


@mcp.custom_route("/health/live", methods=["GET"])
async def live(request: Request) -> JSONResponse:
    """Liveness: the process is up and serving. Never touches the lake, so an
    uptime check on this tells 'our container died' apart from 'the release is unreachable'."""
    return JSONResponse({"status": "ok"})


@mcp.custom_route("/health", methods=["GET"])
@mcp.custom_route("/health/ready", methods=["GET"])
async def health(request: Request) -> JSONResponse:
    """Readiness: the release is reachable and DuckDB can still resolve a release.
    The compose healthcheck uses this; /health is kept as its alias."""
    try:
        release = max(r[0] for r in _provenance_rows())
    except Exception as e:
        return JSONResponse({"status": "error", "detail": str(e)}, status_code=503)
    return JSONResponse({"status": "ok", "release": release})


def main():
    """Entry point for the `bioconice-mcp` console script.

    Defaults to stdio (what `uvx` / Claude Desktop / Claude Code expect).
    `--http` serves the same tools over streamable HTTP instead, plus
    `GET /health` — that's what mcp/Dockerfile's container runs.
    """
    parser = argparse.ArgumentParser(prog="bioconice-mcp")
    parser.add_argument("--http", action="store_true",
                         help="serve streamable HTTP (with /health) instead of stdio")
    parser.add_argument("--host", default="0.0.0.0")
    parser.add_argument("--port", type=int, default=int(os.environ.get("PORT", "8000")))
    args = parser.parse_args()

    if args.http:
        # One JSON line per tool call on stdout, alongside uvicorn's access lines.
        logging.basicConfig(level=logging.INFO, format="%(message)s", stream=sys.stdout, force=True)
        mcp.settings.host = args.host
        mcp.settings.port = args.port
        mcp.run(transport="streamable-http")
    else:
        mcp.run(transport="stdio")


if __name__ == "__main__":
    main()
