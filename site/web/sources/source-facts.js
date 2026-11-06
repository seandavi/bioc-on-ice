// The generated facts on each source page and on sources/index.qmd, read from
// latest/manifest.json when the page loads: nothing runs at render time (site/README.md),
// as releases.qmd does. sources.json maps a source to its tables and to its `source`
// values in provenance.release. A page names its source with
// `::: {.source-facts data-source="hgnc"}`; the index is `::: {#sources-index}`.
(() => {
  const MANIFEST = "https://data.bioconice.cancerdatasci.org/latest/manifest.json";
  const inBrowser = typeof document !== "undefined";
  const REGISTRY = inBrowser ? new URL("sources.json", document.currentScript.src).href : null;

  // ponytail: first sentence of a table comment = up to the first ".", "!" or "?" that is
  // followed by a capital or the end, not after e.g./i.e./vs./etc./cf.; a quoted sentence may
  // still split early.
  const firstSentence = (s) =>
    (s || "").match(/^.*?(?<!\be\.g|\bi\.e|\bvs|\betc|\bcf)[.!?](?=\s+[A-Z`'"(\[]|\s*$)/s)?.[0] || s || "";

  // {release, publishedAt, tables, versions} for one source of the registry.
  // tables: [{name, rows, what, url, shared}], in registry order, own tables then shared;
  // versions: the newest provenance.release row per source name, [{source, version, retrievedAt}].
  function factsOf(manifest, registry, key) {
    const src = registry[key];
    const byName = Object.fromEntries(manifest.tables.map((t) => [`${t.schema}.${t.table}`, t]));
    const row = (name, shared) => {
      const t = byName[name];
      return {
        name, shared,
        rows: t?.row_count ?? null,
        what: t ? firstSentence(t.comment) : "not in this release",
        url: t ? `${manifest.base_url}/${t.path}` : null,
      };
    };
    const newest = {};
    for (const p of manifest.provenance ?? []) {
      if (!src.provenance.includes(p.source)) continue;
      if (!newest[p.source] || p.retrieved_at > newest[p.source].retrieved_at) newest[p.source] = p;
    }
    return {
      release: manifest.release,
      publishedAt: manifest.published_at,
      tables: [...src.tables.map((n) => row(n, false)), ...src.shared.map((n) => row(n, true))],
      versions: src.provenance.map((s) => newest[s]).filter(Boolean).map((p) => ({
        source: p.source, version: p.source_version, retrievedAt: p.retrieved_at.slice(0, 10),
      })),
    };
  }

  if (!inBrowser) { globalThis.bioconiceSourceFacts = factsOf; return; }

  const el = (tag, attrs = {}, ...children) => {
    const e = document.createElement(tag);
    for (const [k, v] of Object.entries(attrs)) e.setAttribute(k, v);
    e.append(...children);
    return e;
  };
  const code = (s) => el("code", {}, s);
  const n = (x) => (x === null ? "–" : x.toLocaleString("en-US"));
  const table = (head, rows) => el("div", { class: "table-responsive" },
    el("table", { class: "table table-sm" },
      el("thead", {}, el("tr", {}, ...head.map(([h, cls]) => el("th", { scope: "col", class: cls || "" }, h)))),
      el("tbody", {}, ...rows)));

  function sourcePage(facts) {
    const versions = facts.versions.length
      ? el("ul", {}, ...facts.versions.map((v) =>
          el("li", {}, code(v.source), ": version ", code(v.version), `, retrieved ${v.retrievedAt}`)))
      : el("p", {}, "No row in ", code("provenance.release"), " names this source.");
    const rows = facts.tables.map((t) => el("tr", {},
      el("td", {}, code(t.name), t.shared ? " " : "",
        t.shared ? el("span", { class: "badge text-bg-secondary" }, "cross-source") : ""),
      el("td", { class: "text-end" }, n(t.rows)),
      el("td", {}, t.what),
      el("td", {}, t.url ? el("a", { href: t.url }, "Parquet") : "–")));
    return [
      el("p", {}, "Release ", code(facts.release), ` (published ${facts.publishedAt.slice(0, 10)}). `,
        "Upstream version as recorded in ", code("provenance.release"), ":"),
      versions,
      table([["Table"], ["Rows", "text-end"], ["What a row is"], ["File"]], rows),
      ...(facts.tables.some((t) => t.shared) ? [el("p", { class: "small" },
        "A cross-source table stacks rows from several sources; the count is the whole table.")] : []),
    ];
  }

  function sourcesIndex(manifest, registry) {
    const keys = Object.keys(registry).filter((k) => !k.startsWith("_"))
      .sort((a, b) => registry[a].name.localeCompare(registry[b].name, "en", { sensitivity: "base" }));
    const rows = keys.map((k) => {
      const s = registry[k];
      const f = factsOf(manifest, registry, k);
      const version = f.versions.length === 1 ? code(f.versions[0].version)
        : f.versions.length ? `${f.versions.length} files, see page` : "–";
      return el("tr", {},
        el("td", {}, el("a", { href: `${k}.html` }, s.name)),
        el("td", {}, s.covers),
        el("td", {}, s.licence),
        el("td", { class: "text-end" }, String(s.tables.length), s.shared.length ? ` +${s.shared.length} shared` : ""),
        el("td", {}, version));
    });
    return [
      el("p", {}, "Release ", code(manifest.release), ` (published ${manifest.published_at.slice(0, 10)}), `,
        `${manifest.tables.length} tables.`),
      table([["Source"], ["Covers"], ["Licence"], ["Tables", "text-end"], ["Upstream version"]], rows),
    ];
  }

  async function main() {
    const targets = [...document.querySelectorAll(".source-facts[data-source], #sources-index")];
    if (!targets.length) return;
    const getJson = async (url) => {
      const r = await fetch(url, { headers: { accept: "application/json" } });
      if (!r.ok) throw new Error(`${r.status} for ${url}`);
      return r.json();
    };
    try {
      const [manifest, registry] = await Promise.all([getJson(MANIFEST), getJson(REGISTRY)]);
      for (const t of targets) {
        t.replaceChildren(...(t.id === "sources-index"
          ? sourcesIndex(manifest, registry)
          : sourcePage(factsOf(manifest, registry, t.dataset.source))));
      }
    } catch (e) {
      for (const t of targets) t.replaceChildren(el("p", { class: "text-danger" }, `Couldn't load the release facts: ${e.message}`));
    }
  }
  main();
})();
