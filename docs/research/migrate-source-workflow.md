# The migrate-source workflow (#183 step 8)

**Status**: frozen at step 8; owner approval required to change it (#183 rules).
`.pi/workflows/migrate-source.js` is a pi-subagents scripted workflow. It moves
**one** source from its bioc-on-ice Iceberg lander to a cdsci-lake source module
plus release SELECTs. It was written from what step 7 did by hand
([`migration-log-step7.md`](migration-log-step7.md), the recipe its prompts
cite) and dry-run on WikiPathways (below).

## Launch

From a clean bioc-on-ice checkout at `origin/main` (the orchestrator's
`bioc-on-ice-orch`), after this file's PR has merged, so the branches it cuts
carry `scripts/migrate/`:

```js
subagent({ workflow: "./.pi/workflows/migrate-source.js", async: true,
           args: { source: "gwas_catalog" } })
```

`source` is the lander module name (`src/bioconice/<source>.py`); `issue`, the
source's data-source issue, is optional. Launch it `async: true`, because a
stage may ask the orchestrator a question through `contact_supervisor`. After
editing the workflow, run `node scripts/migrate/check-workflow.mjs`. It runs
pi-subagents' own validator, then the script with every child stubbed, and
checks the clean, escalated, parity-failure, paused and idempotence paths.

## What it does

| # | stage (key) | model | where | result the script checks |
|---|---|---|---|---|
| 1 | `preflight` | haiku | orchestrator cwd | no `status:paused` on #183; the source is the first not yet migrated or in flight in the map's step-9 order (skipping wave A and non-module sources); every table is "new source module", none with mixed `valid_from`; creates the worktrees and scratch root; downloads `latest/manifest.json` |
| 2 | `module` | opus | cdsci-lake worktree | the module, its registration, licence row and offline tests; ruff and pytest pass |
| 3 | `scratch-load` | opus | cdsci-lake worktree | a load at the Iceberg version into `<scratch>/lake`, with an idempotent re-run |
| 4 | `repoint` | opus | bioc-on-ice worktree | `_<table>(src)` shared SQL, `RELEASE` with the frozen #222 columns, `FROM_LAKE`, contracts out of `UNCONTRACTED_TABLES`, `release_matches_lander` test, map rows; pytest passes |
| 5 | `equivalence` | opus | bioc-on-ice worktree | `scripts/migrate/equivalence.py`, a typed result per table that the script itself judges (0 / 0 `EXCEPT ALL`, same columns as Iceberg and the manifest, key unique and not null, every table present); `scripts/migrate/partial-gate.sh` prints PASS |
| 6 | `upstream` | opus | cdsci-lake worktree | the current upstream in a second scratch lake (`<scratch>/current`); differences explained as upstream changes and within the contract bounds |
| 7 | `prs` | haiku | bioc-on-ice worktree | both PRs open as **drafts**, cdsci-lake first, with the evidence table and the merge order |
| 8 | `packet-rN`, `review-rN`, `fix-rN` | haiku, sol, opus | packet in `<scratch>/review-rN` (new per round) | at most 2 sol rounds (`openai-codex/gpt-6.1-sol`, agent `reviewer`, which has no shell); a fix between rounds re-runs equivalence and the gate |
| 9 | `record` | haiku | bioc-on-ice worktree | the review record (and any unresolved findings) as a PR comment |

State: worktrees `~/Documents/git/worktrees/migrate-<source>/{bioc-on-ice,cdsci-lake}`
on branch `migrate/<source>`, and a scratch root `/data/davsean/tmp/migrate-<source>/`
holding both scratch lakes (ADR-0015 local backend), the evidence files, the PR
bodies and the review packets. It uses persistent worktrees instead of pi's
`worktree: true`, which removes its worktree after each child and so cannot carry
one branch through seven stages in two repositories. A run that finds either
path already there stops (`blocked`); the orchestrator removes them or resumes
the stopped run (`subagent({ action: "resume", id })`).

The return value says `review-clean`, `escalated` (blocking findings remain after
round 2: the orchestrator decides), or `stopped: true` with the stage and why.
`check-failed` means a child reported done but failed the script's own check.

## What it does not do

It never merges, never writes the production lake or Iceberg, never runs
`bioconice upload`, `hf-push` or `release.sh`, and never edits #183 or #208. These
stay with the orchestrator and the owner: owner approval of the gate change,
the production ingest, the cdsci-lake pin bump, marking the PRs ready, merging in
the stated order, ticking #208, and the experiment-log entry (the workflow trace has each
child's duration; token usage was not checked to be there). Sources that are not new source modules
(wave A, small adapters such as `raw.bugsigdb__full_dump`), mixed `valid_from`
(#222), OBO and Ensembl need a human. Preflight refuses them.

## Dry run: WikiPathways, 2026-11-01

The stages were executed by hand from the workflow's own prompts, because
`subagent` is not available inside a subagent. The sol reviews ran headless
(`pi -p --model openai-codex/gpt-6.1-sol --tools read,grep,find,ls`) with the
packet layout and prompt the workflow builds. Preflight picked WikiPathways, the
first wave-B source in the map. Scratch lakes only.

- Draft PRs: seandavi/cdsci-lake#140 (source `wikipathways`, `lake.wikipathways.gene_set`)
  and seandavi/bioc-on-ice#228 (3 tables repointed).
- **Equivalence** at the Iceberg version (20260910): `raw.wikipathways__gmt` 1,957,
  `annotation.wikipathways__pathway` 1,957 and `annotation.wikipathways__gene_pathway`
  76,046 rows. In each table, scratch equals Iceberg equals the manifest, with
  0 / 0 `EXCEPT ALL` and identical columns. **Partial gate: PASS**, including
  `pathway_id` join coverage.
- **Current upstream** (20261010): 1,956 pathways. `WP191` (yeast) was withdrawn upstream
  (1 pathway row, 7 gene rows); every raw row is restamped with the date.
- **Review**: round 1 found a blocker and a major, both real; round 2 approved. The
  blocker: the cdsci-lake test `Settings` inherit `CU_OPENALEX_LAKE_BACKEND`, so
  `postgres` in the environment would point fixture writes at the shared lake.
  The same pattern is in the existing source tests on cdsci-lake `origin/main`,
  which needs a repo-wide fix. The major: the GMT record shape was not checked.
- Wall-clock: scratch load 2.7 s; equivalence 4 s; partial gate 8 s; each sol round under 2 min.

### Prompt fixes the dry run forced

1. **Script bug.** `partial-gate.sh` passed the manifest after `--args`, where jq takes
   it as a string. It now reads the manifest from stdin.
2. **Module key.** Raw tables often declare no business key (WikiPathways). The
   key is now the row identity the column docs name, minus the version column.
3. **Module types.** The lake table keeps raw's types (`line_number` INTEGER), not
   step 7's all-VARCHAR, so `SELECT *` reproduces raw.
4. **Module shape check (review r1-2).** Headerless formats check each record's
   shape on the whole input, beyond any limit, before landing.
5. **Test isolation (review r1-1).** Test `Settings` pin `lake_backend="local"` and
   a tmp catalog.
6. **`upsert` returns the table's row count**, not rows written. The first test
   assumed otherwise.
7. **Frozen value in the test.** `release_matches_lander` takes `"2026.09"`, not
   the test's `REL`.
8. **Upstream diff.** When the version is stamped into values (file names,
   packed names), every raw row differs. The stage now compares with it
   stripped.
9. **PR links.** The cdsci-lake PR is opened first, so its body is edited
   afterwards to link the bioc-on-ice PR.
10. **Packet context.** The reviewer needed unchanged files to answer the
    checklist: `connect.py` for the empty-load guard, `config.py` for
    `Settings`, the conftest fixture and the evidence scripts. These now go
    under `context/`.
11. **Preflight order.** Skip non-module sources when finding "next", so a
    small adapter (`raw.bugsigdb__full_dump`) does not block the queue behind it.
12. **Stop status.** A child that says done but fails the script's check now
    stops as `check-failed`, not `done`.
13. **Fix stage.** After a module fix, it re-runs both scratch loads.

Found by `check-workflow.mjs` rather than the dry run: a preflight that returns
no tables would make every later "all tables pass" check vacuously true, so it
now stops the run. Parity is judged per named table, so a table reported twice
cannot stand in for a missing one.
