# identifier_mapping (#183 G2): supervisor commands

The long runs the migration worker did not do: the model freshness check, the
equivalence against Iceberg and the partial gate. Every writer is already in the shared
lake, so nothing is loaded: both sides are **read only** against production
(`CU_OPENALEX_LAKE_BACKEND=postgres`, `lake_connect(read_only=True)`) and the live
Iceberg catalog. The one write below (1b, refreshing a stale model) is a production lake
write and needs the orchestrator's step-9 authority. Run each step detached
(`systemd-run --user` or `setsid nohup … &`) with its `timeout`, temp files on `/data`.

## What the union is

`publish.FROM_LAKE["annotation.identifier_mapping"]`, a UNION ALL of one branch per writer:

| source | branch | lake table | scope |
|---|---|---|---|
| NCBI | `ncbi.MAPPING` | model `ncbi_gene.mapping` | all taxa |
| NCBI_ACCESSION | `ncbi_accession.MAPPING` | model `ncbi_gene2accession.mapping` | all taxa |
| ENSEMBL | `ensembl.MAPPING` | `ensembl.gene` | the published assembly per taxon (276) |
| HGNC | `hgnc.MAPPING` | `hgnc.gene` | 9606 |
| MANE | `mane.MAPPING` | `mane.transcript` | 9606 |
| RNACENTRAL | `rnacentral.MAPPING` | `rnacentral.id_mapping` | 9606, 10090; no ENA (ADR-0015 c) |
| UniProt | inline in `publish.py` | model `uniprot.identifier_mapping` | 9606 |

BioGRID and IntAct write no `identifier_mapping` rows (code and v2026-11-03 agree).

## Measured so far (worker, 2026-11-04)

v2026-11-03 per source (its local Parquet, `/data/davsean/tmp/bioconice-release-2026-11-03`),
and taxon 9606 on both sides. Iceberg's current 9606 rows gave the same counts as the
Parquet (64 s through the gateway).

| source | v2026-11-03 rows | Iceberg 9606 | lake branch 9606 | 9606 query time |
|---|---:|---:|---:|---:|
| NCBI | 121,856,131 | 456,282 | 456,801 | 87 s |
| NCBI_ACCESSION | 332,152,875 | 1,025,764 | not measured | > 220 s (killed) |
| Ensembl / ENSEMBL | 4,232,653 | 43,458 | 43,458 | 7 s |
| HGNC | 128,704 | 128,704 | 128,988 | 0.4 s |
| MANE | 19,437 | 19,437 | 19,437 | 0.4 s |
| RNACENTRAL | 5,911,834 (ENA 1,918,777) | 4,882,016 | not measured | > 220 s (killed) |
| UniProt | 34,223 | 34,223 | 39,878 | 17 s |
| **total** | **464,335,857** | | | |

Lake versions on 2026-11-04: NCBI dumps `snapshot_version` 2026-10-10; HGNC 2026-11-01
(Iceberg 2026-09-18); MANE 1.5 (same); Ensembl 116 (same); RNAcentral **28** (Iceberg 27);
`uniprot.idmapping` loaded 2026-10-03. Model builds from `lake_ops.run`:
`ncbi_gene.mapping` 123,868,663 rows (2026-10-31, **stale**: its inputs changed in
snapshot 4891, after its build at 4729), `ncbi_gene2accession.mapping` 334,983,091
(2026-10-31, current), `uniprot.identifier_mapping` 20,605,114 (2026-10-03, current;
39,878 for 9606).

## 0. Env

```bash
W=<bioc-on-ice worktree on migrate/identifier-mapping>
C=~/Documents/git/cdsci-lake                     # main; its .venv has sqlglot
T=$(mktemp -d /data/davsean/tmp/identifier-mapping-g2.XXXXXX); cd "$T"
export CU_OPENALEX_LAKE_BACKEND=postgres TMPDIR=/data/davsean/tmp
REF=/data/davsean/tmp/bioconice-release-2026-11-03/manifest.json  # or curl -o from the release URL
```

## 1. Are the three models built from what their inputs hold now?

Read only: the ledger is attached `READ_ONLY` (not through `with_ops`, which bootstraps it).
`is_stale` is the transform runner's own test: never built, SQL changed, or an input
written after the model's last successful build.

```bash
cd "$C" && timeout 10m .venv/bin/python - > "$T/stale.txt" 2>&1 <<'EOF'
from cdsci.lake import ops
from cdsci.lake.config import get_settings
from cdsci.lake.connect import lake_connect
from cdsci.lake.transform import runner
from cdsci.lake.transform.lineage import table_dependencies
from cdsci.lake.transform.models import load_models

s = get_settings()
con = lake_connect(read_only=True)
con.execute(f"ATTACH 'dbname={s.lake_pg_dbname} host={s.lake_pg_host} port={s.lake_pg_port} "
            f"user={s.lake_pg_user}' AS {ops.OPS} (TYPE postgres, READ_ONLY)")
models = load_models("transform/models")
for t in ("ncbi_gene.mapping", "ncbi_gene2accession.mapping", "uniprot.identifier_mapping"):
    last = ops.last_run(con, t, status="success")
    deps = {d: ops.last_change_snapshot(con, f"lake.{d}") for d in table_dependencies(models[t])}
    print(t, "STALE" if runner.is_stale(con, models[t]) else "current",
          {k: last[k] for k in ("snapshot_after", "rows_after", "finished_at")} if last else None,
          "inputs changed at", deps, flush=True)
EOF
cat "$T/stale.txt"; cd "$T"
```

