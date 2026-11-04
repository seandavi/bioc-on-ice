# iCite (#183 H1): supervisor commands

The long runs the migration worker did not do: the scratch load of the full snapshot, its
re-run, the value equivalence and the partial gate. Run them detached, in this order, from
one shell. Worktrees: `~/Documents/git/worktrees/migrate-icite/{cdsci-lake,bioc-on-ice}`
on branch `migrate/icite`. Nothing below writes outside `$D`; Iceberg is read only and the
lake is a scratch one (ADR-0015 scratch recipe, `CU_OPENALEX_LAKE_BACKEND=local`).

Expected, from the v2026-11-03 manifest, all at iCite snapshot **2026-08** (Figshare file
68211877): `raw.icite__metadata`, `annotation.icite__publication` and
`annotation.icite__metrics` 41,035,229 rows each, `annotation.icite__citation` 928,458,585.
The production lake is at 2026-09 and Figshare's latest is newer, so the snapshot is
downloaded by its file id and loaded with `--file`: `run --version 2026-08` alone labels
the load 2026-08 but downloads the *latest* snapshot (`download_snapshot` resolves the
file from the newest article whatever the version).

The four tables change columns, owner-approved (see the PR body for the exact diff), so
the generic `equivalence.py` (same columns as Iceberg) does not apply: step 3 recasts the
Iceberg rows to the published shape with `icite.AS_PUBLISHED`, then compares both sides
as text.

Nothing larger than the 3-row test fixture was run by the worker. Load time, peak memory
and spill size are unmeasured; step 1a measures them on a prefix before the full load.

## 0. Scratch env

```bash
C=~/Documents/git/worktrees/migrate-icite/cdsci-lake
W=~/Documents/git/worktrees/migrate-icite/bioc-on-ice
D=$(mktemp -d /data/davsean/tmp/bioconice-scratch-lake.XXXXXX)
T=$D/out; mkdir -p "$T" "$D/icite"
cd "$D"
export CU_OPENALEX_LAKE_BACKEND=local
export CU_OPENALEX_STORAGE_BASE_URI="file://$D"
export CU_OPENALEX_LAKE_CATALOG="$D/catalog.ducklake"
export CU_OPENALEX_LAKE_DATA_PREFIX=data
export CU_OPENALEX_DUCKDB_TEMP_DIRECTORY="$D/duckdb_tmp"
curl -s -o "$T/manifest-v2026-11-03.json" \
  https://data.bioconice.cancerdatasci.org/v2026-11-03/manifest.json
```

## 1. Download the Iceberg snapshot (2026-08, ~14 GB zip, ~40 GB CSV)

```bash
curl -fL -o "$D/icite/2026-08.zip" https://ndownloader.figshare.com/files/68211877
unzip -o -d "$D/icite/2026-08" "$D/icite/2026-08.zip"
CSV=$(ls "$D"/icite/2026-08/*.csv); echo "$CSV"
```

### 1a. Time a prefix first (the 4-hour cap)

One million rows into a throwaway lake, then extrapolate ×41 (the snapshot is 41.0M rows).

```bash
head -n 1000001 "$CSV" > "$D/icite/prefix.csv"   # header + 1M rows (a quoted newline may cut one short)
( time env CU_OPENALEX_LAKE_CATALOG="$D/prefix.ducklake" CU_OPENALEX_LAKE_DATA_PREFIX=prefix \
    timeout 1h uv run --project "$C" python -m cdsci.lake.sources.icite run \
    --file "$D/icite/prefix.csv" --version 2026-08 ) > "$T/prefix.log" 2>&1
tail -5 "$T/prefix.log"; du -sh "$D/prefix" "$D/duckdb_tmp" 2>/dev/null
rm -rf "$D/prefix" "$D/prefix.ducklake"* "$D/icite/prefix.csv"
```

