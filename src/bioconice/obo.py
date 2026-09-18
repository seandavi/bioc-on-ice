"""OBO Foundry ontologies -> Iceberg: term and relationship tables for the ontology namespace.

Each ontology publishes its own versioned OBO Graphs JSON release (nodes + edges, the JSON
serialization of the OWL graph). No per-ontology code beyond a URL and a licence: `land_raw`
reads the file's `graphs[0].nodes`/`graphs[0].edges` with DuckDB and lands one row per node,
one row per edge, into `raw.obo__<name>` — the same land-whole discipline as every other
source (ADR-0002), so an ontology's imported classes from other namespaces are not filtered
out at land time.

The file is whole, not just the ontology's own namespace: CL's OBO Graphs JSON carries BFO,
RO, IAO and PR classes referenced in logical definitions, plus a handful of bare external
IRIs (e.g. Ensembl gene ids in CL's marker-gene axioms). `ontology.term`'s row count is
therefore the file's own node count exactly (issue #83 acceptance criterion 3), not a
CL-prefixed subset.

`transform` re-parses the `meta` JSON text raw kept verbatim (definition, synonyms, the
deprecated flag, IAO:0100001 "term replaced by", oboInOwl:hasOBONamespace) and derives:

  ontology.term          one row per node, obsolete terms kept with obsolete=true
  ontology.relationship  one row per edge (subject_id, predicate, object_id)

Both are Type 2, merged per ontology (EqualTo("ontology", name)), so one ontology's re-ingest
can never retire another's terms even though they share the same two tables.

IRIs are turned into CURIEs where they follow the OBO PURL convention
(`.../<PREFIX>_<NUMBER>` -> `PREFIX:NUMBER`) and kept verbatim otherwise — see `_curie`.
Edge predicates are `is_a` verbatim, or the CURIE of the relation; a handful of BFO/RO
relations common to every OBO ontology (part_of, has_part, ...) get their familiar short
name instead of a numeric id, exactly as OBO flat format would print them
(`relationship: part_of ...`). Nothing beyond that small, fixed table is invented.

Deliberately NOT using ncbi._land: that helper does an unconditional full-table replace,
which is right for a source with no retained history (NCBI's nightly dumps, iCite's
latest-snapshot-only raw) but wrong here — an ontology release is immutable and raw must
accumulate versions, exactly like raw.ensembl__gtf. So landing uses ensembl._write with an
overwrite_filter scoped to this landing's own release_version, the same pattern the GTF
lander uses for (taxon_id, ensembl_release).
"""

import duckdb
from pyiceberg.expressions import EqualTo

from . import merge
from .ensembl import _write
from .ncbi import _manifest

# name -> (OBO Graphs JSON release URL, licence). "latest" GitHub/PURL redirects, so a
# re-ingest naturally picks up a new release; --url overrides for tests and pinned reruns.
# Kept in sync with schemas._OBO_ONTOLOGIES (names and licences), which cannot import this
# module without a cycle; test_obo.py asserts the two agree. HPO is deliberately absent —
# issue #83 flags its licence for review before it joins this list.
REGISTRY = {
    "cl":      ("https://purl.obolibrary.org/obo/cl.json", "CC-BY-4.0"),
    "uberon":  ("https://purl.obolibrary.org/obo/uberon.json", "CC-BY-3.0"),
    "mondo":   ("https://purl.obolibrary.org/obo/mondo.json", "CC-BY-4.0"),
    "efo":     ("https://github.com/EBISPOT/efo/releases/latest/download/efo.json", "Apache-2.0"),
    "hsapdv":  ("https://purl.obolibrary.org/obo/hsapdv.json", "CC-BY-4.0"),
    "mmusdv":  ("https://purl.obolibrary.org/obo/mmusdv.json", "CC-BY-4.0"),
    "go":      ("https://purl.obolibrary.org/obo/go/go-basic.json", "CC-BY-4.0"),
}

# OBO Graphs JSON is one JSON object per file; DuckDB's 16 MB default max_object_size is
# far below EFO's ~170 MB release. ponytail: bump this if a release ever exceeds it — none
# in REGISTRY is within 3x of it today.
MAX_OBJECT_SIZE = 500_000_000

NAMESPACE_PRED = "http://www.geneontology.org/formats/oboInOwl#hasOBONamespace"
REPLACED_BY_PRED = "http://purl.obolibrary.org/obo/IAO_0100001"

# BFO/RO relations shared across every OBO ontology, given their familiar OBO-format short
# name. Anything not listed here keeps its own CURIE in `predicate` rather than being guessed at.
PREDICATE_LABELS = {
    "BFO:0000050": "part_of",
    "BFO:0000051": "has_part",
    "RO:0002202": "develops_from",
    "RO:0002211": "regulates",
    "RO:0002212": "negatively_regulates",
    "RO:0002213": "positively_regulates",
}


def _curie(col):
    """CURIE from an OBO PURL IRI (`.../PREFIX_NUMBER` -> `PREFIX:NUMBER`); verbatim otherwise."""
    return (f"CASE WHEN regexp_matches({col}, '/[A-Za-z0-9]+_[0-9]+$') "
            f"THEN replace(regexp_extract({col}, '/([A-Za-z0-9]+_[0-9]+)$', 1), '_', ':') "
            f"ELSE {col} END")


def _predicate(col):
    curied = _curie(col)
    whens = " ".join(f"WHEN '{k}' THEN '{v}'" for k, v in PREDICATE_LABELS.items())
    return f"CASE ({curied}) {whens} ELSE ({curied}) END"


