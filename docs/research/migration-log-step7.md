# Migration log, #183 step 7 (2026-11-01)

**Status**: working record, not a decision. It covers two sources migrated by
hand, HGNC and Complex Portal, plus the `annotation.signature_taxon` repoint.
It records every command, decision, surprise and gotcha, written as the recipe
that step 8 freezes into `.pi/workflows/migrate-source.js`. Nothing here wrote
to the shared lake, Iceberg or R2. The only lake writes went to a local scratch
DuckLake (ADR-0015, "Scratch catalog recipe"). The map of all 81 tables is
[`lake-migration-map.md`](lake-migration-map.md).

## Outcome

| table | lake source | same upstream version as Iceberg | gate on the 5-table bundle |
|---|---|---|---|
| `raw.hgnc__complete_set` | `lake.hgnc.gene` | 45,083 = 45,083 rows, 0 diffs both ways | pass |
| `annotation.hgnc__gene` | `lake.hgnc.gene` | 45,083 = 45,083, 0 diffs | pass |
| `raw.complexportal__complex` | `lake.complexportal.complex` | 20,579 = 20,579, 0 diffs | pass |
| `annotation.complexportal__complex` | `lake.complexportal.complex` | 20,579 = 20,579, 0 diffs | pass |
| `annotation.complexportal__participant` | `lake.complexportal.complex` | 101,561 = 101,561, 0 diffs | pass (incl. `complex_ac` join coverage) |
| `annotation.signature_taxon` | `lake.bugsigdb.signature_taxon` (shared lake, read-only) | 59,486 = 59,486, 0 diffs | already contracted |

"0 diffs" means `EXCEPT ALL` returned no rows in either direction, over every
column including the frozen ones. Columns and types equal both the live Iceberg
table and the `v2026-11-01` manifest, and the contract key is unique and not
null in each table.

**On the current upstream** (HGNC rolling file of 2026-10-30, Complex Portal
2026-10-17), the counts move as expected and stay inside the gate's default
bounds. HGNC: 45,289 ids (208 added, 2 withdrawn); in `hgnc__gene`, 242 rows are
new or edited upstream and 36 Iceberg rows are edited or withdrawn. Every raw
row differs because `hgnc_version` is restamped. Complex
Portal: 21,829 complexes (1,250 added, none dropped) in 30 files instead of 29.
`complexportal__participant` has 108,060 rows; 6,530 are new or changed
upstream, e.g. stoichiometry is now unknown (NULL) for `CPX-11778`'s members
where 2026-01-09 said 1.

Wall-clock per source (the scratch ingest only): HGNC 1.2 s from a local file
and 2.8 s with download; Complex Portal 50 s, almost all of it downloading 29
small files from EBI. Equivalence for 5 tables: 8 s. Partial publish plus gate:
seconds. Tokens were not metered per source.

## Decisions

1. **Picked HGNC and Complex Portal**, the task's candidates. The map confirms
   they are among the smallest new-source modules. Complex Portal is fully
   self-contained. HGNC also writes an `identifier_mapping` branch, which stays
   on Iceberg (map, "Cross-cutting findings"), so step 7 exercised one coupled
   source as well as one clean one.
2. **Frozen carry-over for `valid_from`/`valid_to`/`landed_in`** (supervisor
   decision, option A): a lake-backed SELECT emits `NULL::VARCHAR AS valid_to`,
   and `'2026.09'` (the value on every current row) as `valid_from` or
   `landed_in`. The constant is per source module (`FROZEN_RELEASE`), with a
   comment pointing at #222, which drops all three columns in one deliberate
   schema-change release. The gate's `columns_unchanged` check is required and
   has no exemption, so dropping them now would fail the gate.
   **Workflow rule:** every SCD2 or raw table gets this treatment until #222
   lands. 8 tables have mixed `valid_from` and need a decision first (map).
