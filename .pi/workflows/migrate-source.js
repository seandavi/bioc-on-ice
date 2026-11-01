// migrate-source: move ONE bioc-on-ice source from Iceberg to cdsci-lake (#183 step 9).
// Frozen at step 8 from what step 7 did by hand: docs/research/migration-log-step7.md is the
// recipe; docs/research/migrate-source-workflow.md says how to launch this and what it
// does not do. Changing this file is a gate change: owner approval (#183 rules).
//
// args: { source, issue? }
//   source: the bioc-on-ice lander module name (src/bioconice/<source>.py), e.g. "wikipathways".
//   issue:  the source's data-source issue, linked from the PRs (optional).
//
// Stages, each a child agent: preflight -> cdsci-lake module -> scratch load at the Iceberg
// version -> bioc-on-ice repoint -> equivalence + partial gate -> current upstream -> draft PRs
// -> review packet + sol review (<= 2 rounds, fix between) -> stop.
// The workflow never merges, never writes the production lake or Iceberg, and never runs
// publish/upload against R2: those stay with the orchestrator and the owner.
//
// Each repository gets its own worktree on branch migrate/<source>, cut from origin/main by
// preflight and kept across stages (pi's worktree:true removes its worktree after each child,
// so it cannot carry one branch through seven stages in two repositories).

const OPUS = "claude-bridge/claude-opus-5-5";
const HAIKU = "claude-bridge/claude-haiku-5-5";
const SOL = "openai-codex/gpt-6.1-sol";

const source = args.source;
if (!source || !/^[a-z0-9_]+$/.test(source)) throw new Error("args.source must be a lander module name, e.g. wikipathways");
const issue = args.issue || null;
const MAX_ROUNDS = 2;

const REPO = "seandavi/bioc-on-ice";
const LAKE_REPO = "seandavi/cdsci-lake";
const BRANCH = `migrate/${source}`;
const ROOT = `/home/davsean/Documents/git/worktrees/migrate-${source}`;
const W = `${ROOT}/bioc-on-ice`;   // bioc-on-ice worktree
const C = `${ROOT}/cdsci-lake`;    // cdsci-lake worktree
const T = `/data/davsean/tmp/migrate-${source}`; // scratch root: lakes, inputs, bundles, packets
const D = `${T}/lake`;             // scratch DuckLake at the Iceberg version
const D2 = `${T}/current`;         // scratch DuckLake at the current upstream version
const REF = `${T}/manifest-latest.json`;
const RECIPE = `${W}/docs/research/migration-log-step7.md`;
const MAP = `${W}/docs/research/lake-migration-map.md`;
const TITLE = `Migrate ${source} to cdsci-lake (#183)`;

const scratchEnv = (dir) => `cd ${dir} && export CU_OPENALEX_LAKE_BACKEND=local CU_OPENALEX_STORAGE_BASE_URI="file://${dir}" \\
  CU_OPENALEX_LAKE_CATALOG="${dir}/catalog.ducklake" CU_OPENALEX_LAKE_DATA_PREFIX=data \\
  CU_OPENALEX_DUCKDB_TEMP_DIRECTORY="${dir}/duckdb_tmp"`;

const COMMON = `
You are one stage of the migrate-source workflow (seandavi/bioc-on-ice#183): move the "${source}"
source from bioc-on-ice's Iceberg lander to a cdsci-lake source module plus release SELECTs.
The recipe is ${RECIPE} (written from step 7, HGNC and Complex Portal): read it before you act,
and follow it where this brief is silent. Read AGENTS.md in each repository you touch.
Paths: bioc-on-ice worktree ${W}, cdsci-lake worktree ${C} (both on branch ${BRANCH}),
scratch root ${T}, scratch lake at the Iceberg version ${D}, at the current upstream ${D2},
reference manifest ${REF}.
Hard rules, no exceptions:
- Never write the shared production lake: never set CU_OPENALEX_LAKE_BACKEND=postgres for a
  command that writes. Every lake write goes to a scratch dir under ${T} with this env:
  ${scratchEnv("<scratch dir>")}
- Never write Iceberg: no "bioconice ingest*", no lander ingest/land_raw/transform against the
  live catalog. Iceberg is read only through publish.ATTACH_LIVE (anonymous read).
- Never run "bioconice upload", "bioconice hf-push", scripts/release.sh or anything that writes
  R2 or Hugging Face. Never merge, never force-push, never "git stash", never touch main or the
  main checkouts (~/Documents/git/bioc-on-ice, ~/Documents/git/cdsci-lake): read origin/main
  with "git show origin/main:<path>".
- Work in the foreground; leave no background process running. Keep each long command under
  25 minutes and give shell calls an explicit timeout.
- Commit with: git -c user.name="Sean Davis" -c user.email=seandavi@gmail.com commit, with the
  trailer "Co-Authored-By: Claude <noreply@anthropic.com>".
- If something needs a decision this brief and the recipe do not settle, ask through
  contact_supervisor (reason need_decision) and wait for the reply; do not guess.
- Report honestly: a number you did not observe is not a result.`;

