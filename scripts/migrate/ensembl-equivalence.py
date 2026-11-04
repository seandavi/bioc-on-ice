"""Ensembl equivalence (#183 G1): each table's lake release SELECT against its current
Iceberg rows and a published manifest, with the row comparison cut to two taxa: the whole
tables are 334M (gtf) and 143M (exon) rows. Read-only on both sides.

  CU_OPENALEX_LAKE_BACKEND=postgres \
  uv run --project <bioc-on-ice worktree> python <this file> <manifest.json> [<table> ...]

Per table, one JSON object on stdout: (a) columns of the lake SELECT == Iceberg's and the
manifest's (name, type, order); (b) row counts, lake SELECT / Iceberg / manifest; (c) EXCEPT
ALL both ways on taxon 9606 and on 7962 (carp: three assemblies in the lake, one
published). 3 sample rows of each difference go to stderr. Exits 1 if any check fails.
Expected: reference.genome's 13 assemblies with no accession (turTru1, ...) are '' in
Iceberg and the assembly name in the lake SELECT, so its full count matches but those rows
differ; neither taxon below is one of them.
"""

import json
import sys
from pathlib import Path

from cdsci.lake.connect import lake_connect

from bioconice import ensembl, publish

TAXA = (9606, 7962)
ref = {f"{t['schema']}.{t['table']}": t for t in json.loads(Path(sys.argv[1]).read_text())["tables"]}
con = lake_connect(read_only=True)
con.execute(publish.ATTACH_LIVE)
results = []
for name in sys.argv[2:] or ensembl.RELEASE:
    new, old = ensembl.RELEASE[name], publish._current(name, publish.DECLARED[name])

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
    for taxon in TAXA:
        con.execute(f"CREATE OR REPLACE TEMP TABLE new AS SELECT * FROM ({new}) WHERE taxon_id = {taxon}")
        con.execute(f"CREATE OR REPLACE TEMP TABLE old AS SELECT * FROM ({old}) WHERE taxon_id = {taxon}")
        for side, a, b in (("new", "new", "old"), ("iceberg", "old", "new")):
            diff = f"SELECT * FROM {a} EXCEPT ALL SELECT * FROM {b}"
            r[f"only_{side}_{taxon}"] = k = n(f"SELECT count(*) FROM ({diff})")
            ok = ok and not k
            if k:
                print(f"{name} taxon {taxon} sample only in {side}:",
                      con.sql(f"{diff} LIMIT 3").fetchall(), file=sys.stderr)
        r[f"rows_{taxon}"] = n("SELECT count(*) FROM new")
    r["pass"] = ok
    results.append(r)
    print(json.dumps(r), file=sys.stderr, flush=True)
print(json.dumps(results, indent=1))
sys.exit(0 if all(r["pass"] for r in results) else 1)