3. **Placement (ADR-0015 b, confirmed by the supervisor):** the lake gets EL
   tables only, `lake.hgnc.gene` and `lake.complexportal.complex`, with every
   column VARCHAR as published and a version column. Taxon 9606, the
   participant explosion and bioc-on-ice's namespace vocabulary stay in
   bioc-on-ice as release SELECTs.
4. **Names (cdsci-lake ADR-0026):** schema = upstream source (`hgnc`,
   `complexportal`), table named for one row (`gene`, `complex`), no `raw_`
   prefix, because there is no typed table beside these all-VARCHAR landings.
5. **One upstream version per lake table.** The version column
   (`hgnc_version`, `complexportal_version`) takes part in the upsert's change
   compare, as `bugsigdb_version` does, so a new version restamps every row.
   A full load also prunes (`prune="true"`) keys that upstream dropped. Reactome
   instead excludes `snapshot_version` from the compare and does not prune; that
   would leave a mix of versions in the release's raw tables.
6. **One SQL text for the lander and the release.** The lander's transform SQL
   moved into `_gene(src)` / `_complex(src)` / `_participant(src)`, which both
   `transform()` (over Iceberg `raw`) and `RELEASE` (over `lake.*`) call. The
   offline test then only has to show that the lake table plus the wrapper
   reproduce the lander's rows. The lander stays until the production lake
   write. It retires in its own PR once the source is in the production lake
   (ADR-0015 a).
7. **Contracts from declarations.** `gate.declared_contract(name, key,
   references, license)` builds a `TableContract` from `schemas.py`: comment,
   columns, docs and `bioc.license`, keyed by the business key by default.
   The raw tables have no business key. Their contract key leaves out the
   version column (`hgnc_id`, `complex_ac`), because the lake holds one
   version. References were added only where cheap:
   `complexportal__participant.complex_ac` → `complexportal__complex.complex_ac`.
8. **`signature_taxon`** reads `release.SIGNATURE_TAXON_SELECT`, which is the
   `bioconice-bugsigdb` release's SELECT without `AT (VERSION => …)`. The
   bundle reads it unpinned until step 10, like `pubmed__article`.

## Recipe (what step 8 automates)

Paths: bioc-on-ice worktree `$W`, cdsci-lake worktree `$C`, scratch `$D`.

### 0. Preconditions
- `gh issue view 183 --json labels` has no `status:paused`.
- Read the lander (`src/bioconice/<source>.py`), its `schemas.py` entries,
  its row in `lake-migration-map.md`, and the published columns
  (`curl -o manifest.json https://data.bioconice.cancerdatasci.org/v<date>/manifest.json`).
- cdsci-lake changes go in a fresh worktree, never the main checkout:
  `git -C ~/Documents/git/cdsci-lake fetch && git -C ~/Documents/git/cdsci-lake worktree add -b <branch> $C origin/main`.

### 1. cdsci-lake source module (`$C`)
- `src/cdsci/lake/sources/<source>/{__init__,__main__,ingest}.py`, with
  bugsigdb as the template: `COLUMNS` (header checked whole, fail before
  landing), `download_*` into `raw_dir(<source>)`, `curate(con, path(s),
  version, *, target, limit)` → `upsert(key=…, prune=None if limit else "true")`,
  and `ingest(*, file|directory, version, schema, limit, settings)` inside
  `ops.run(...)`. Copy the lander's dialect (delim, quote, escape, nullstr)
  verbatim.
- Register `Source(...)` in `ops.SOURCES`; `build_app` refuses an unregistered
  name. Add the URL to `config.Settings` and a row to `docs/data-licenses.md`
  (quote the upstream licence page and its date).
- Offline test with the fixture written at test time: lands whole, dialect
  edge cases, idempotent re-run (no new snapshot), a new version prunes and
  restamps, a bad header raises.
- `uv sync --all-extras && uv run ruff check src/ tests/ && uv run pytest -q`.