const status = (extra, required) => ({
  type: "object",
  required: ["status", "summary"].concat(required || []),
  properties: Object.assign({
    status: { type: "string", enum: ["done", "blocked"] },
    summary: { type: "string" },
  }, extra),
});
const strings = { type: "array", items: { type: "string" } };

// A child that reports "done" but fails the script's own check (tests, idempotence, parity,
// gate, bounds) stops as "check-failed".
function stop(stage, result, extra) {
  const out = result.structuredOutput || {};
  const state = !result.ok ? "failed" : out.status === "done" ? "check-failed" : out.status || "failed";
  return Object.assign({ source, stage, stopped: true, status: state,
    reason: out.summary || out.reason || result.error || "no structured output" }, extra || {});
}

// ---- 1. Preflight ----------------------------------------------------------------------
const pre = await runs.run("preflight", {
  label: `Preflight ${source}`,
  agent: "worker",
  model: HAIKU,
  outputSchema: {
    type: "object",
    required: ["status", "reason"],
    properties: {
      status: { type: "string", enum: ["ready", "paused", "not-next", "blocked"] },
      reason: { type: "string" },
      tables: strings,
      lake_schema: { type: "string" },
      reference_release: { type: "string" },
      iceberg_version: { type: "string" },
      version_method: { type: "string" },
    },
  },
  task: `${COMMON}

You are the PREFLIGHT stage. Change no tracked file. Check, in order, and stop at the first
failure with that status:
1. paused: "gh issue view 183 -R ${REPO} --json labels" lists status:paused.
2. not-next / blocked: read the map on origin/main
   (git -C ~/Documents/git/bioc-on-ice fetch -q; git -C ~/Documents/git/bioc-on-ice show
   origin/main:docs/research/lake-migration-map.md) and the bioc-on-ice origin/main
   src/bioconice/publish.py (FROM_LAKE). Take the "Proposed order for step 9" list. Skip
   wave A (repoint-only) and every source whose rows in the "Per table" table are not all
   effort "new source module": those are not this workflow's job. A source counts as migrated
   when its tables are in FROM_LAKE on origin/main, or in flight when an open PR titled
   "Migrate <source> to cdsci-lake" exists in ${REPO}. "${source}" must be the first source
   in that order that is neither: otherwise status not-next, naming the source that is next.
   Then status blocked if any of the source's rows has an effort other than "new source
   module", is marked mixed valid_from, or the map says it needs an owner decision: those
   need a human (recipe, "Needs a human or the owner").
   Tables: every published table of the source (its rows in "Per table"; check them against
   the "${source}" entries in src/bioconice/schemas.py and publish.TABLES). Do not count a
   branch the source writes into a cross-source table (annotation.identifier_mapping,
   resource.resource_relationship, annotation.interaction): those stay on Iceberg (map,
   "Cross-cutting findings").
3. blocked: ${ROOT} or ${T} already exists (a previous run's state; the orchestrator decides).
Then set up, and report blocked if any step fails:
- git -C ~/Documents/git/bioc-on-ice worktree add -b ${BRANCH} ${W} origin/main
- git -C ~/Documents/git/cdsci-lake fetch -q && git -C ~/Documents/git/cdsci-lake worktree add -b ${BRANCH} ${C} origin/main
- mkdir -p ${T}/inputs && curl -sfo ${REF} https://data.bioconice.cancerdatasci.org/latest/manifest.json
  (curl, not Python urllib: the release Worker answers urllib with 403). reference_release is
  its "release" field.
- iceberg_version and version_method: the "${source}" row of the manifest's "provenance" list
  (source_version, version_method).
- lake_schema: the cdsci-lake schema per cdsci-lake ADR-0026 (one schema per upstream source;
  usually "${source}").
Status ready only when every check passed and the setup is done.`,
});
const preOut = pre.structuredOutput || {};
const TABLES = preOut.tables || [];
// No tables would make every later "all tables pass" check vacuously true.
if (!pre.ok || preOut.status !== "ready" || !TABLES.length) return stop("preflight", pre);
const SCHEMA = preOut.lake_schema || source;
const facts = `Tables: ${TABLES.join(", ")}. cdsci-lake schema: ${SCHEMA}. Iceberg version: ${preOut.iceberg_version} (${preOut.version_method}). Reference release: ${preOut.reference_release} (${REF}).`;

