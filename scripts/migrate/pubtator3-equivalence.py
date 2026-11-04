"""PubTator3 equivalence (#183 H2): each table's lake release SELECT against its current
Iceberg rows and a published manifest, with the row comparison cut to a PMID range: the
tables are 8M-478M rows. Read-only on both sides.

  uv run --project <bioc-on-ice worktree> python <this file> <manifest.json> [<table> ...]

Run with the env of the lake to compare (scratch: the ADR-0015 local-backend env;
production: CU_OPENALEX_LAKE_BACKEND=postgres). Per table, one JSON object on stdout:
(a) columns of the lake SELECT == Iceberg's and the manifest's (name, type, order);
(b) row counts, lake SELECT / Iceberg / manifest; (c) EXCEPT ALL both ways on pmid in
[LO, HI]. 3 sample rows of each difference go to stderr. Exits 1 if any check fails.
"""

import json
import sys
from pathlib import Path

from cdsci.lake.connect import lake_connect

from bioconice import publish, pubtator3

LO, HI = 35_000_000, 35_099_999
ref = {f"{t['schema']}.{t['table']}": t for t in json.loads(Path(sys.argv[1]).read_text())["tables"]}
con = lake_connect(read_only=True)
con.execute(publish.ATTACH_LIVE)
results = []
for name in sys.argv[2:] or pubtator3.RELEASE:
    new, old = pubtator3.RELEASE[name], publish._current(name, publish.DECLARED[name])

    def n(sql):
        return con.sql(sql).fetchone()[0]

    def cols(sql):
        return [list(r[:2]) for r in con.sql(f"DESCRIBE {sql}").fetchall()]

    r = {"table": name,
         "columns_eq_iceberg": cols(new) == cols(old),
         "columns_eq_manifest": cols(new) == [[c["name"], c["type"]] for c in ref[name]["columns"]],
         "rows_new": n(f"SELECT count(*) FROM ({new})"),
         "rows_iceberg": n(f"SELECT count(*) FROM ({old})"),
         "rows_manifest": ref[name]["row_count"]}
    ok = (r["columns_eq_iceberg"] and r["columns_eq_manifest"]
          and r["rows_new"] == r["rows_iceberg"] == r["rows_manifest"])
    where = f"WHERE TRY_CAST(pmid AS BIGINT) BETWEEN {LO} AND {HI}"
    con.execute(f"CREATE OR REPLACE TEMP TABLE new AS SELECT * FROM ({new}) {where}")
    con.execute(f"CREATE OR REPLACE TEMP TABLE old AS SELECT * FROM ({old}) {where}")
    for side, a, b in (("new", "new", "old"), ("iceberg", "old", "new")):
        diff = f"SELECT * FROM {a} EXCEPT ALL SELECT * FROM {b}"
        r[f"only_{side}_pmid_range"] = k = n(f"SELECT count(*) FROM ({diff})")
        ok = ok and not k
        if k:
            print(f"{name} sample only in {side}:", con.sql(f"{diff} LIMIT 3").fetchall(),
                  file=sys.stderr)
    r["rows_pmid_range"] = n("SELECT count(*) FROM new")
    ok = ok and r["rows_pmid_range"] > 0  # an empty range proves nothing
    r["pass"] = ok
    results.append(r)
    print(json.dumps(r), file=sys.stderr, flush=True)
print(json.dumps(results, indent=1))
sys.exit(0 if all(r["pass"] for r in results) else 1)
