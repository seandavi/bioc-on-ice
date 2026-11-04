# PubTator3 migration (#183 wave H2): commands for the supervisor

The heavy steps were not run by the migrating agent. Every number below marked
*measured* comes from a run. Everything else is an estimate and says so.

Paths: bioc-on-ice worktree `$W`, cdsci-lake worktree `$C` (both on
`migrate/pubtator3`), scratch `$D`. Run each step detached (`systemd-run --user`
or `setsid nohup … &`) with an explicit `timeout`, and keep every temp file on `/data`.

```bash
W=~/Documents/git/worktrees/migrate-pubtator3/bioc-on-ice
C=~/Documents/git/worktrees/migrate-pubtator3/cdsci-lake
REF=/data/davsean/tmp/pubtator3-mig/manifest-v2026-11-03.json   # curl -o, already fetched
```

## Sizes

| file | rows at the Iceberg version (2026-09-19) | gzip bytes upstream (Last-Modified 2026-10-06, HEAD 2026-11-03) |
|---|---:|---:|
| gene2pubtator3.gz | 74,519,899 | 757,625,998 |
| disease2pubtator3.gz | 168,305,967 | 2,180,457,155 |
| chemical2pubtator3.gz | 146,957,635 | 1,878,281,874 |
| species2pubtator3.gz | 48,958,091 | 490,625,738 |
| mutation2pubtator3.gz | 8,135,311 | 116,112,742 |
| **total** | **446,876,903** | **5,423,103,507 (5.4 GB)** |

## 1. Scratch lake at the Iceberg version

Upstream rewrote the files on 2026-10-06 and archives none, so the 2026-09-19
dump Iceberg holds cannot be downloaded. Re-export it from Iceberg in the upstream
dialect, as step 7 did for HGNC. Row order differs from the original file, so
`line_number` differs, but it is not published.

```bash
D=$(mktemp -d /data/davsean/tmp/pubtator3-scratch.XXXXXX); cd "$D"
export CU_OPENALEX_LAKE_BACKEND=local CU_OPENALEX_STORAGE_BASE_URI="file://$D" \
  CU_OPENALEX_LAKE_CATALOG="$D/catalog.ducklake" CU_OPENALEX_LAKE_DATA_PREFIX=data \
  CU_OPENALEX_DUCKDB_TEMP_DIRECTORY="$D/duckdb_tmp" TMPDIR=/data/davsean/tmp
mkdir -p "$D/iceberg-2026-09-19"
timeout 4h uv run --project "$W" python -I - "$D/iceberg-2026-09-19" <<'EOF'
import sys, duckdb
from bioconice import publish, pubtator3
con = duckdb.connect()
con.execute(publish.ATTACH_LIVE)
for kind in pubtator3.KINDS:
    out = f"{sys.argv[1]}/{kind}2pubtator3.gz"
    con.execute(f"COPY (SELECT pmid, type, concept_id, mentions, resource "
                f"FROM bioc.raw.pubtator3__{kind}) TO '{out}' (FORMAT csv, DELIMITER '\t', "
                "HEADER false, QUOTE '', ESCAPE '', NULLSTR '', COMPRESSION gzip)")
    print(kind, con.sql(f"SELECT count(*) FROM bioc.raw.pubtator3__{kind}").fetchone()[0], flush=True)
EOF
# expect the five row counts in the table above
timeout 4h uv run --project "$C" python -m cdsci.lake.sources.pubtator3 run \
  --directory "$D/iceberg-2026-09-19" --version 2026-09-19 --mode append
```

`QUOTE ''` with `NULLSTR ''` round-trips through the source's read (checked on a
row holding `"`, a comma, `-` and a NULL mention). Expected: `rows=446876903`, and
counts per table equal to the table above.

## 2. Idempotence

Re-run the default merge. Expected: `status=idempotent`, the snapshot id unchanged.
One batch per file (the default) reads each target once; *measured* 65 s for species
and 233 s for disease, so all five take ~10 min.