// ---- 2. cdsci-lake source module -------------------------------------------------------
const mod = await runs.run("module", {
  label: `cdsci-lake module ${source}`,
  agent: "worker",
  model: OPUS,
  cwd: C,
  outputSchema: status({
    lake_tables: strings,
    key: strings,
    version_column: { type: "string" },
    changed_files: strings,
    checks: { type: "string", enum: ["pass", "fail"] },
  }, ["lake_tables", "checks"]),
  task: `${COMMON}
${facts}

You are the MODULE stage, in ${C}. Recipe step 1. Port the EL half of
${W}/src/bioconice/${source}.py (its land_raw) to src/cdsci/lake/sources/${SCHEMA}/
{__init__,__main__,ingest}.py, with sources/hgnc and sources/complexportal as the templates
(read both, and their tests):
- One lake table per raw Iceberg table, named for one row (ADR-0026), every column as the raw
  table has it minus landed_in, plus the raw table's version column: the same names and types
  (not all VARCHAR when raw has a typed column), so SELECT * reproduces raw. Copy the lander's
  dialect and parsing verbatim.
- The input's shape is checked on the whole input, not just a limited load's rows, before
  anything lands: a CSV/TSV header whole; for a headerless or ragged format (GMT, JSON lines)
  each record's minimal structure. What the values mean is bioc-on-ice's to check.
- curate(con, ..., version, *, target, limit) -> upsert(key=..., prune=None if limit else "true");
  the version column takes part in the change compare, so a new version restamps every row and
  the table holds one version. upsert returns the table's row count, not the rows written.
  Key: the raw table's row identity without the version column: schemas.py's business_key, or,
  when raw declares none, the columns its docs say identify a row (e.g. file_name, line_number).
- ingest(...) inside ops.run(...); download into raw_dir("${SCHEMA}") keyed by version, and take a
  version argument that fetches that upstream version when upstream keeps dated copies.
- Register Source(...) in ops.SOURCES, add the URL to config.Settings, add a row to
  docs/data-licenses.md (quote the upstream licence page and the date you read it).
- Offline test (fixture written at test time, in the real dialect): lands whole, dialect edge
  cases, idempotent re-run makes no snapshot, a limited load never prunes, a new version prunes
  and restamps, a malformed input raises before landing (also beyond a limit) and keeps the
  table, a header-only/empty input is refused and the table kept. The test Settings pin
  lake_backend="local" and a catalog under tmp_path: Settings(storage_base_uri=...) alone
  inherits CU_OPENALEX_LAKE_BACKEND=postgres from the environment or a .env and would write the
  shared lake.
- uv sync --all-extras && uv run ruff check src/ tests/ && uv run pytest -q. Commit.
Report the lake tables (lake.<schema>.<table>), key, version column, changed files, checks.`,
});
const modOut = mod.structuredOutput || {};
if (!mod.ok || modOut.status !== "done" || modOut.checks !== "pass") return stop("module", mod);

