# PubTator3 migration (#183 wave H2): commands for the supervisor

The heavy steps were not run by the migrating agent. Every number below marked
*measured* comes from a run. Everything else is an estimate and says so.

Paths: bioc-on-ice worktree `$W`, cdsci-lake worktree `$C` (both on
`migrate/pubtator3`), scratch `$D`. Run each step detached (`systemd-run --user`
or `nohup … &`) with an explicit `timeout`, and keep every temp file on `/data`.

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

Re-run at `--mode merge`. Expected: `status=idempotent — no change` and the
snapshot id unchanged. Each batch's MERGE reads its whole target table, so do the
two small files first; the big three repeat the same code path.

```bash
for k in mutation species; do
  timeout 2h uv run --project "$C" python -m cdsci.lake.sources.pubtator3 run \
    --directory "$D/iceberg-2026-09-19" --version 2026-09-19 --kind $k
done
# optional, hours: the same with no --kind
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

## 5. Current upstream (optional, before production)

```bash
timeout 4h uv run --project "$C" python -m cdsci.lake.sources.pubtator3 run \
  --version "$(date +%F)" --mode merge
```

With line_number as the key, every line of a new dump shifts, so this rewrites every
row of every table. On the scratch lake it is a dry run of the monthly refresh cost.

## 6. Production (standing approval, step 9)

After the cdsci-lake PR merges. The first load goes into empty tables at
`--mode append`, one file per run so each run is capped on its own:

```bash
cd "$(mktemp -d /data/davsean/tmp/pubtator3-prod.XXXXXX)"
export CU_OPENALEX_LAKE_BACKEND=postgres TMPDIR=/data/davsean/tmp
V=$(date +%F)
for k in mutation species gene chemical disease; do
  timeout 4h uv run --project ~/Documents/git/cdsci-lake python -m cdsci.lake.sources.pubtator3 run \
    --kind $k --version $V --mode append || break
done
```

If a run dies part way, its table has some slices and not others. Re-run that
`--kind` at `--mode merge`: append refuses a slice that already holds rows.

## Time against the 4-hour cap (estimates)

- **Download**: 5.4 GB from NCBI over https. At 20–50 MB/s that is 2–5 min. Not measured.
- **Scan**: *measured* 0.39 s for 1,940,315 gene lines (80.8 MB of TSV, ~42 B/line)
  through `row_number()` plus the shard filter: ~200 MB/s. All five files are ~18.8 GB
  of TSV, ~95 s per pass. A load makes 1 check pass and `batches` (20) load passes:
  **~35 min of scanning** for all five, ~13 min for disease alone.
- **Write**: not measured. The initial append writes ~447M rows of Parquet once. The
  nearest precedent is gene2accession (285M rows, sharded the same way), whose module
  calls the parquet write the dominant cost and the job "overnight-shaped". Expect
  **1–3 h** for all five at `append`, inside the cap per file if not for the whole.
- **Iceberg re-export** (scratch only): 447M rows over anonymous icegate/R2. Not
  measured; budget 1–2 h.
- **Merge re-runs** (idempotence, monthly refresh): each of the 20 batch MERGEs reads
  its whole target, so disease costs ~20 reads of 168M rows. Likely **beyond 4 h** for
  disease and chemical at `merge`. This is the risk for the monthly refresh: a new dump
  shifts every line_number, so every row is rewritten anyway.
- **Publish**: the mention release SELECT is a DISTINCT over ~480M exploded rows. The
  lander measured 16.2 GB peak for species alone (55M derived rows) in memory; DuckDB
  spills, so expect tens of GB in `duckdb_temp_directory`.

## Merge order

1. cdsci-lake PR (`sources/pubtator3`).
2. Production ingest (section 6).
3. Bump the cdsci-lake pin in bioc-on-ice to the merged commit.
4. bioc-on-ice PR. Merged earlier, the next `bioconice publish` fails:
   `lake.pubtator3.*` does not exist in the production lake.