Estimate (unmeasured; replace with 41 × the prefix's `real`): the production 17-column
load of the 2026-09 CSV took 6 min 16 s (journal, 2026-11-01, idempotent). This load also
parses and writes the three PMID lists (Iceberg's raw Parquet is 12 GB, against 3.5 GB
for publication), then explodes 928M edges once (`SELECT DISTINCT`, spills to
`$D/duckdb_tmp`) and runs 16 MERGEs of ~58M edges into an empty table. Expected 1–2.5 h
for step 2 with the download ~15 min on top, inside the 4-hour cap. If 41 × the prefix
exceeds 3 h, stop and report rather than start step 2.

## 2. Scratch load at the Iceberg version, and the idempotence re-run

```bash
( time timeout 4h uv run --project "$C" python -m cdsci.lake.sources.icite run \
    --file "$CSV" --version 2026-08 ) > "$T/load1.log" 2>&1
grep -E "SUCCESS|rows" "$T/load1.log" | tail -4   # want: success, metadata rows=41035229
( time timeout 4h uv run --project "$C" python -m cdsci.lake.sources.icite run \
    --file "$CSV" --version 2026-08 ) > "$T/load2.log" 2>&1
grep -E "idempotent|SUCCESS" "$T/load2.log" | tail -4   # want: both tables idempotent, snapshot N→N
```

## 3. Value equivalence against Iceberg

Per table: the release SELECT over the scratch lake (`new`) against Iceberg's current rows
recast by `icite.AS_PUBLISHED` (`old`); every column cast to text on both sides. Full row
counts against Iceberg and the manifest; EXCEPT ALL both ways on PMIDs 30,000,000–30,999,999
(`citing_pmid` for the citation table).

```bash
uv run --project "$W" python - "$T/manifest-v2026-11-03.json" > "$T/equivalence.json" <<'EOF'
import json, sys
from pathlib import Path
from cdsci.lake.connect import lake_connect
from bioconice import icite, publish

LO, HI = 30_000_000, 30_999_999
ref = {f"{t['schema']}.{t['table']}": t for t in json.loads(Path(sys.argv[1]).read_text())["tables"]}
con = lake_connect(read_only=True)
con.execute(publish.ATTACH_LIVE)
n = lambda sql: con.sql(sql).fetchone()[0]
out, ok = [], True
for name, new in icite.RELEASE.items():
    old = icite.AS_PUBLISHED[name].format(f"({publish._current(name, publish.DECLARED[name])})")
    key = "citing_pmid" if name.endswith("citation") else "pmid"
    r = {"table": name, "rows_new": n(f"SELECT count(*) FROM ({new})"),
         "rows_iceberg": n(f"SELECT count(*) FROM ({old})"), "rows_manifest": ref[name]["row_count"]}
    for side, sql in (("new", new), ("old", old)):
        con.execute(f"CREATE OR REPLACE TEMP TABLE {side} AS SELECT COLUMNS(*)::VARCHAR "
                    f"FROM ({sql}) WHERE {key} BETWEEN {LO} AND {HI}")
    r["rows_in_range"] = n("SELECT count(*) FROM new")
    t_ok = r["rows_new"] == r["rows_iceberg"] == r["rows_manifest"] and r["rows_in_range"] > 0
    for side, a, b in (("new", "new", "old"), ("iceberg", "old", "new")):
        diff = f"SELECT * FROM {a} EXCEPT ALL SELECT * FROM {b}"
        r[f"only_{side}"] = k = n(f"SELECT count(*) FROM ({diff})")
        t_ok = t_ok and not k
        if k:
            print(name, side, con.sql(f"{diff} LIMIT 3").fetchall(), file=sys.stderr)
    r["pass"] = t_ok
    ok = ok and t_ok
    out.append(r)
print(json.dumps(out, indent=1))
sys.exit(0 if ok else 1)
EOF
echo "equivalence exit $?"
```

Pass: exit 0, every `pass` true, counts 41,035,229 (citation 928,458,585) on all three
sides, no `only_*` non-zero. Full Iceberg counts scan the 12 GB raw and 8 GB citation
Parquet over icegate; budget ~30 min for this step.

## 4. Partial gate, with `--allow-column-change` on the four tables

Builds a bundle of the five iCite-backed tables from the scratch lake (writes about 25 GB of
Parquet under `$T/gate`) and checks it against v2026-11-03 with the contracts this PR adds.
`raw.metadata` is included to show its columns unchanged (it is pruned to the snapshot, so
its rows fall from 41,172,752 to 41,035,229: inside the row-count bound).

```bash
ALLOW_COLUMN_CHANGE="raw.icite__metadata annotation.icite__publication annotation.icite__metrics annotation.icite__citation" \
  "$W/scripts/migrate/partial-gate.sh" "$W" "$T/manifest-v2026-11-03.json" "$T/gate" \
  raw.icite__metadata annotation.icite__publication annotation.icite__metrics \
  annotation.icite__citation raw.metadata 2>&1 | tee "$T/gate.txt"
grep "column change allowed" "$T/gate.txt"   # want: the four diffs in the PR body, nothing for raw.metadata
tail -1 "$T/gate.txt"                         # want: PASS
```

The release that ships this runs the full gate with the same four tables:
`bioconice gate <bundle> --allow-column-change raw.icite__metadata annotation.icite__publication annotation.icite__metrics annotation.icite__citation`.

## 5. Clean up

```bash
cd ~ && rm -rf "$D"
```