### 2. Scratch lake: same upstream version as Iceberg
```bash
D=$(mktemp -d /data/davsean/tmp/bioconice-scratch-lake.XXXXXX); cd "$D"
export CU_OPENALEX_LAKE_BACKEND=local CU_OPENALEX_STORAGE_BASE_URI="file://$D" \
  CU_OPENALEX_LAKE_CATALOG="$D/catalog.ducklake" CU_OPENALEX_LAKE_DATA_PREFIX=data \
  CU_OPENALEX_DUCKDB_TEMP_DIRECTORY="$D/duckdb_tmp"
uv run --project $C python -m cdsci.lake.sources.complexportal run --version 2026-01-09
uv run --project $C python -m cdsci.lake.sources.hgnc run \
  --file /data/davsean/tmp/step7/inputs/hgnc_complete_set_iceberg-2026-09-18.txt --version 2026-09-18
# re-run both: "status=idempotent — no change"
```
The Iceberg version comes from `provenance.release` (`source_version`,
`version_method`). A dated upstream release (Complex Portal) is downloaded by
version. A rolling, unversioned file (HGNC) is no longer downloadable at that
version, so its raw table is re-exported from Iceberg in the upstream dialect
and landed with `--file`:
```python
con.execute(ATTACH_LIVE)   # publish.ATTACH_LIVE, anonymous read
cols = ", ".join(f'{c.replace(".", "_").replace("-", "_")} AS "{c}"' for c in hgnc.COLUMNS)
con.execute(f"""COPY (SELECT {cols} FROM bioc.raw.hgnc__complete_set WHERE hgnc_version='2026-09-18')
  TO 'hgnc_complete_set_iceberg-2026-09-18.txt' (FORMAT csv, DELIMITER '\t', HEADER, QUOTE '"', ESCAPE '"', NULLSTR '')""")
```

