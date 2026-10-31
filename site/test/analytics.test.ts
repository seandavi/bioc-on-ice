import { test } from "node:test";
import assert from "node:assert/strict";
import { logUsage } from "../src/analytics.ts";

async function logged(method: string, bytes: number) {
  const points: AnalyticsEngineDataPoint[] = [];
  const env = { USAGE: { writeDataPoint: (p: AnalyticsEngineDataPoint) => points.push(p) } };
  const req = new Request("https://data.bioconice.cancerdatasci.org/latest/a/b.parquet", {
    method,
    headers: { "cf-connecting-ip": "203.0.113.7", "user-agent": "duckdb/v1.4.0" },
  });
  await logUsage(env as any, req, "latest/a/b.parquet", 206, bytes);
  return points[0];
}

test("logUsage: a ranged GET logs the bytes sent", async () => {
  const p = await logged("GET", 8192);
  assert.deepEqual(p.indexes, ["latest/a/b.parquet"]);
  assert.equal(p.blobs![0], "latest/a/b.parquet");
  assert.equal(p.blobs![1], "duckdb/v1.4.0");
  assert.match(p.blobs![2] as string, /^[0-9a-f]{8}$/);
  assert.equal(p.blobs![3], "GET");
  assert.deepEqual(p.doubles!.slice(0, 2), [206, 8192]);
});

test("logUsage: HEAD logs 0 bytes, not the object size", async () => {
  const p = await logged("HEAD", 5_000_000);
  assert.equal(p.doubles![1], 0);
});