// ---- 3. Scratch load at the Iceberg version, plus an idempotent re-run ------------------
const load = await runs.run("scratch-load", {
  label: `Scratch load ${source} at the Iceberg version`,
  agent: "worker",
  model: OPUS,
  cwd: C,
  outputSchema: status({
    version: { type: "string" },
    how: { type: "string", enum: ["dated-download", "iceberg-reexport"] },
    rows: { type: "object", additionalProperties: { type: "integer" } },
    idempotent: { type: "boolean" },
    seconds: { type: "number" },
  }, ["version", "rows", "idempotent"]),
  task: `${COMMON}
${facts}
Lake tables: ${(modOut.lake_tables || []).join(", ")}.

You are the SCRATCH-LOAD stage. Recipe step 2. mkdir -p ${D}, then with the scratch env for
${D}, load the source at the Iceberg version ${preOut.iceberg_version}:
- Upstream keeps that version (a dated release or archive): download it by version:
  uv run --project ${C} python -m cdsci.lake.sources.${SCHEMA} run --version <version as the module takes it>
- A rolling file that no longer exists at that version: re-export the raw Iceberg rows of that
  version in the upstream dialect into ${T}/inputs/ (recipe step 2 shows the HGNC COPY) and
  land the file with --file.
Then run the same command again: it must report idempotent (no change, no new snapshot).
Change no tracked file; if the module needs a fix, stop with status blocked and say what.
Report the version as landed, how, the row count of each lake table (count(*)), whether the
re-run was idempotent, and the wall-clock seconds of the first load.`,
});
const loadOut = load.structuredOutput || {};
if (!load.ok || loadOut.status !== "done" || !loadOut.idempotent) return stop("scratch-load", load);

// ---- 4. bioc-on-ice repoint ------------------------------------------------------------
const repoint = await runs.run("repoint", {
  label: `bioc-on-ice repoint ${source}`,
  agent: "worker",
  model: OPUS,
  cwd: W,
  outputSchema: status({
    frozen_release: { type: "string" },
    contracts: strings,
    changed_files: strings,
    checks: { type: "string", enum: ["pass", "fail"] },
  }, ["frozen_release", "checks"]),
  task: `${COMMON}
${facts}
Lake tables (key ${(modOut.key || []).join(", ")}, version column ${modOut.version_column}): ${(modOut.lake_tables || []).join(", ")}.

You are the REPOINT stage, in ${W}. Recipe step 3; src/bioconice/hgnc.py and complexportal.py
(and their tests) are the templates.
- In src/bioconice/${source}.py, move each transform SELECT into a _<table>(src) function that
  both transform() and RELEASE call (one SQL text). RELEASE = {published table: SELECT over
  lake.${SCHEMA}.<table>}. Raw tables: SELECT *, <frozen> AS landed_in FROM lake.<schema>.<table>.
  Derived (valid_from/valid_to) tables: SELECT *, <frozen> AS valid_from, NULL::VARCHAR AS valid_to.
- The frozen value (#222) is FROZEN_RELEASE, the single landed_in / valid_from value of the
  source's current rows. Read it from Iceberg (SELECT DISTINCT over the current rows of each
  table through publish.ATTACH_LIVE): if any table has more than one value, stop with status
  blocked (mixed valid_from needs the owner, recipe decision 2). Comment it as hgnc.py does.
- src/bioconice/publish.py: FROM_LAKE gains **${source}.RELEASE.
- src/bioconice/gate.py: BUNDLE gains declared_contract(...) for every table (raw tables keyed
  without their version column; license= where schemas.py declares none; references= only where
  a join is cheap and real), and each table's line leaves UNCONTRACTED_TABLES (the #208 list).
- tests/test_${source}.py: a test that calls the release_matches_lander fixture
  (tests/conftest.py) with the lake table -> raw table map, RELEASE and the frozen value as a
  plain string (FROZEN_RELEASE without its SQL quotes, e.g. "2026.09"; not the test's REL).
- docs/research/lake-migration-map.md: the source's rows read "lake (migrate/${source})" in
  "reads now", with the lake table in the cdsci-lake column.
- uv run pytest -q. Commit.
Report FROZEN_RELEASE (as the SQL literal), the contract keys ("table: key"), changed files and checks.`,
});
const repOut = repoint.structuredOutput || {};
if (!repoint.ok || repOut.status !== "done" || repOut.checks !== "pass") return stop("repoint", repoint);

