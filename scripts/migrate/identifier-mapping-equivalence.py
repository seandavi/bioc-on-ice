"""identifier_mapping equivalence (#183 G2): the lake union against Iceberg's current rows
and a published manifest, per writer. Read-only on both sides.

  uv run --project <bioc-on-ice worktree> python <this file> <manifest.json> [<taxon>]

Run with CU_OPENALEX_LAKE_BACKEND=postgres: every writer is already in the shared lake.
One JSON object on stdout: (a) columns of the union == Iceberg's and the manifest's;
(b) rows per source, union / Iceberg, whole table; (c) per source, EXCEPT ALL both ways on
one taxon (default 9606), Iceberg's 'Ensembl' read as 'ENSEMBL' and its RNAcentral ENA
rows set aside and counted (ADR-0015 c); (d) the gate's key, unique and not NULL, on that
taxon. 3 sample rows of each difference go to stderr. Exits 1 if (a) or (d) fails: the
row differences are expected (the lake is newer) and explained in
identifier-mapping-supervisor.md.
"""

import json
import sys
from pathlib import Path

from cdsci.lake.connect import lake_connect

from bioconice import publish

NAME = "annotation.identifier_mapping"
KEY = "source_namespace, source_id, target_namespace, target_id, taxon_id, source"
taxon = int(sys.argv[2]) if len(sys.argv) > 2 else 9606
ref = next(t for t in json.loads(Path(sys.argv[1]).read_text())["tables"]
           if f"{t['schema']}.{t['table']}" == NAME)
new = publish.FROM_LAKE[NAME]
old = (f"SELECT * REPLACE (CASE source WHEN 'Ensembl' THEN 'ENSEMBL' ELSE source END AS source) "
       f"FROM ({publish._current(NAME, publish.DECLARED[NAME])})")
ENA = "source = 'RNACENTRAL' AND target_namespace = 'ENA'"
con = lake_connect(read_only=True)
con.execute(publish.ATTACH_LIVE)


def n(sql):
    return con.sql(sql).fetchone()[0]


def cols(sql):
    return [list(r[:2]) for r in con.sql(f"DESCRIBE {sql}").fetchall()]


def per_source(sql):
    return dict(con.sql(f"SELECT source, count(*) FROM ({sql}) GROUP BY 1 ORDER BY 1").fetchall())


r = {"table": NAME, "taxon": taxon,
     "columns_eq_iceberg": cols(new) == cols(old),
     "columns_eq_manifest": cols(new) == [[c["name"], c["type"]] for c in ref["columns"]],
     "rows_manifest": ref["row_count"]}
print(json.dumps(r), file=sys.stderr, flush=True)
r["rows_new"] = per_source(new)
print(json.dumps({"rows_new": r["rows_new"]}), file=sys.stderr, flush=True)
r["rows_iceberg"] = per_source(old)
r["rows_iceberg_ena"] = n(f"SELECT count(*) FROM ({old}) WHERE {ENA}")
print(json.dumps({k: r[k] for k in ("rows_iceberg", "rows_iceberg_ena")}), file=sys.stderr,
      flush=True)

con.execute(f"CREATE OR REPLACE TEMP TABLE new AS SELECT * FROM ({new}) WHERE taxon_id = {taxon}")
con.execute(f"CREATE OR REPLACE TEMP TABLE old_all AS SELECT * FROM ({old}) WHERE taxon_id = {taxon}")
con.execute(f"CREATE OR REPLACE TEMP TABLE old AS SELECT * FROM old_all WHERE NOT ({ENA})")
r["slice_iceberg_ena"] = n(f"SELECT count(*) FROM old_all WHERE {ENA}")
r["slice"] = {}
for source in sorted(set(per_source("SELECT * FROM new")) | set(per_source("SELECT * FROM old"))):
    s = {"rows_new": n(f"SELECT count(*) FROM new WHERE source = '{source}'"),
         "rows_iceberg": n(f"SELECT count(*) FROM old WHERE source = '{source}'")}
    for side, a, b in (("new", "new", "old"), ("iceberg", "old", "new")):
        diff = (f"SELECT * FROM {a} WHERE source = '{source}' EXCEPT ALL "
                f"SELECT * FROM {b} WHERE source = '{source}'")
        s[f"only_{side}"] = k = n(f"SELECT count(*) FROM ({diff})")
        if k:
            print(f"{source} sample only in {side}:", con.sql(f"{diff} LIMIT 3").fetchall(),
                  file=sys.stderr)
    r["slice"][source] = s
r["slice_key_duplicated"] = n(f"SELECT count(*) FROM (SELECT 1 FROM new GROUP BY {KEY} "
                              "HAVING count(*) > 1)")
r["slice_key_null"] = n("SELECT count(*) FROM new WHERE " + " OR ".join(
    f"{k} IS NULL" for k in KEY.split(", ")))
r["pass"] = (r["columns_eq_iceberg"] and r["columns_eq_manifest"]
             and not r["slice_key_duplicated"] and not r["slice_key_null"])
print(json.dumps(r, indent=1))
sys.exit(0 if r["pass"] else 1)
