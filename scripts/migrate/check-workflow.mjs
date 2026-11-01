// Offline check of .pi/workflows/migrate-source.js with pi-subagents' own validator and runner:
// every child is stubbed, so nothing launches. Run after editing the workflow:
//   node scripts/migrate/check-workflow.mjs
// PI_SUBAGENTS overrides where the pi-subagents package is installed.
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";

const pkg = process.env.PI_SUBAGENTS || `${process.env.HOME}/.pi/agent/npm/node_modules/pi-subagents`;
const { validateWorkflowScript, runWorkflowScript } = await import(`${pkg}/src/workflows/scripted-workflow.js`);
const script = readFileSync(new URL("../../.pi/workflows/migrate-source.js", import.meta.url), "utf8");

const agents = new Set(["worker", "reviewer"]);
const v = validateWorkflowScript(script, { agentNameError: (n) => (agents.has(n) ? undefined : `unknown agent ${n}`) });
assert.deepEqual(v.errors, []);

const tables = ["raw.x__y", "annotation.x__z"];
const same = tables.map((table) => ({ table, rows_new: 5, rows_iceberg: 5, rows_manifest: 5, columns_eq_iceberg: true,
  columns_eq_manifest: true, key_duplicated: 0, key_null: 0, only_new: 0, only_iceberg: 0, pass: true }));
const changes = (id) => ({ verdict: "changes", findings: [{ id, severity: "major", location: "l", problem: "p", fix: "f" }] });
const done = { status: "done", summary: "s" };

function run(overrides) {
  const out = {
    preflight: { status: "ready", reason: "r", tables, lake_schema: "x", reference_release: "2026-11-01", iceberg_version: "1", version_method: "m" },
    module: { ...done, lake_tables: ["lake.x.y"], key: ["k"], version_column: "x_version", checks: "pass" },
    "scratch-load": { ...done, version: "1", rows: { "lake.x.y": 5 }, idempotent: true },
    repoint: { ...done, frozen_release: "'2026.09'", contracts: [], checks: "pass" },
    equivalence: { ...done, tables: same, gate: "PASS" },
    upstream: { ...done, version: "2", tables: same, within_bounds: true, report: "r" },
    prs: { status: "done", lake_pr: "L", bioc_pr: "B" },
    "packet-r1": { ...done, dir: "d1" }, "packet-r2": { ...done, dir: "d2" },
    "review-r1": changes("r1-1"), "fix-r1": { ...done, responses: [], tables: same, gate: "PASS" },
    "review-r2": { verdict: "approve", findings: [] },
    ...overrides,
  };
  const keys = [];
  return runWorkflowScript({
    script, args: { source: "x" },
    launch: async (key, params) => {
      keys.push(key);
      assert.ok(!(params.model || "").includes("sonnet"));
      if (key.startsWith("review-")) assert.equal(params.model, "openai-codex/gpt-6.1-sol");
      return { key, ok: true, output: "", artifactPaths: [], structuredOutput: out[key] };
    },
    status: async (key) => ({ key, ok: true, output: "", artifactPaths: [] }),
  }).then((r) => ({ ...r.value, keys }));
}

let r = await run({});
assert.equal(r.status, "review-clean");
assert.equal(r.rounds[0].blocking.length, 1);
assert.deepEqual(r.keys, ["preflight", "module", "scratch-load", "repoint", "equivalence", "upstream",
  "prs", "packet-r1", "review-r1", "fix-r1", "packet-r2", "review-r2", "record"]);

r = await run({ "review-r2": changes("r2-1") });
assert.equal(r.status, "escalated");
assert.equal(r.unresolved[0].id, "r2-1");
assert.ok(!r.keys.includes("fix-r2"), "no fix after the last round");

r = await run({ equivalence: { ...done, tables: [{ ...same[0], only_iceberg: 1, pass: false }, same[1]], gate: "PASS" } });
assert.deepEqual([r.stage, r.status], ["equivalence", "check-failed"]);

r = await run({ equivalence: { ...done, tables: same.slice(1), gate: "PASS" } });
assert.equal(r.stage, "equivalence", "a missing table is not a pass");

r = await run({ equivalence: { ...done, tables: [same[0], same[0]], gate: "PASS" } });
assert.equal(r.stage, "equivalence", "a table reported twice does not stand in for another");

r = await run({ "fix-r1": { ...done, responses: [], tables: [{ ...same[0], columns_eq_manifest: false }, same[1]], gate: "PASS" } });
assert.equal(r.stage, "fix-r1", "a fix must keep parity");

r = await run({ preflight: { status: "paused", reason: "status:paused" } });
assert.deepEqual([r.stage, r.status, r.keys], ["preflight", "paused", ["preflight"]]);

r = await run({ preflight: { status: "ready", reason: "r", tables: [] } });
assert.deepEqual([r.stage, r.keys], ["preflight", ["preflight"]], "no tables is not ready");

r = await run({ "scratch-load": { ...done, version: "1", rows: {}, idempotent: false } });
assert.equal(r.stage, "scratch-load");

console.log("migrate-source.js: valid; clean, escalated, parity, missing-table, fix-parity, paused, no-tables and idempotence paths ok");