// ---- 5. Equivalence (typed) and the partial-bundle gate --------------------------------
const tableResult = {
  type: "object",
  required: ["table", "rows_new", "rows_iceberg", "rows_manifest", "columns_eq_iceberg",
    "columns_eq_manifest", "key_duplicated", "key_null", "only_new", "only_iceberg", "pass"],
  properties: {
    table: { type: "string" }, rows_new: { type: "integer" }, rows_iceberg: { type: "integer" },
    rows_manifest: { type: "integer" }, columns_eq_iceberg: { type: "boolean" },
    columns_eq_manifest: { type: "boolean" }, key_duplicated: { type: "integer" },
    key_null: { type: "integer" }, only_new: { type: "integer" }, only_iceberg: { type: "integer" },
    pass: { type: "boolean" },
  },
};
const eq = await runs.run("equivalence", {
  label: `Equivalence and gate ${source}`,
  agent: "worker",
  model: OPUS,
  cwd: W,
  outputSchema: status({
    tables: { type: "array", items: tableResult },
    gate: { type: "string", enum: ["PASS", "FAIL"] },
    fixes: strings,
  }, ["tables", "gate"]),
  task: `${COMMON}
${facts}

You are the EQUIVALENCE stage. Recipe steps 4 and 5. With the scratch env for ${D}:
  uv run --project ${W} python ${W}/scripts/migrate/equivalence.py ${REF} ${TABLES.join(" ")} > ${T}/equivalence.json
  ${W}/scripts/migrate/partial-gate.sh ${W} ${REF} ${T}/gate ${TABLES.join(" ")} 2>&1 | tee ${T}/gate.txt
Pass means: every table "pass" true (same columns and types as Iceberg and the manifest, key
unique and not null, 0 rows only on either side) and the gate prints PASS.
If a table differs, find the cause from the samples on stderr. A bug in the release SELECT or
the lake module: fix it (commit in the worktree it belongs to; for a module fix, reload ${D} as
the recipe's step 2 does) and re-run both commands; list each fix in fixes. A difference that is
not a bug (the lake cannot hold what Iceberg does): stop with status blocked and explain.
Report the final run: the JSON objects of ${T}/equivalence.json verbatim as tables, and the
gate verdict.`,
});
// Parity is judged here from the typed numbers, not from the child's verdict.
function parity(results) {
  return (results || []).length === TABLES.length && TABLES.every((name) => results.some((t) =>
    t.table === name && t.pass && t.columns_eq_iceberg && t.columns_eq_manifest &&
    t.key_duplicated === 0 && t.key_null === 0 && t.only_new === 0 && t.only_iceberg === 0 &&
    t.rows_new === t.rows_iceberg));
}
const eqOut = eq.structuredOutput || {};
const eqTables = eqOut.tables || [];
if (!eq.ok || eqOut.status !== "done" || !parity(eqTables) || eqOut.gate !== "PASS") return stop("equivalence", eq, { tables: eqTables, gate: eqOut.gate || null });

// ---- 6. Current upstream ---------------------------------------------------------------
const up = await runs.run("upstream", {
  label: `Current upstream ${source}`,
  agent: "worker",
  model: OPUS,
  cwd: C,
  outputSchema: status({
    version: { type: "string" },
    tables: { type: "array", items: tableResult },
    within_bounds: { type: "boolean" },
    report: { type: "string" },
  }, ["version", "tables", "within_bounds", "report"]),
  task: `${COMMON}
${facts}

You are the UPSTREAM stage. Recipe step 6. mkdir -p ${D2}; with the scratch env for ${D2}, load
the newest upstream version (the module's run with no version), then re-run the equivalence
script against ${D2}:
  uv run --project ${W} python ${W}/scripts/migrate/equivalence.py ${REF} ${TABLES.join(" ")} > ${T}/upstream.json
(it exits 1 when rows differ; that is expected here). Leave ${D} untouched: it stays at the
Iceberg version for re-runs. Explain every difference as an upstream change (ids added or
dropped, edits, versions restamped), with counts and one or two concrete examples, and check
each table's new row count against its contract's row_count_bounds (default 0.9 to 1.5 of the
reference). When the version is stamped into row values (a file name, a packed set name),
every raw row differs: compare again with it stripped, to tell real upstream changes from
restamps. Columns must still equal the manifest's; a column change is status blocked.
Write the explanation to ${T}/upstream.md (it goes into the PR body). Change no tracked file.
Report the version, the JSON objects verbatim as tables, within_bounds, and report = ${T}/upstream.md.`,
});
const upOut = up.structuredOutput || {};
if (!up.ok || upOut.status !== "done" || !upOut.within_bounds) return stop("upstream", up);

