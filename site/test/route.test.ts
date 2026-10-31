import { test } from "node:test";
import assert from "node:assert/strict";
import { permanentRedirect, route } from "../src/route.ts";

const at = (u: string) => route(new URL(u));

test("site host: docs everywhere but /data", () => {
  assert.deepEqual(at("https://bioconice.cancerdatasci.org/"), { kind: "docs" });
  assert.deepEqual(at("https://bioconice.cancerdatasci.org/guides/x/"), { kind: "docs" });
  assert.deepEqual(at("https://bioconice.cancerdatasci.org/releases"), { kind: "docs" });
});

test("site host: /data/* strips the prefix", () => {
  const data = (key: string) => ({ kind: "data", key, base: "/data" });
  assert.deepEqual(at("https://bioconice.cancerdatasci.org/data"), data(""));
  assert.deepEqual(at("https://bioconice.cancerdatasci.org/data/"), data(""));
  assert.deepEqual(
    at("https://bioconice.cancerdatasci.org/data/latest/a%20b.parquet"),
    data("latest/a b.parquet"),
  );
});

test("data host: bucket at /", () => {
  const data = (key: string) => ({ kind: "data", key, base: "" });
  assert.deepEqual(at("https://data.bioconice.cancerdatasci.org/"), data(""));
  assert.deepEqual(
    at("https://data.bioconice.cancerdatasci.org/latest/manifest.json"),
    data("latest/manifest.json"),
  );
  // no /data alias here: a top-level `data/` key stays a key
  assert.deepEqual(
    at("https://data.bioconice.cancerdatasci.org/data/x"),
    data("data/x"),
  );
});

test("malformed percent-escape is a bad request, not a throw", () => {
  assert.deepEqual(at("https://bioconice.cancerdatasci.org/data/%E0%A4%A"), { kind: "bad" });
  assert.deepEqual(at("https://data.bioconice.cancerdatasci.org/%"), { kind: "bad" });
});

test("permanentRedirect: assets' trailing-slash 307 becomes a 301 (omicidx#233)", () => {
  const r = permanentRedirect(new Response(null, { status: 307, headers: { location: "/guides/x" } }));
  assert.equal(r.status, 301);
  assert.equal(r.headers.get("location"), "/guides/x");
  const ok = new Response("hi");
  assert.equal(permanentRedirect(ok), ok);
});