### 3. bioc-on-ice repoint (`$W`)
- In the lander, move each transform SELECT into a `_<table>(src)` function
  and add `RELEASE = {table: SELECT over lake.<schema>.<table>}`, with the
  frozen `FROZEN_RELEASE` constant (#222) and `NULL::VARCHAR AS valid_to`.
  Raw tables: `SELECT *, <frozen> AS landed_in FROM lake.<schema>.<table>`.
- `publish.FROM_LAKE` gains `**<source>.RELEASE`.
- `gate.BUNDLE` gains `declared_contract(...)` for each table, and each
  table's line leaves `UNCONTRACTED_TABLES`.
- A test calls the `release_matches_lander` fixture (`tests/conftest.py`): it
  runs the lander on its fixture, builds `lake.<schema>.<table>` from the raw
  table without `landed_in`, and checks every `RELEASE` SELECT for the same
  columns and types, the same current rows, and the frozen values.
- `uv run pytest -q`.

### 4. Equivalence against Iceberg (read-only)
Run from `$D` with the env above: `uv run --project $W python equivalence.py <table> ...`.
For each table it prints rows (new / Iceberg / manifest), columns == Iceberg
and == manifest, key duplicates and nulls, `EXCEPT ALL` counts both ways, and 3
sample rows of each difference. The script, verbatim:
```python
"""Step-7 equivalence: each migrated table's publish SELECT (over the scratch lake) against
its current Iceberg rows and the published v2026-11-01 manifest. Read-only on both sides.

Run from the scratch dir with the ADR-0015 local-backend env set:
  uv run --project <bioc-on-ice worktree> python equivalence.py <table> ...
"""

import json
import sys

from cdsci.lake.connect import lake_connect

from bioconice import gate, publish

# curl -o; the release Worker answers Python-urllib's default User-Agent with 403.
REF = "/data/davsean/tmp/step7/manifest-v2026-11-01.json"
ref = {f"{t['schema']}.{t['table']}": t for t in json.load(open(REF))["tables"]}
selects = {f"{s}.{t}": q for s, t, q in publish.TABLES}
contracts = {n: c for d in gate.CONTRACTS for n, c in d.tables.items()}

con = lake_connect(read_only=True)
con.execute(publish.ATTACH_LIVE)
for name in sys.argv[1:]:
    new = selects[name]
    old = publish._current(name, publish.DECLARED[name]) if name in publish.DECLARED \
        else f"SELECT * FROM bioc.{name}"
    con.execute(f"CREATE OR REPLACE TEMP TABLE new AS {new}")
    con.execute(f"CREATE OR REPLACE TEMP TABLE old AS {old}")
    cols_new = [tuple(r[:2]) for r in con.sql("DESCRIBE new").fetchall()]
    cols_old = [tuple(r[:2]) for r in con.sql("DESCRIBE old").fetchall()]
    cols_ref = [(c["name"], c["type"]) for c in ref[name]["columns"]]
    n_new, n_old = (con.sql(f"SELECT count(*) FROM {t}").fetchone()[0] for t in ("new", "old"))
    key = contracts[name].primary_key
    dup = con.sql(f"SELECT count(*) FROM (SELECT 1 FROM new GROUP BY {', '.join(key)} "
                  f"HAVING count(*) > 1)").fetchone()[0]
    nulls = con.sql(f"SELECT count(*) FROM new WHERE "
                    + " OR ".join(f"{k} IS NULL" for k in key)).fetchone()[0]
    only_new = con.sql("SELECT count(*) FROM (SELECT * FROM new EXCEPT ALL SELECT * FROM old)").fetchone()[0]
    only_old = con.sql("SELECT count(*) FROM (SELECT * FROM old EXCEPT ALL SELECT * FROM new)").fetchone()[0]
    print(f"{name}\n  rows new {n_new:,} / iceberg {n_old:,} / v2026-11-01 {ref[name]['row_count']:,}"
          f"\n  columns == iceberg: {cols_new == cols_old}; == v2026-11-01: {cols_new == cols_ref}"
          f"\n  key {key}: duplicated {dup}, null {nulls}"
          f"\n  rows only in new {only_new}, only in iceberg {only_old}")
    if cols_new != cols_ref:
        print("  column diff vs v2026-11-01:", sorted(set(cols_new) ^ set(cols_ref)))
    if only_new or only_old:
        k = ", ".join(key)
        print("  sample (only in new):", con.sql(f"SELECT * FROM (SELECT * FROM new EXCEPT ALL "
                                               f"SELECT * FROM old) ORDER BY {k} LIMIT 3").fetchall())
        print("  sample (only in iceberg):", con.sql(f"SELECT * FROM (SELECT * FROM old EXCEPT ALL "
                                                   f"SELECT * FROM new) ORDER BY {k} LIMIT 3").fetchall())
```
A table already in the shared lake (`signature_taxon`) runs the same script
from an empty directory with `CU_OPENALEX_LAKE_BACKEND=postgres` and nothing
else set. That is read-only.

### 5. Gate on a partial bundle
```python
out = Path(".../bundle"); publish.publish(publish.connect(), out, out.as_uri(), ONLY)
ref = Path(".../ref")   # v<date>/manifest.json with "tables" cut to ONLY
r = verify_release_over_release("bioconice", out, str(ref), gate.CONTRACTS, gate.UNCONTRACTED_TABLES)
```
`bioconice gate` itself refuses a partial bundle (every reference table must be
present) and a same-day reference (`2026-11-01` is not older than today's
build). Calling `verify_release_over_release` directly with a cut-down
reference manifest runs the same per-table checks.

### 6. Current upstream
Ingest the newest version into the same scratch lake (`run` with no
arguments), re-run step 4, and explain each difference as an upstream change:
ids added or dropped, versions restamped. Check the counts against the
contract's `row_count_bounds`.

### 7. PRs, not merged
- cdsci-lake: the source module. bioc-on-ice: repoints, contracts, tests, docs.
- Both marked "Step 7 — owner reviews before the production lake write".
- **Merge order:** cdsci-lake PR → owner-approved production ingest of the
  new sources → bump the cdsci-lake pin in bioc-on-ice → merge the bioc-on-ice
  PR. If the bioc-on-ice PR merges first, the next `bioconice publish` fails:
  `lake.hgnc.gene` does not exist in the production lake.
- Clean up: `rm -rf "$D"` (the step-7 scratch dir is left for review at
  `/data/davsean/tmp/bioconice-scratch-lake.Zp7Y8w`; TMPDIR cleans it after
  7 days).

## Surprises and gotchas

- **The release Worker answers Python's `urllib` with 403.** `curl` and DuckDB
  httpfs get the manifest. Download it with `curl -o` (or read it through
  DuckDB) rather than `urllib.request.urlopen`.
- **`duckdb_tables().estimated_size` is not a row count on DuckLake.**
  `lake.icite.metadata` showed 126,956,914, but `count(*)` is 41,172,752. Use
  `count(*)`; it returns in under a second on the shared lake.
- **Shared-lake GROUP BYs are slow.** A query batch with `GROUP BY` over
  `icite.metadata`/`ncbi_gene.gene_info` hit a 120 s tool timeout. Give the
  shell call an explicit timeout and keep shared-lake queries to counts and
  DESCRIBEs.
- **DuckDB versions differ.** cdsci-lake `origin/main` locks DuckDB 1.5.4 and
  bioc-on-ice 1.5.5. A scratch catalog written by 1.5.4 read fine from 1.5.5.
- **Don't switch branches in the main cdsci-lake checkout.** I briefly
  detached `~/Documents/git/cdsci-lake` to read `origin/main` and restored it
  straight away. Read with `git show origin/main:<path>` and write only in a
  worktree.
- **`main` was 2 commits behind `origin/main`** (ADR-0026 and the gate had
  merged). `git fetch` first; the worktree is cut from `origin/main`.
- **Complex Portal published 2026-10-17** since the Iceberg load
  (2026-01-09): 1,250 new complexes and a 30th file.
- **A lander-time invariant moves to the gate.** In Iceberg, a malformed
  participant token (NULL `participant_id`) failed the write on a required
  field. A release SELECT writes it, and the gate's `key_not_null` check on
  `(complex_ac, participant_id)` fails the release instead.
- **37 of 78 declared tables have no `bioc.license` property**, so
  `declared_contract` takes `license=` for those.
- **`lake.snapshots()` from the bioc-on-ice venv** fails when a timestamp is
  fetched to Python (`pytz` is not installed). Select `snapshot_id` only.
- **Each lake table keeps its bronze copy** under `$D/lake/raw/<source>/`, so
  a scratch run downloads into `$D`, not the shared raw store.

## What step 8's workflow must automate

1. Stop-switch check and source selection from the map (`#`, effort).
2. The cdsci-lake worktree, module skeleton from the template, registration,
   config URL, licence row, offline test; ruff and pytest.
3. Scratch lake creation with the ADR-0015 env, the same-version ingest (dated
   download, or Iceberg re-export for a rolling file), and the idempotent
   re-run.
4. The bioc-on-ice edits: `_<table>(src)` extraction, `RELEASE` with the
   frozen constants, `FROM_LAKE`, `declared_contract` entries, exemption-list
   removal, `release_matches_lander` test; pytest.
5. `equivalence.py` per table: a typed parity result with rows, columns, key
   and `EXCEPT ALL` counts. Pass means identical on the same version.
6. Partial publish and `verify_release_over_release` against the cut-down
   reference manifest.
7. The current-upstream run, with its diff summary for the PR body.
8. Two PRs with the merge-order note, and the per-source time and token
   record for the #183 experiment log.

Needs a human or the owner, not the workflow: tables with mixed `valid_from`
(#222), the OBO artifact choice, Ensembl assembly scope, the production lake
write, and the pin bump.
