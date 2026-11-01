import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";

// web/shell-links.js is a plain browser script: run it with a stub document
// (no SQL blocks on the page) and read back the function it defines.
const window: { bioconiceShellUrl?: (sql: string) => string } = {};
runInNewContext(readFileSync(new URL("../web/shell-links.js", import.meta.url), "utf8"), {
  window,
  document: { querySelectorAll: () => [] },
});
const shellUrl = window.bioconiceShellUrl!;

// The queries a shell link carries, undoing the URL's encoding and swaps.
const queries = (url: string) => {
  const prefix = "https://shell.duckdb.org/#queries=v0,";
  assert.ok(url.startsWith(prefix));
  return url.slice(prefix.length).split(",").map((q) =>
    decodeURIComponent(q).replace(/[ \-;~]/g, (c) => ({ " ": "-", "-": " ", ";": "~", "~": ";" })[c]!));
};

test("shell link: attaches latest when the example doesn't", () => {
  assert.deepEqual(queries(shellUrl("-- mouse genes\nSELECT count(*)\nFROM annotation.gene;\n")), [
    "ATTACH 'https://data.bioconice.cancerdatasci.org/latest/bioconice.duckdb' AS bioc (READ_ONLY);",
    "USE bioc;",
    "SELECT count(*) FROM annotation.gene;",
  ]);
});

test("shell link: keeps an example's own ATTACH, one query per statement", () => {
  const sql = "ATTACH 'https://x/v2026-10-31/bioconice.duckdb' AS bioc (READ_ONLY);\nSELECT 'a-b~c';";
  assert.deepEqual(queries(shellUrl(sql)), [
    "ATTACH 'https://x/v2026-10-31/bioconice.duckdb' AS bioc (READ_ONLY);",
    "SELECT 'a-b~c';",
  ]);
});
