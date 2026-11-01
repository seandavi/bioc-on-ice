"""Migration equivalence (#183): each migrated table's publish SELECT, over the lake the
environment points at, against its current Iceberg rows and a published manifest.
Read-only on both sides. From migration-log-step7.md, step 4.

Run from the scratch dir with the ADR-0015 local-backend env set:
  uv run --project <bioc-on-ice worktree> python <this file> <manifest.json> <table> ...

Prints 3 sample rows of each difference to stderr, then one JSON list on stdout, one
object per table. A table passes when `pass` is true: same columns and types as Iceberg
and the manifest, key unique and not null, and no row only on one side (EXCEPT ALL).
Exits 1 if any table fails.
"""

import json
import sys
from pathlib import Path

from cdsci.lake.connect import lake_connect

from bioconice import gate, publish

# curl -o the manifest; the release Worker answers Python-urllib's User-Agent with 403.
ref = {f"{t['schema']}.{t['table']}": t for t in json.loads(Path(sys.argv[1]).read_text())["tables"]}
selects = {f"{s}.{t}": q for s, t, q in publish.TABLES}
contracts = {n: c for d in gate.CONTRACTS for n, c in d.tables.items()}

con = lake_connect(read_only=True)
con.execute(publish.ATTACH_LIVE)
results = []
for name in sys.argv[2:]:
    old = publish._current(name, publish.DECLARED[name]) if name in publish.DECLARED \
        else f"SELECT * FROM bioc.{name}"
    con.execute(f"CREATE OR REPLACE TEMP TABLE new AS {selects[name]}")
    con.execute(f"CREATE OR REPLACE TEMP TABLE old AS {old}")
    cols_new = [list(r[:2]) for r in con.sql("DESCRIBE new").fetchall()]
    cols_old = [list(r[:2]) for r in con.sql("DESCRIBE old").fetchall()]
    cols_ref = [[c["name"], c["type"]] for c in ref[name]["columns"]]
    key = list(contracts[name].primary_key)

    def n(sql):
        return con.sql(sql).fetchone()[0]

    r = {
        "table": name, "key": key,
        "rows_new": n("SELECT count(*) FROM new"), "rows_iceberg": n("SELECT count(*) FROM old"),
        "rows_manifest": ref[name]["row_count"],
        "columns_eq_iceberg": cols_new == cols_old, "columns_eq_manifest": cols_new == cols_ref,
        "key_duplicated": n(f"SELECT count(*) FROM (SELECT 1 FROM new GROUP BY {', '.join(key)} "
                            "HAVING count(*) > 1)"),
        "key_null": n("SELECT count(*) FROM new WHERE " + " OR ".join(f"{k} IS NULL" for k in key)),
        "only_new": n("SELECT count(*) FROM (SELECT * FROM new EXCEPT ALL SELECT * FROM old)"),
        "only_iceberg": n("SELECT count(*) FROM (SELECT * FROM old EXCEPT ALL SELECT * FROM new)"),
    }
    r["pass"] = (r["columns_eq_iceberg"] and r["columns_eq_manifest"] and not r["key_duplicated"]
                 and not r["key_null"] and not r["only_new"] and not r["only_iceberg"])
    if cols_new != cols_ref:
        r["column_diff_vs_manifest"] = sorted({tuple(c) for c in cols_new}
                                             ^ {tuple(c) for c in cols_ref})
    for side, a, b in (("new", "new", "old"), ("iceberg", "old", "new")):
        if r[f"only_{side}"]:
            print(f"{name} sample only in {side}:", con.sql(
                f"SELECT * FROM (SELECT * FROM {a} EXCEPT ALL SELECT * FROM {b}) "
                f"ORDER BY {', '.join(key)} LIMIT 3").fetchall(), file=sys.stderr)
    results.append(r)
print(json.dumps(results, indent=1))
sys.exit(0 if all(r["pass"] for r in results) else 1)
