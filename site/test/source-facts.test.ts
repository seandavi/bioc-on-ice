import { test } from "node:test";
import assert from "node:assert/strict";
import { readFileSync } from "node:fs";
import { runInNewContext } from "node:vm";

// web/sources/source-facts.js is a plain browser script; without a `document` it only
// exposes its pure function, factsOf(manifest, registry, key).
const ctx: { bioconiceSourceFacts?: (m: any, r: any, k: string) => any } = {};
runInNewContext(readFileSync(new URL("../web/sources/source-facts.js", import.meta.url), "utf8"), ctx);
const factsOf = ctx.bioconiceSourceFacts!;

const manifest = {
  release: "2026-11-04", published_at: "2026-11-04T16:29:53+00:00",
  base_url: "https://data.bioconice.cancerdatasci.org/latest",
  tables: [
    { schema: "raw", table: "hgnc__complete_set", path: "raw/hgnc__complete_set.parquet", row_count: 45289,
      comment: "HGNC's hgnc_complete_set.txt landed verbatim and whole: one row per approved human gene, e.g. TP53. Columns are text." },
    { schema: "annotation", table: "identifier_mapping", path: "annotation/identifier_mapping.parquet", row_count: 7,
      comment: "Cross-references between identifier authorities. Mappings are many-to-many." },
  ],
  provenance: [
    { source: "hgnc", source_version: "2026-09-18", retrieved_at: "2026-09-18T21:11:13+00:00" },
    { source: "hgnc", source_version: "2026-08-07", retrieved_at: "2026-08-07T10:00:00+00:00" },
    { source: "ensembl", source_version: "116", retrieved_at: "2026-09-18T09:38:26+00:00" },
  ],
};
const registry = {
  hgnc: { name: "HGNC", tables: ["raw.hgnc__complete_set", "annotation.hgnc__gene"],
          shared: ["annotation.identifier_mapping"], provenance: ["hgnc"] },
};

test("factsOf: own tables then shared, first sentence, Parquet URL, newest provenance row", () => {
  // Round-trip through JSON: the vm context's arrays are not reference-equal to ours.
  const f = JSON.parse(JSON.stringify(factsOf(manifest, registry, "hgnc")));
  assert.equal(f.release, "2026-11-04");
  assert.deepEqual(f.tables.map((t: any) => [t.name, t.rows, t.shared]), [
    ["raw.hgnc__complete_set", 45289, false],
    ["annotation.hgnc__gene", null, false],
    ["annotation.identifier_mapping", 7, true],
  ]);
  assert.equal(f.tables[0].what,
    "HGNC's hgnc_complete_set.txt landed verbatim and whole: one row per approved human gene, e.g. TP53.");
  assert.equal(f.tables[0].url, "https://data.bioconice.cancerdatasci.org/latest/raw/hgnc__complete_set.parquet");
  assert.equal(f.tables[1].what, "not in this release");
  assert.equal(f.tables[1].url, null);
  assert.deepEqual(f.versions, [{ source: "hgnc", version: "2026-09-18", retrievedAt: "2026-09-18" }]);
});