```bash
timeout 1h uv run --project "$C" python -m cdsci.lake.sources.pubtator3 run \
  --directory "$D/iceberg-2026-09-19" --version 2026-09-19
```

## 3. Equivalence against Iceberg (read-only)

Full row counts and columns for all six tables, `EXCEPT ALL` both ways on PMIDs
35,000,000–35,099,999. The mention count runs the release SELECT whole: a DISTINCT
over ~480M exploded rows that spills to `$D/duckdb_tmp`.

```bash
cd "$D"
timeout 4h uv run --project "$W" python "$W/scripts/migrate/pubtator3-equivalence.py" "$REF" \
  > equivalence.json 2> equivalence.log; echo "exit $?"
```

Expected: exit 0, and per table `columns_eq_iceberg`, `columns_eq_manifest` true,
`rows_new == rows_iceberg == rows_manifest`, `only_new_pmid_range == only_iceberg_pmid_range
== 0`, `rows_pmid_range > 0`. Raw rows are exact by construction (exported from
Iceberg). The mention table is the real check: lake SELECT against what the lander
merged.

## 4. Partial gate

```bash
cd "$D"
timeout 4h "$W/scripts/migrate/partial-gate.sh" "$W" "$REF" "$D/gate" \
  raw.pubtator3__gene raw.pubtator3__disease raw.pubtator3__chemical \
  raw.pubtator3__species raw.pubtator3__mutation annotation.pubtator3__mention
```

Expected: `PASS`. The six tables are contracted now (they left
`UNCONTRACTED_TABLES`). The raw contracts have no key, and the mention contract is
keyed `(pmid, concept_type, concept_id, resource)`, so the gate checks that key over
478M rows.

## 5. Full re-run timing (scratch, detached)

The production procedure (section 6) on the scratch lake: two default runs against
current upstream, all five kinds, merge. Run 1 rewrites every row (a new dump shifts
every `line_number`); run 2 must be idempotent. Uses a copy of `$D`'s lake, so the
Iceberg-version lake stays as sections 3 and 4 need it.

```bash
T=$(mktemp -d /data/davsean/tmp/pubtator3-rerun.XXXXXX)
cp -a "$D/catalog.ducklake" "$D/data" "$T/"; mkdir -p "$T/duckdb_tmp"
uv run --project "$C" python -I -c "import duckdb, sys; duckdb.connect(sys.argv[1]).execute(
  \"UPDATE ducklake_metadata SET value = ? WHERE key = 'data_path'\", [sys.argv[2]])" \
  "$T/catalog.ducklake" "$T/data/"
cd "$T"
export CU_OPENALEX_LAKE_BACKEND=local CU_OPENALEX_STORAGE_BASE_URI="file://$T" \
  CU_OPENALEX_LAKE_CATALOG="$T/catalog.ducklake" CU_OPENALEX_LAKE_DATA_PREFIX=data \
  CU_OPENALEX_DUCKDB_TEMP_DIRECTORY="$T/duckdb_tmp" TMPDIR=/data/davsean/tmp
setsid nohup bash -c 'for i in 1 2; do /usr/bin/time -v timeout 4h uv run --project "$0" \
  python -m cdsci.lake.sources.pubtator3 run > run$i.log 2>&1 || exit; done' "$C" \
  < /dev/null > /dev/null 2>&1 &
```

Expected: `run1.log` ends `status=success`, `rows` equal to the upstream line counts;
`run2.log` ends `status=idempotent` with `snapshot` equal to run 1's. Times are in
section 7.

## 6. Production (standing approval, step 9)

After the cdsci-lake PR merges. The tables may or may not exist: the default merge
creates an absent table and merges into an existing one, so the procedure is the same
either way. Do not use `--mode append` here: it refuses any slice that holds rows.

Run the default entrypoint **twice** against current upstream, each under its own
4-hour cap. With no `--version` it downloads the five dumps into
`raw/pubtator3/<today>/` and stamps today's date; the second run on the same day
reuses the download.