def _bpv(pred_iri):
    """First `val` of a node's basicPropertyValues entry asserting `pred_iri`, or NULL."""
    return (f"(SELECT first(v) FROM (SELECT json_extract_string(x, '$.val') AS v, "
            f"json_extract_string(x, '$.pred') AS p FROM "
            f"unnest(coalesce(json_extract(meta, '$.basicPropertyValues')::JSON[], [])) AS t(x)) "
            f"WHERE p = '{pred_iri}')")


def land_raw(cat, release, name, url=None):
    """Phase 1: one ontology's OBO Graphs JSON release, landed whole as (node|edge) rows.

    `url` overrides the registry URL — a local path for offline tests, or a pinned past
    release. The ontology's own version comes from the file's own graphs[0].meta.version,
    never invented, and is what raw is replaced per (ADR-0007's version_method='release_number').
    """
    url = url or REGISTRY[name][0]
    con = duckdb.connect()
    # The graph is read as one JSON value and walked with JSON paths, not as an
    # inferred struct: struct inference makes `meta` a *key*, and a file whose
    # edges (go-basic) or nodes (mmusdv) carry no meta at all then has no such
    # key and the query fails to bind. A JSON path on a missing member is NULL.
    # The arrays and the version are pulled out of the 70 MB document once, in a
    # materialised CTE; extracting them per unnested row re-parses the whole
    # document each time and turned GO into a ten-minute query.
    arrow = con.sql(f"""
        WITH g AS MATERIALIZED (
            SELECT json_extract(graphs, '$[0].nodes')::JSON[] AS nodes,
                   json_extract(graphs, '$[0].edges')::JSON[] AS edges,
                   graphs->>'$[0].meta.version' AS version
            FROM read_json('{url}', columns={{'graphs': 'JSON'}},
                           maximum_object_size={MAX_OBJECT_SIZE}))
        SELECT 'node' AS kind, n->>'id' AS id, n->>'lbl' AS lbl,
               NULL::VARCHAR AS sub, NULL::VARCHAR AS pred, NULL::VARCHAR AS obj,
               (n->'meta')::VARCHAR AS meta, version AS release_version,
               '{release}' AS landed_in
        FROM g, unnest(g.nodes) AS t(n)
      UNION ALL
        SELECT 'edge', NULL, NULL, e->>'sub', e->>'pred', e->>'obj', (e->'meta')::VARCHAR,
               version, '{release}'
        FROM g, unnest(g.edges) AS t(e)
    """).to_arrow_table()
    if not arrow.num_rows:
        raise SystemExit(f"obo {name}: {url} yielded no nodes or edges")
    version = arrow.column("release_version")[0].as_py()
    if not version:
        raise SystemExit(f"obo {name}: {url} carries no graphs[0].meta.version")

    n = _write(cat, f"raw.obo__{name}", arrow, EqualTo("release_version", version))
    _manifest(cat, release, f"obo_{name}", url, n, version=version, method="release_number")
    return version, n


# Invariants the derived rows must satisfy, asserted before any merge runs — the same
# fail-loudly-before-writing discipline as icite.CHECKS.
CHECKS = {
    "term_id is present": "SELECT count(*) FROM term WHERE term_id IS NULL",
    "term_id is unique":  "SELECT count(*) - count(DISTINCT term_id) FROM term",
    "relationship endpoints and predicate are present":
        "SELECT count(*) FROM rel WHERE subject_id IS NULL OR predicate IS NULL OR object_id IS NULL",
}


def _check(con):
    failed = {k: con.sql(v).fetchone()[0] for k, v in CHECKS.items()}
    failed = {k: v for k, v in failed.items() if v}
    if failed:
        raise ValueError("obo: derived rows violate " + "; ".join(f"{k} ({v:,} rows)" for k, v in failed.items()))


def transform(cat, release, name, version):
    """Phase 2: ontology.term (by node) and ontology.relationship (by edge), for one ontology.

    Scoped to `version`'s rows in raw.obo__<name>: raw accumulates every landed release (like
    raw.ensembl__gtf), so an unscoped read would derive from every version ever landed at once.
    """
    con = duckdb.connect()
    con.register("raw", cat.load_table(f"raw.obo__{name}").scan(
        row_filter=EqualTo("release_version", version)).to_arrow())

    con.execute(f"""
        CREATE TABLE term AS
        SELECT '{name}' AS ontology,
               {_curie('id')} AS term_id,
               lbl AS name,
               json_extract_string(meta, '$.definition.val') AS definition,
               {_bpv(NAMESPACE_PRED)} AS namespace,
               list_aggregate(list_transform(coalesce(json_extract(meta, '$.synonyms')::JSON[], []),
                                              x -> json_extract_string(x, '$.val')),
                              'string_agg', '|') AS synonyms,
               coalesce(json_extract_string(meta, '$.deprecated'), 'false') = 'true' AS obsolete,
               {_curie(_bpv(REPLACED_BY_PRED))} AS replaced_by
        FROM raw WHERE kind = 'node'
    """)
    con.execute(f"""
        CREATE TABLE rel AS
        SELECT '{name}' AS ontology, {_curie('sub')} AS subject_id,
               {_predicate('pred')} AS predicate, {_curie('obj')} AS object_id
        FROM raw WHERE kind = 'edge'
    """)
    _check(con)
    term = con.sql("SELECT * FROM term").to_arrow_table()
    rel = con.sql("SELECT * FROM rel").to_arrow_table()

    scope = EqualTo("ontology", name)
    return {
        "ontology.term": merge.merge(cat, "ontology.term", term, release, scope),
        "ontology.relationship": merge.merge(cat, "ontology.relationship", rel, release, scope),
    }


def ingest(cat, release, name, url=None):
    version, n = land_raw(cat, release, name, url)
    return {f"raw.obo__{name} [{version}]": n, **transform(cat, release, name, version)}