// ---- 7. Draft PRs ----------------------------------------------------------------------
const evidence = [
  "| table | rows (scratch = Iceberg) | EXCEPT ALL both ways | columns vs Iceberg / manifest | key dup / null | rows on current upstream |",
  "|---|---:|---|---|---|---:|",
].concat(eqTables.map((t) => {
  const u = (upOut.tables || []).find((x) => x.table === t.table) || {};
  return `| ${t.table} | ${t.rows_new} | ${t.only_new} / ${t.only_iceberg} | ${t.columns_eq_iceberg} / ${t.columns_eq_manifest} | ${t.key_duplicated} / ${t.key_null} | ${u.rows_new === undefined ? "?" : u.rows_new} |`;
})).join("\n");
const mergeOrder = `**Merge order:** the cdsci-lake PR -> owner-approved production ingest (python -m cdsci.lake.sources.${SCHEMA} run, against the production lake) -> bump the cdsci-lake pin in bioc-on-ice -> merge the bioc-on-ice PR. Merged first, the bioc-on-ice PR breaks the next bioconice publish: lake.${SCHEMA}.* does not exist in the production lake.`;

const prs = await runs.run("prs", {
  label: `Draft PRs ${source}`,
  agent: "worker",
  model: HAIKU,
  cwd: W,
  outputSchema: {
    type: "object",
    required: ["status", "lake_pr", "bioc_pr"],
    properties: {
      status: { type: "string", enum: ["done", "blocked"] },
      lake_pr: { type: "string" },
      bioc_pr: { type: "string" },
    },
  },
  task: `${COMMON}

You are the PR stage. Change no code. Push both branches (git -C ${C} push -u origin ${BRANCH};
git -C ${W} push -u origin ${BRANCH}), then open BOTH as DRAFTS, cdsci-lake first, writing each
body to a file under ${T} and passing --body-file:
1. gh pr create -R ${LAKE_REPO} --draft --base main --head ${BRANCH} --title "${TITLE}: source ${SCHEMA}"
   Body: first line "Part of ${REPO}#183${issue ? ` (${REPO}#${issue})` : ""}, opened by .pi/workflows/migrate-source.js. Draft: the orchestrator does not merge it before the production ingest."
   Then what the module lands (module stage summary: ${modOut.summary}; lake tables ${(modOut.lake_tables || []).join(", ")}; key ${(modOut.key || []).join(", ")}; version column ${modOut.version_column}),
   the offline tests, the scratch evidence table below, and the merge order. Say that nothing was written to the shared lake.
2. gh pr create -R ${REPO} --draft --base main --head ${BRANCH} --title "${TITLE}"
   Body: first line "Part of #183${issue ? `, #${issue}` : ""}, opened by .pi/workflows/migrate-source.js. Companion: <the cdsci-lake PR URL>."
   Then edit the cdsci-lake PR body (gh pr edit --body-file) so it links this PR as its companion.
   bioc-on-ice body, continued (repoint stage summary: ${repOut.summary}): the repoints (${TABLES.join(", ")}) and FROZEN_RELEASE ${repOut.frozen_release} (#222);
   the gate change (contracts ${(repOut.contracts || []).join("; ")}, removed from UNCONTRACTED_TABLES;
   tick these in #208 on merge: ${TABLES.join(", ")}) needs owner approval; the evidence table;
   "Partial-bundle gate: PASS" with the bundle tables; the current-upstream section copied from ${T}/upstream.md;
   the merge order.
Evidence table (paste as is):
${evidence}
Merge order (paste as is into both bodies):
${mergeOrder}
Report both PR URLs.`,
});
const prOut = prs.structuredOutput || {};
if (!prs.ok || prOut.status !== "done") return stop("prs", prs);

// ---- 8. Review packet + sol review, <= 2 rounds, fixing between ------------------------
const CHECKLIST = `Review checklist (answer every item; a "no" is a finding):
1. No path writes the shared lake (CU_OPENALEX_LAKE_BACKEND=postgres), Iceberg, R2 or Hugging Face.
2. cdsci-lake module: the header (or record shape) is checked whole before landing; the dialect
   is the lander's verbatim; prune only on full loads (limit never prunes); the version column is
   in the change compare (one version per table); the shared upsert's empty-load guard is not
   bypassed; registered in ops.SOURCES, URL in Settings, licence row with a dated quote.
3. cdsci-lake tests cover: lands whole, dialect edge cases, idempotent re-run, limited load,
   new version prunes and restamps, malformed input raises, empty input refused, table kept.
4. bioc-on-ice: one SQL text (transform() and RELEASE call the same _<table>(src)); FROZEN_RELEASE
   is the Iceberg value and valid_to is NULL (#222); FROM_LAKE gains the source; contract keys are
   the real row keys (raw tables without the version column); the tables left UNCONTRACTED_TABLES;
   the release_matches_lander test exercises every RELEASE entry.
5. Evidence: every table 0 / 0 EXCEPT ALL on the same version, columns equal Iceberg and the
   manifest, key unique and not null; the partial gate passed; the current-upstream differences
   are explained as upstream changes and within the contract bounds.
6. PRs are drafts and both carry the merge order; the bioc-on-ice PR flags the gate change.`;

const reviewSchema = {
  type: "object",
  required: ["verdict", "findings"],
  properties: {
    verdict: { type: "string", enum: ["approve", "changes"] },
    findings: {
      type: "array",
      items: {
        type: "object",
        required: ["id", "severity", "location", "problem", "fix"],
        properties: {
          id: { type: "string" },
          severity: { type: "string", enum: ["blocker", "major", "minor", "nit"] },
          location: { type: "string" },
          problem: { type: "string" },
          fix: { type: "string" },
        },
      },
    },
  },
};

function packet(round, prior) {
  const dir = `${T}/review-r${round}`;
  return runs.run(`packet-r${round}`, {
    label: `Review packet r${round} ${source}`,
    agent: "worker",
    model: HAIKU,
    cwd: W,
    outputSchema: status({ dir: { type: "string" } }, ["dir"]),
    task: `${COMMON}

You are the PACKET stage, round ${round}. The reviewer has no shell, so it reads only files you put
in ${dir} (a new directory: never reuse an earlier round's). Change no tracked file. Write:
- ${dir}/cdsci-lake.diff: git -C ${C} diff origin/main...${BRANCH}
- ${dir}/bioc-on-ice.diff: git -C ${W} diff origin/main...${BRANCH}
- ${dir}/files/cdsci-lake/<path> and ${dir}/files/bioc-on-ice/<path>: the full current text of
  every file either diff touches (git diff --name-only), plus AGENTS.md of each repository and
  ${RECIPE}.
- ${dir}/context/: unchanged files the checklist needs: cdsci-lake src/cdsci/lake/connect.py
  (the shared upsert and its empty-load guard), src/cdsci/lake/config.py (Settings) and
  sources/hgnc/ingest.py (the template); bioc-on-ice tests/conftest.py (release_matches_lander),
  src/bioconice/hgnc.py (the template) and scripts/migrate/ (the evidence scripts).
- ${dir}/evidence/: copies of ${T}/equivalence.json, ${T}/gate.txt, ${T}/upstream.json,
  ${T}/upstream.md, and both PR bodies (gh pr view <url> --json body -q .body):
  ${prOut.lake_pr} and ${prOut.bioc_pr}.
- ${dir}/README.md: what the change is (one paragraph), the file list with what each part is
  for, and this checklist verbatim:
${CHECKLIST}
${prior ? `- ${dir}/prior-round.json: ${JSON.stringify(prior)}` : ""}
Report dir.`,
  });
}

function review(round, prior) {
  const dir = `${T}/review-r${round}`;
  return runs.run(`review-r${round}`, {
    label: `Sol review r${round} ${source}`,
    agent: "reviewer",
    model: SOL,
    cwd: dir,
    outputSchema: reviewSchema,
    task: `You are a READ-ONLY reviewer (round ${round} of at most ${MAX_ROUNDS}) of a migration of the
"${source}" source from bioc-on-ice's Iceberg lander to cdsci-lake (seandavi/bioc-on-ice#183).
Everything you need is in ${dir}: start with ${dir}/README.md and its checklist, then the two
diffs, the full files under files/, the unchanged files they rely on under context/, and
evidence/. Use only paths under ${dir}. You cannot run tests; say which ones the orchestrator
should run if it matters.
${prior ? `${dir}/prior-round.json has the previous round's findings and the fixer's responses: judge each fix itself, not just whether the symptom is gone; a declined finding you disagree with stays open at its severity.` : ""}
Severity: blocker (wrong or unsafe), major (must fix before merge), minor, nit. Ids
"r${round}-<n>". verdict is "changes" only with at least one blocker or major. Zero findings is a
valid review; do not pad.`,
  });
}

const blocking = (r) => ((r.structuredOutput || { findings: [{ id: `r-error`, severity: "major", location: "-", problem: `review failed: ${r.error || "no structured output"}`, fix: "re-run the review" }] }).findings || [])
  .filter((f) => f.severity === "blocker" || f.severity === "major");
const minors = (r) => ((r.structuredOutput || {}).findings || []).filter((f) => f.severity === "minor" || f.severity === "nit");

const rounds = [];
let prior = null;
let open = [];
let notes = [];
for (let round = 1; round <= MAX_ROUNDS; round++) {
  const p = await packet(round, prior);
  if (!p.ok || (p.structuredOutput || {}).status !== "done") return stop(`packet-r${round}`, p, { prs: prOut });
  const r = await review(round, prior);
  open = blocking(r);
  notes = notes.concat(minors(r));
  rounds.push({ round, verdict: (r.structuredOutput || {}).verdict || "failed",
    blocking: open.map((f) => `${f.id} (${f.severity}) ${f.location}: ${f.problem}`) });
  if (!open.length || round === MAX_ROUNDS) break;

  const fix = await runs.run(`fix-r${round}`, {
    label: `Fix review r${round} ${source}`,
    agent: "worker",
    model: OPUS,
    cwd: W,
    outputSchema: status({
      responses: {
        type: "array",
        items: {
          type: "object",
          required: ["id", "action"],
          properties: { id: { type: "string" }, action: { type: "string", enum: ["fixed", "declined"] }, note: { type: "string" } },
        },
      },
      tables: { type: "array", items: tableResult },
      gate: { type: "string", enum: ["PASS", "FAIL"] },
    }, ["responses", "tables", "gate"]),
    task: `${COMMON}
${facts}

You are the FIX stage after review round ${round}. Address these findings:
${JSON.stringify(open, null, 2)}
Fix each at its root (in ${C} or ${W}), or decline it with a one-line reason if it contradicts the
recipe or AGENTS.md. Re-run the module's checks (ruff, pytest) or bioc-on-ice's pytest for what
you touched. After a module fix, re-run the scratch load in ${D} (and in ${D2} with the module's
plain run): idempotent unless the fix changes landed rows, which then needs explaining. Always re-run
the equivalence script and partial-gate.sh against ${D} exactly as the EQUIVALENCE stage did
(overwriting ${T}/equivalence.json and ${T}/gate.txt). Commit, push both branches
(git push, no force), and update the PR bodies if a number in them changed:
${prOut.lake_pr} ${prOut.bioc_pr}
Report every finding id in responses, the equivalence JSON objects verbatim as tables, and the gate verdict.`,
  });
  const fixOut = fix.structuredOutput || {};
  const fixTables = fixOut.tables || [];
  if (!fix.ok || fixOut.status !== "done" || !parity(fixTables) || fixOut.gate !== "PASS") return stop(`fix-r${round}`, fix, { prs: prOut, rounds });
  prior = { findings: open, responses: fixOut.responses || [] };
  rounds[rounds.length - 1].responses = prior.responses;
}

// ---- 9. Record the review on the PR and stop -------------------------------------------
const escalate = open.length > 0;
await runs.run("record", {
  label: `Record review ${source}`,
  agent: "worker",
  model: HAIKU,
  cwd: W,
  task: `${COMMON}

You are the RECORD stage. Change no code. Post one comment on ${prOut.bioc_pr}
(gh pr comment <url> --body-file <file under ${T}>):
"migrate-source review record" then, per round, its verdict, each blocking finding and how it
was answered (fixed or declined, with the note): ${JSON.stringify(rounds)}.
${escalate ? `Then "Unresolved after ${MAX_ROUNDS} rounds, escalated to the orchestrator:" and one bullet per finding: ${JSON.stringify(open)}.` : "Then: no open blocker or major finding."}
${notes.length ? `Then "Minor/nit findings, not fixed:" and one bullet per finding: ${JSON.stringify(notes)}.` : ""}
Leave both PRs as drafts.`,
});

return {
  source,
  stopped: false,
  status: escalate ? "escalated" : "review-clean",
  prs: { lake: prOut.lake_pr, bioc: prOut.bioc_pr },
  tables: TABLES,
  frozen_release: repOut.frozen_release,
  equivalence: eqTables,
  gate: eqOut.gate,
  upstream: { version: upOut.version, report: upOut.report },
  rounds,
  unresolved: open,
  notes: notes.length,
  next: "orchestrator: owner approval of the gate change, production ingest, pin bump, then mark ready and merge in the stated order",
};