```bash
P=$(mktemp -d /data/davsean/tmp/pubtator3-prod.XXXXXX); cd "$P"
export CU_OPENALEX_LAKE_BACKEND=postgres TMPDIR=/data/davsean/tmp
for i in 1 2; do
  timeout 4h uv run --project ~/Documents/git/cdsci-lake python -m cdsci.lake.sources.pubtator3 run \
    > run$i.log 2>&1 || { echo "run $i failed: $?"; break; }
done
```

Acceptance:

- `run1.log`: `status=success` (or `idempotent` if a previous run already landed
  this dump), one `<kind>_annotation <- N rows` line per kind.
- `run2.log`: `status=idempotent — no change`, `snapshot` equal to run 1's, and the
  same five row counts. An idempotent run makes no snapshot at all, so this holds for
  every table. `pubtator3_version` is out of change detection, so this also holds if
  run 2 starts after midnight and re-downloads under the next date.

Retry:

- **A run fails or times out.** Tables land one at a time, in the order gene, disease,
  chemical, species, mutation. Each table's line delete, merge and prune (every batch,
  with `--batches`) are one transaction and one snapshot, so the tables before the
  failing one are at the new dump and the failing table and those after it are as
  they were before the run, never half-merged. Run the same command again: merge is
  idempotent and finishes the rest. Then run once more and require `idempotent`.
- **A download dies.** It resumes from `<file>.part` on the next run.
- **A check fails** (`line N has K fields`, `Type other than`, `zero rows`): nothing
  was written. Delete that day's `raw/pubtator3/<date>/` file and re-run; a second
  failure on a fresh download is an upstream problem, stop and report it.
- **Run 2 is not idempotent.** Upstream regenerates about monthly; compare the files'
  `Last-Modified` (`curl -sI …/disease2pubtator3.gz`) with run 1's download. If
  upstream changed, delete `raw/pubtator3/<date>/` and repeat the two runs. If it did
  not, stop and report: that is a lander bug.

## 7. Time against the 4-hour cap

*Measured* on species and disease, one batch, local scratch lake on this host,
4 DuckDB threads (2026-11-04). Peak RSS from `/usr/bin/time -v`. Each run reads each
target once: one MERGE and one prune per table, plus one line delete.

| run | kind | rows | elapsed | peak RSS |
|---|---|---:|---:|---:|
| unchanged | species | 48,958,091 | 65 s | 12.8 GB |
| new dump (every row rewritten) | species | 49,148,290 | 102 s | 16.4 GB |
| unchanged | disease | 168,305,967 | 233 s | 47.8 GB |
| new dump (every row rewritten) | disease | 168,857,629 | 367 s | 59.3 GB |

For comparison, the old default of 20 batches took 346 s (2.0 GB) on the unchanged
species re-run, 279 s of it in 20 gzip passes, and a full re-run of disease and
chemical would have read each 168M- and 147M-row target 20 times.

Extrapolated by rows (447M rows in all, 2.7x disease): run 1 against a new dump
~16 min, run 2 ~10 min, plus ~90 s of download (*measured* 62 MB/s from NCBI).
Peak memory is the largest table, disease: ~59 GB. That is below DuckDB's
default limit of 70% of RAM, and DuckDB spills to `duckdb_temp_directory` past it.
The production lake writes Parquet to R2 instead of a local disk; not measured, but
the write is one pass per table, not twenty.

- **Iceberg re-export** (scratch only): 447M rows over anonymous icegate/R2. Not
  measured; budget 1–2 h.
- **Publish**: the mention release SELECT is a DISTINCT over ~480M exploded rows. The
  lander measured 16.2 GB peak for species alone (55M derived rows) in memory; DuckDB
  spills, so expect tens of GB in `duckdb_temp_directory`.

## Merge order

1. cdsci-lake PR (`sources/pubtator3`).
2. Production ingest (section 6): two default runs, the second idempotent.
3. Bump the cdsci-lake pin in bioc-on-ice to the merged commit.
4. bioc-on-ice PR. Merged earlier, the next `bioconice publish` fails:
   `lake.pubtator3.*` does not exist in the production lake.