Expected now: `ncbi_gene.mapping STALE`, the other two `current`. The model was built
at snapshot 4729 (2026-10-31); `ncbi_gene` runs wrote snapshots 4835-4891 on 2026-11-02/03,
when `gene_history`, `gene_orthologs` and `gene_group` joined the schema (#183 C3c, C4).
A run against the schema counts as a change to every table in it, so `gene_info` and
`gene2ensembl` may not have changed (their `snapshot_version` is still 2026-10-10).

### 1b. Refresh a stale model (production lake write)

```bash
cd "$C" && timeout 2h .venv/bin/python -m cdsci.lake.transform run --if-stale ncbi_gene.mapping \
  > "$T/refresh.txt" 2>&1; tail -2 "$T/refresh.txt"; cd "$T"
```

Its 2026-10-31 build took 3.5 min (`lake_ops.run`; `ncbi_gene2accession.mapping` took
14 min); the cap is 2 h. Its SELECT DISTINCT spills, so `CU_OPENALEX_DUCKDB_TEMP_DIRECTORY`
must be on `/data`. Re-run step 1: want `current`.
If the dumps did not change, the row count stays 123,868,663.

## 2. Equivalence against Iceberg

```bash
( time timeout 2h uv run --project "$W" python "$W/scripts/migrate/identifier-mapping-equivalence.py" \
    "$REF" 9606 ) > "$T/equivalence.json" 2> "$T/equivalence.err"
echo "exit $?"; tail -5 "$T/equivalence.err"
```

What it does: columns of the union against Iceberg and the manifest; rows per source,
whole table, on both sides (two full passes: the lake union and Iceberg's 464M current
rows through the gateway); EXCEPT ALL per source on taxon 9606, Iceberg's `Ensembl` read
as `ENSEMBL` and its ENA rows set aside; the gate's key on the slice. Exit 0 means the
columns and key hold; the row differences are judged against the table below.

Time budget: not measured whole. The 9606 slices above took up to > 220 s per branch
(NCBI_ACCESSION and RNACENTRAL scan their whole lake table: 335M and 264M rows), and
Iceberg's 9606 slice 64 s. Expect 30-60 min; the cap is 2 h.

Expected (`rows_*` whole table, `slice` taxon 9606):

| source | expected | why |
|---|---|---|
| ENSEMBL | `slice` only_new = only_iceberg = 0; rows equal (4,232,653) | Ensembl 116 on both sides, the same 276 assemblies (G1). Iceberg spells the source `Ensembl`: `migrate-assembly-scope` never respelled it in production. The release publishes `ENSEMBL`, SPEC's spelling: **a release-notes item** |
| MANE | 0 / 0; 19,437 | v1.5 on both sides |
| HGNC | small both ways; 128,988 vs 128,704 | lake 2026-11-01 vs Iceberg 2026-09-18 (+284 net at 9606) |
| NCBI | both ways; ~123.9M vs 121,856,131 | lake dumps 2026-10-10 vs Iceberg's (2026-09); the model is ported from `ncbi.transform`, same SQL. At 9606: 456,801 vs 456,282 |
| NCBI_ACCESSION | both ways; ~335.0M vs 332,152,875 | the same newer dump; same SQL, including the `^[A-Z]{2}_` RefSeq rule |
| RNACENTRAL | `slice_iceberg_ena` > 0 and `rows_iceberg_ena` = 1,918,777 set aside; then both ways | ADR-0015 c drops every ENA row (−1,918,777, measured on v2026-11-03); the lake is release 28, Iceberg 27, so the non-ENA rows also differ. Iceberg's non-ENA total is 3,993,057 |
| UniProt | only_new ≈ 5,655 or more at 9606; 39,878 vs 34,223 | Iceberg holds the old reverse-ETL copy of an earlier `uniprot.idmapping` load (cdsci-lake's disabled Iceberg target); the lake model is the 2026-10-03 load. Not checked row by row by the worker |

A difference not explained above (another source, a column, an ENSEMBL or MANE row on
one side only) is a stop.

Expected total ≈ 123.9M + 335.0M + 4.23M + 0.13M + 0.02M + ~4.0M + 0.04M ≈ **467M**,
about 1.006 × 464,335,857: inside the gate's default bounds (0.9, 1.5).

## 3. Partial gate

Builds a one-table bundle from the production lake (read only; about 4.8 GB of Parquet
under `$T/gate`, the size of v2026-11-03's) and checks it against v2026-11-03 with the
contract this PR adds: columns unchanged, row count in bounds, the key
`(source_namespace, source_id, target_namespace, target_id, taxon_id, source)` unique and
not NULL over every row, and the manifest checks. No `ALLOW_COLUMN_CHANGE`: the columns
and types are v2026-11-03's.

```bash
( time timeout 3h "$W/scripts/migrate/partial-gate.sh" "$W" "$REF" "$T/gate" \
    annotation.identifier_mapping ) > "$T/gate.txt" 2>&1
tail -1 "$T/gate.txt"   # want: PASS
```

Time budget: not measured. The COPY reads every branch once (the same passes as step 2's
lake count) and the key check groups ~467M rows locally. Expect 45-90 min; the cap is 3 h.

## Pass

Step 1 `current` for all three (after 1b if needed), step 2 exit 0 with every difference
in the table above, step 3 `PASS`. Report the per-source counts from `equivalence.json`
in the PR, and the `Ensembl` → `ENSEMBL` and ENA changes in the release notes.
