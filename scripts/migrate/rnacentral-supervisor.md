# RNAcentral (#183 H3): supervisor commands

The long runs the migration worker did not do: the scratch load of the full file, its
re-run, the equivalence and the partial gate. Run them detached, in this order, from
one shell. Worktrees: `~/Documents/git/worktrees/migrate-rnacentral/{cdsci-lake,bioc-on-ice}`
on branch `migrate/rnacentral`. Nothing below writes outside `$D`; both sides are read
only (ADR-0015 scratch recipe, `CU_OPENALEX_LAKE_BACKEND=local`).

Expected, from the v2026-11-03 manifest: `raw.rnacentral__id_mapping` 264,333,345 rows
and `annotation.rnacentral__rna` 2,753,423 (9606: 2,004,721; 10090: 748,702), both at
RNAcentral release **27**. `current_release` is 28 (2026-11-03), so `--version 27` is
required: without it the load fetches 28 and nothing below matches.

Measured by the worker on a 100,000-line prefix of release 27 (2026-11-03): the load
took 1.4 s, the re-run 1.1 s and reported idempotent, no `\r` reached a cell (the file
is CRLF), and gene_name was non-NULL on 8,377 rows. The whole-file field-count check runs
at 1.25 s per 2M lines, so about 3 min for 264M. Nothing larger than the prefix was run:
the full-load time, peak memory and spill size are unmeasured. Each of the 20 batches
re-reads the 2.66 GB gzip.

## 0. Scratch env

```bash
C=~/Documents/git/worktrees/migrate-rnacentral/cdsci-lake
W=~/Documents/git/worktrees/migrate-rnacentral/bioc-on-ice
D=$(mktemp -d /data/davsean/tmp/bioconice-scratch-lake.XXXXXX)
T=$D/out; mkdir -p "$T"
cd "$D"
export CU_OPENALEX_LAKE_BACKEND=local
export CU_OPENALEX_STORAGE_BASE_URI="file://$D"
export CU_OPENALEX_LAKE_CATALOG="$D/catalog.ducklake"
export CU_OPENALEX_LAKE_DATA_PREFIX=data
export CU_OPENALEX_DUCKDB_TEMP_DIRECTORY="$D/duckdb_tmp"
curl -s -o "$T/manifest-v2026-11-03.json" \
  https://data.bioconice.cancerdatasci.org/v2026-11-03/manifest.json
```

## 1. Scratch download and load at the Iceberg version (27)

```bash
( time timeout 6h uv run --project "$C" python -m cdsci.lake.sources.rnacentral run --version 27 ) \
  > "$T/load1.log" 2>&1
tail -3 "$T/load1.log"   # want: success, rows=264333345
```

## 2. Idempotence re-run

The download is cached under `$D/lake/raw/rnacentral/27/`, so this re-reads the local file only.

```bash
( time timeout 6h uv run --project "$C" python -m cdsci.lake.sources.rnacentral run --version 27 ) \
  > "$T/load2.log" 2>&1
grep -E "idempotent|success" "$T/load2.log" | tail -2   # want: idempotent, snapshot N→N
```

## 3. Equivalence

`annotation.rnacentral__rna` (2.75M rows) goes through the generic script: columns
against Iceberg and the manifest, the key unique and not NULL, and EXCEPT ALL both ways
on every row.

```bash
uv run --project "$W" python "$W/scripts/migrate/equivalence.py" \
  "$T/manifest-v2026-11-03.json" annotation.rnacentral__rna > "$T/equivalence-rna.json"
```

`raw.rnacentral__id_mapping` is 264M rows, so its columns and counts are checked in full
and EXCEPT ALL runs both ways on taxa 9606 and 10090: the two taxa `rnacentral__rna` is
derived for, every database including ENA.

```bash
uv run --project "$W" python - "$T/manifest-v2026-11-03.json" > "$T/equivalence-raw.json" <<'EOF'
import json, sys
from pathlib import Path
from cdsci.lake.connect import lake_connect
from bioconice import publish, rnacentral

name = "raw.rnacentral__id_mapping"
ref = {f"{t['schema']}.{t['table']}": t for t in json.loads(Path(sys.argv[1]).read_text())["tables"]}
new, old = rnacentral.RELEASE[name], publish._current(name, publish.DECLARED[name])
con = lake_connect(read_only=True)
con.execute(publish.ATTACH_LIVE)
n = lambda sql: con.sql(sql).fetchone()[0]
cols = lambda sql: [list(r[:2]) for r in con.sql(f"DESCRIBE {sql}").fetchall()]
r = {"table": name, "columns_eq_iceberg": cols(new) == cols(old),
     "columns_eq_manifest": cols(new) == [[c["name"], c["type"]] for c in ref[name]["columns"]],
     "rows_new": n(f"SELECT count(*) FROM ({new})"), "rows_iceberg": n(f"SELECT count(*) FROM ({old})"),
     "rows_manifest": ref[name]["row_count"]}
ok = (r["columns_eq_iceberg"] and r["columns_eq_manifest"]
      and r["rows_new"] == r["rows_iceberg"] == r["rows_manifest"])
for taxon in rnacentral.RNA_TAXA:
    con.execute(f"CREATE OR REPLACE TEMP TABLE new AS SELECT * FROM ({new}) WHERE taxon_id = {taxon}")
    con.execute(f"CREATE OR REPLACE TEMP TABLE old AS SELECT * FROM ({old}) WHERE taxon_id = {taxon}")
    for side, a, b in (("new", "new", "old"), ("iceberg", "old", "new")):
        diff = f"SELECT * FROM {a} EXCEPT ALL SELECT * FROM {b}"
        r[f"only_{side}_{taxon}"] = k = n(f"SELECT count(*) FROM ({diff})")
        ok = ok and not k
        if k:
            print(name, taxon, side, con.sql(f"{diff} LIMIT 3").fetchall(), file=sys.stderr)
    r[f"rows_{taxon}"] = n("SELECT count(*) FROM new")
r["pass"] = ok
print(json.dumps(r, indent=1))
sys.exit(0 if ok else 1)
EOF
echo "raw equivalence exit $?"
```

Pass: both JSON files have `"pass": true`, the counts are 264,333,345 and 2,753,423 on
every side, and no `only_*` is non-zero.

## 4. Partial gate

Builds a two-table bundle from the scratch lake (writes about 2.6 GB of Parquet under
`$T/gate`) and checks it against v2026-11-03 with the contracts this PR adds.

```bash
"$W/scripts/migrate/partial-gate.sh" "$W" "$T/manifest-v2026-11-03.json" "$T/gate" \
  raw.rnacentral__id_mapping annotation.rnacentral__rna 2>&1 | tee "$T/gate.txt"
tail -1 "$T/gate.txt"   # want: PASS
```

## 5. Clean up

```bash
cd ~ && rm -rf "$D"
```
