// biocOnIce Explorer — reads the live icegate REST catalog, anonymously.
// Plain JS, no framework, no build step: this file is served as-is.
import { RECIPES, PROVENANCE_MATRIX_SQL } from "./recipes.js";

const CATALOG_ENDPOINT = "https://icegate-bioconice.seandavi.workers.dev";
const WAREHOUSE = "bioconice";

// Every metadata field this page shows (comments, `doc`, properties, snapshots)
// is read from this call at view time — never copied into this file — so the
// page can never drift from the live schema. See AGENTS.md / SPEC.md
// "Semantic Layer".
//
// icegate rate-limits bursts of catalog calls (see this repo's commit
// history on 429s), and loading the whole namespace tree up front is exactly
// that kind of burst, so a 429 gets one short retry before it's treated as a
// real error.
async function api(path, { retried = false } = {}) {
  const res = await fetch(`${CATALOG_ENDPOINT}/v1/${WAREHOUSE}${path}`);
  if (res.status === 429 && !retried) {
    await new Promise((r) => setTimeout(r, 500 + Math.random() * 500));
    return api(path, { retried: true });
  }
  if (!res.ok) throw new Error(`${path} -> HTTP ${res.status}`);
  return res.json();
}

document.getElementById("catalog-endpoint").textContent = CATALOG_ENDPOINT;

// ---------- sidebar: namespaces -> tables ----------

const tree = document.getElementById("catalog-tree");
let currentTableEl = null;

async function loadCatalog() {
  try {
    const { namespaces } = await api("/namespaces");
    tree.innerHTML = "";
    tree.removeAttribute("aria-busy");
    for (const [ns] of namespaces.sort((a, b) => a[0].localeCompare(b[0]))) {
      tree.appendChild(await renderNamespaceGroup(ns));
    }
  } catch (err) {
    tree.innerHTML = `<p class="status">Could not reach the catalog: ${escapeHtml(err.message)}</p>`;
  }
}

async function renderNamespaceGroup(ns) {
  const group = document.createElement("div");
  group.className = "ns-group";

  let nsInfo = { properties: {} };
  let tablesInfo = { identifiers: [] };
  let loadError = null;
  try {
    [nsInfo, tablesInfo] = await Promise.all([
      api(`/namespaces/${ns}`),
      api(`/namespaces/${ns}/tables`),
    ]);
  } catch (err) {
    loadError = err.message;
  }
  const tables = tablesInfo.identifiers
    .map((t) => t.name)
    .sort((a, b) => a.localeCompare(b));

  const btn = document.createElement("button");
  btn.className = "ns-button";
  btn.setAttribute("aria-expanded", "true");
  btn.innerHTML = `${escapeHtml(ns)} <span class="count">${loadError ? "error" : tables.length}</span>`;
  group.appendChild(btn);

  if (loadError) {
    const p = document.createElement("p");
    p.className = "ns-comment";
    p.textContent = `Could not load this namespace: ${loadError}`;
    group.appendChild(p);
    return group;
  }

  if (nsInfo.properties?.comment) {
    const p = document.createElement("p");
    p.className = "ns-comment";
    p.textContent = nsInfo.properties.comment;
    group.appendChild(p);
  }

  const ul = document.createElement("ul");
  ul.className = "tables";
  for (const table of tables) {
    const li = document.createElement("li");
    const tbtn = document.createElement("button");
    tbtn.textContent = table;
    tbtn.setAttribute("aria-current", "false");
    tbtn.addEventListener("click", () => selectTable(ns, table, tbtn));
    li.appendChild(tbtn);
    ul.appendChild(li);
  }
  group.appendChild(ul);

  btn.addEventListener("click", () => {
    const open = btn.getAttribute("aria-expanded") === "true";
    btn.setAttribute("aria-expanded", String(!open));
    ul.hidden = open;
  });

  return group;
}

function selectTable(ns, table, el) {
  if (currentTableEl) currentTableEl.setAttribute("aria-current", "false");
  el.setAttribute("aria-current", "true");
  currentTableEl = el;
  showBrowseView();
  renderTable(ns, table);
}

// ---------- top nav ----------

const navBrowse = document.getElementById("nav-browse");
const navRecipes = document.getElementById("nav-recipes");
const viewBrowse = document.getElementById("view-browse");
const viewRecipes = document.getElementById("view-recipes");

function showBrowseView() {
  navBrowse.setAttribute("aria-current", "page");
  navRecipes.removeAttribute("aria-current");
  viewBrowse.hidden = false;
  viewRecipes.hidden = true;
}
function showRecipesView() {
  navRecipes.setAttribute("aria-current", "page");
  navBrowse.removeAttribute("aria-current");
  viewBrowse.hidden = true;
  viewRecipes.hidden = false;
  if (!viewRecipes.dataset.rendered) {
    renderRecipes();
    viewRecipes.dataset.rendered = "1";
  }
}
navBrowse.addEventListener("click", showBrowseView);
navRecipes.addEventListener("click", showRecipesView);

// ---------- table view ----------

const tableView = document.getElementById("table-view");

function attachSnippet(ns, table, hasValidTo) {
  const lines = [
    "INSTALL iceberg; LOAD iceberg;",
    "ATTACH 'bioconice' AS bioc (",
    "    TYPE ICEBERG,",
    `    ENDPOINT '${CATALOG_ENDPOINT}',`,
    "    AUTHORIZATION_TYPE 'none'",
    ");",
    "",
    `SELECT * FROM bioc.${ns}.${table} LIMIT 10;`,
  ];
  if (hasValidTo) {
    lines.push("", `-- current rows only:`, `SELECT * FROM bioc.${ns}.${table} WHERE valid_to IS NULL LIMIT 10;`);
  }
  return lines.join("\n");
}

function fmtType(t) {
  if (typeof t === "string") return t;
  if (t?.type === "list") return `list<${fmtType(t.element)}>`;
  if (t?.type === "struct") return "struct";
  if (t?.type === "map") return `map<${fmtType(t.key)}, ${fmtType(t.value)}>`;
  return JSON.stringify(t);
}

function businessKeyFieldIds(schema) {
  const validFromField = schema.fields.find((f) => f.name === "valid_from");
  const ids = new Set(schema["identifier-field-ids"] || []);
  if (validFromField) ids.delete(validFromField.id);
  return ids;
}

async function renderTable(ns, table) {
  tableView.innerHTML = `<p class="status" aria-busy="true">Loading ${escapeHtml(ns)}.${escapeHtml(table)}…</p>`;
  let data;
  try {
    data = await api(`/namespaces/${ns}/tables/${table}`);
  } catch (err) {
    tableView.innerHTML = `<p class="status">Could not load this table: ${escapeHtml(err.message)}</p>`;
    return;
  }
  const meta = data.metadata;
  const schema = meta.schemas.find((s) => s["schema-id"] === meta["current-schema-id"]) || meta.schemas[0];
  const props = meta.properties || {};
  const comment = props.comment || "(no table comment)";
  const businessKeyIds = businessKeyFieldIds(schema);
  const hasValidTo = schema.fields.some((f) => f.name === "valid_to");
  const partSpec = (meta["partition-specs"] || []).find((p) => p["spec-id"] === meta["default-spec-id"]);

  const el = document.createElement("div");

  const h2 = document.createElement("h2");
  h2.textContent = `${ns}.${table}`;
  el.appendChild(h2);

  const commentP = document.createElement("p");
  commentP.className = "muted";
  commentP.textContent = comment;
  el.appendChild(commentP);

  // tabs
  const tabIds = ["overview", "columns", "snapshots"];
  if (localStorage.getItem("bioconice_query_tab") === "1") tabIds.push("query");
  const tabsNav = document.createElement("div");
  tabsNav.className = "tabs";
  tabsNav.setAttribute("role", "tablist");
  const panels = {};
  for (const id of tabIds) {
    const b = document.createElement("button");
    b.textContent = id[0].toUpperCase() + id.slice(1);
    b.setAttribute("role", "tab");
    b.setAttribute("aria-selected", id === "overview" ? "true" : "false");
    b.addEventListener("click", () => {
      for (const t of tabsNav.children) t.setAttribute("aria-selected", "false");
      b.setAttribute("aria-selected", "true");
      for (const k in panels) panels[k].hidden = k !== id;
    });
    tabsNav.appendChild(b);
  }
  el.appendChild(tabsNav);

  // Overview panel
  const overview = document.createElement("div");
  overview.appendChild(sectionTitle("Copyable DuckDB snippet"));
  overview.appendChild(sqlBlock(attachSnippet(ns, table, hasValidTo)));

  if (partSpec && partSpec.fields.length) {
    overview.appendChild(sectionTitle("Partitioned by"));
    const p = document.createElement("p");
    p.textContent = partSpec.fields.map((f) => `${f.name} (${f.transform})`).join(", ");
    overview.appendChild(p);
  }

  if (businessKeyIds.size) {
    const names = schema.fields.filter((f) => businessKeyIds.has(f.id)).map((f) => f.name);
    overview.appendChild(sectionTitle("Business key"));
    const p = document.createElement("p");
    p.innerHTML = names.map((n) => `<code>${escapeHtml(n)}</code>`).join(", ") +
      ` <span class="muted">— what a merge joins on; the Iceberg row key is this plus <code>valid_from</code>.</span>`;
    overview.appendChild(p);
  }

  const licence = props["bioc.license"];
  if (licence) {
    overview.appendChild(sectionTitle("Licence"));
    const p = document.createElement("p");
    p.textContent = licence;
    overview.appendChild(p);
  }

  if (ns === "provenance" && table === "release") {
    overview.appendChild(sectionTitle("Release × source matrix"));
    const note = document.createElement("div");
    note.className = "warn";
    note.textContent =
      "Rendering the actual rows needs a query engine, which this static page does not have in phase 1 " +
      "(the in-browser query tab exists but is off by default — see the Recipes page and explorer/README.md " +
      "for why). Run this PIVOT yourself: rows are releases, columns are sources, cells are each source's own " +
      "version string, and the current release is the lexicographically greatest one (SPEC.md's release ordering).";
    overview.appendChild(note);
    overview.appendChild(sqlBlock(PROVENANCE_MATRIX_SQL));
  }

  panels.overview = overview;
  el.appendChild(overview);

  // Columns panel
  const columns = document.createElement("div");
  columns.hidden = true;
  const t = document.createElement("table");
  t.className = "cols";
  t.innerHTML = `<thead><tr><th>Column</th><th>Type</th><th>Required</th><th>Key</th><th>Doc</th></tr></thead>`;
  const tbody = document.createElement("tbody");
  for (const f of schema.fields) {
    const tr = document.createElement("tr");
    const badges = [];
    if (businessKeyIds.has(f.id)) badges.push('<span class="badge key">key</span>');
    const prefix = props[`bioc.column.${f.name}.prefix`];
    if (prefix) badges.push(`<span class="badge">prefix: ${escapeHtml(prefix)}</span>`);
    const coord = props[`bioc.column.${f.name}.coordinate_system`];
    if (coord) badges.push(`<span class="badge">${escapeHtml(coord)}</span>`);
    tr.innerHTML = `
      <td><code>${escapeHtml(f.name)}</code></td>
      <td><code>${escapeHtml(fmtType(f.type))}</code></td>
      <td>${f.required ? "yes" : "no"}</td>
      <td>${badges.join(" ") || "&nbsp;"}</td>
      <td>${escapeHtml(f.doc || "")}</td>`;
    tbody.appendChild(tr);
  }
  t.appendChild(tbody);
  columns.appendChild(t);
  panels.columns = columns;
  el.appendChild(columns);

  // Snapshots panel
  const snapshots = document.createElement("div");
  snapshots.hidden = true;
  snapshots.appendChild(renderSnapshots(meta));
  panels.snapshots = snapshots;
  el.appendChild(snapshots);

  // Query panel (phase 2, opt-in only)
  if (panels.query === undefined && tabIds.includes("query")) {
    const queryPanel = document.createElement("div");
    queryPanel.hidden = true;
    queryPanel.appendChild(buildQueryPanel(ns, table, hasValidTo));
    panels.query = queryPanel;
    el.appendChild(queryPanel);
  }

  tableView.innerHTML = "";
  tableView.appendChild(el);
}

function renderSnapshots(meta) {
  const wrap = document.createElement("div");
  const snaps = (meta.snapshots || []).slice().sort((a, b) => b["timestamp-ms"] - a["timestamp-ms"]);
  if (!snaps.length) {
    wrap.innerHTML = `<p class="muted">No snapshots.</p>`;
    return wrap;
  }
  const SHOWN = 50;
  const t = document.createElement("table");
  t.className = "snap-table";
  t.innerHTML = `<thead><tr><th>Timestamp (UTC)</th><th>Operation</th><th>+records</th><th>-records</th><th>Snapshot id</th></tr></thead>`;
  const tbody = document.createElement("tbody");
  for (const s of snaps.slice(0, SHOWN)) {
    const tr = document.createElement("tr");
    if (s["snapshot-id"] === meta["current-snapshot-id"]) tr.className = "current";
    const sum = s.summary || {};
    tr.innerHTML = `
      <td>${new Date(s["timestamp-ms"]).toISOString().replace("T", " ").slice(0, 19)}</td>
      <td>${escapeHtml(sum.operation || "")}${s["snapshot-id"] === meta["current-snapshot-id"] ? " (current)" : ""}</td>
      <td class="num">${sum["added-records"] ?? "–"}</td>
      <td class="num">${sum["deleted-records"] ?? "–"}</td>
      <td><code>${s["snapshot-id"]}</code></td>`;
    tbody.appendChild(tr);
  }
  t.appendChild(tbody);
  wrap.appendChild(t);
  if (snaps.length > SHOWN) {
    const p = document.createElement("p");
    p.className = "muted";
    p.textContent = `Showing the latest ${SHOWN} of ${snaps.length} snapshots.`;
    wrap.appendChild(p);
  }
  return wrap;
}

function sectionTitle(text) {
  const h3 = document.createElement("h3");
  h3.textContent = text;
  h3.style.marginBottom = "0.3rem";
  return h3;
}

function sqlBlock(sql) {
  const wrap = document.createElement("div");
  wrap.className = "copy-wrap";
  const pre = document.createElement("pre");
  pre.className = "sql";
  pre.textContent = sql;
  const btn = document.createElement("button");
  btn.className = "copy-btn";
  btn.type = "button";
  btn.textContent = "Copy";
  btn.addEventListener("click", () => copyText(sql, btn));
  wrap.appendChild(pre);
  wrap.appendChild(btn);
  return wrap;
}

function copyText(text, btn) {
  const done = () => {
    const orig = btn.textContent;
    btn.textContent = "Copied";
    setTimeout(() => (btn.textContent = orig), 1200);
  };
  if (navigator.clipboard?.writeText) {
    navigator.clipboard.writeText(text).then(done).catch(() => fallbackCopy(text, done));
  } else {
    fallbackCopy(text, done);
  }
}
function fallbackCopy(text, done) {
  const ta = document.createElement("textarea");
  ta.value = text;
  ta.style.position = "fixed";
  ta.style.opacity = "0";
  document.body.appendChild(ta);
  ta.select();
  try { document.execCommand("copy"); } catch { /* ponytail: best-effort fallback for clipboard API-less browsers */ }
  document.body.removeChild(ta);
  done();
}

function escapeHtml(s) {
  return String(s).replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));
}

// ---------- recipes view ----------

function renderRecipes() {
  const container = viewRecipes;
  container.innerHTML = "";
  const h2 = document.createElement("h2");
  h2.textContent = "Recipes";
  container.appendChild(h2);
  const intro = document.createElement("p");
  intro.className = "muted";
  intro.textContent =
    "The same fixed worked queries issue #100's MCP recipes() tool serves. Each snippet is self-contained — paste it verbatim into desktop DuckDB.";
  container.appendChild(intro);

  for (const r of RECIPES) {
    const div = document.createElement("div");
    div.className = "recipe";
    const h3 = document.createElement("h3");
    h3.textContent = r.title;
    div.appendChild(h3);
    const p = document.createElement("p");
    p.textContent = r.description;
    div.appendChild(p);
    div.appendChild(sqlBlock(r.sql));
    const result = document.createElement("p");
    result.className = "result";
    result.textContent = `Verified 2026-09-18: ${r.verified}`;
    div.appendChild(result);
    container.appendChild(div);
  }

  const flagCard = document.createElement("div");
  flagCard.className = "card";
  flagCard.innerHTML = `<h3>In-browser queries (phase 2, experimental)</h3>
    <p class="muted">DuckDB-WASM's <code>ATTACH</code> to this catalog works anonymously in the browser, but reading
    table <em>data</em> currently fails: the R2 bucket behind icegate serves no CORS headers, so the browser blocks
    the data-file fetch (icegate's own CORS is fine — this is the bucket). See explorer/README.md for the exact
    finding. Off by default for that reason.</p>`;
  const label = document.createElement("label");
  label.className = "toggle";
  const cb = document.createElement("input");
  cb.type = "checkbox";
  cb.id = "query-flag";
  cb.checked = localStorage.getItem("bioconice_query_tab") === "1";
  cb.addEventListener("change", () => {
    localStorage.setItem("bioconice_query_tab", cb.checked ? "1" : "0");
  });
  label.appendChild(cb);
  label.append(" Enable the Query tab on table pages (known broken — see above)");
  flagCard.appendChild(label);
  container.appendChild(flagCard);
}

// ---------- phase 2: in-browser DuckDB-WASM query panel ----------

let duckdbDbPromise = null;

async function getDuckDB(log) {
  if (!duckdbDbPromise) {
    duckdbDbPromise = (async () => {
      const duckdb = await import(
        "https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.33.1-dev57.0/dist/duckdb-browser.mjs"
      );
      const bundle = await duckdb.selectBundle(duckdb.getJsDelivrBundles());
      const workerUrl = URL.createObjectURL(
        new Blob([`importScripts("${bundle.mainWorker}");`], { type: "text/javascript" })
      );
      const worker = new Worker(workerUrl);
      const db = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(), worker);
      await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
      URL.revokeObjectURL(workerUrl);
      const conn = await db.connect();
      await conn.query("INSTALL iceberg; LOAD iceberg; INSTALL httpfs; LOAD httpfs;");
      await conn.query(
        `ATTACH 'bioconice' AS bioc (TYPE ICEBERG, ENDPOINT '${CATALOG_ENDPOINT}', AUTHORIZATION_TYPE 'none');`
      );
      return conn;
    })();
  }
  return duckdbDbPromise;
}

const WRITE_STATEMENT = /^\s*(insert|update|delete|merge|attach|detach|copy|pragma|create|drop|alter|call|export|import|install|load|set|vacuum)\b/i;

function buildQueryPanel(ns, table, hasValidTo) {
  const wrap = document.createElement("div");
  wrap.innerHTML = `
    <p class="warn">Experimental and, as of 2026-09-18, expected to fail on any real data read (R2 CORS — see
    Recipes page). Kept behind this opt-in flag so the failure mode is visible rather than hidden.</p>`;
  const box = document.createElement("textarea");
  box.id = "sql-box";
  box.setAttribute("aria-label", "SQL query");
  box.value = `SELECT * FROM bioc.${ns}.${table} LIMIT 25;`;
  wrap.appendChild(box);

  const controls = document.createElement("div");
  controls.style.display = "flex";
  controls.style.gap = "0.75rem";
  controls.style.alignItems = "center";
  controls.style.margin = "0.5rem 0";

  const currentOnly = document.createElement("label");
  currentOnly.className = "toggle";
  currentOnly.style.margin = "0";
  const currentOnlyCb = document.createElement("input");
  currentOnlyCb.type = "checkbox";
  currentOnlyCb.disabled = !hasValidTo;
  currentOnly.appendChild(currentOnlyCb);
  currentOnly.append(" current only (valid_to IS NULL)");
  controls.appendChild(currentOnly);

  const runBtn = document.createElement("button");
  runBtn.className = "copy-btn";
  runBtn.type = "button";
  runBtn.textContent = "Run";
  controls.appendChild(runBtn);
  wrap.appendChild(controls);

  const out = document.createElement("div");
  out.setAttribute("role", "status");
  wrap.appendChild(out);

  const ROW_CAP = 1000;

  runBtn.addEventListener("click", async () => {
    const raw = box.value.trim();
    if (WRITE_STATEMENT.test(raw) || raw.includes(";") && raw.trim().split(";").filter(Boolean).length > 1) {
      out.innerHTML = `<p class="warn">Refused: this box only runs a single read-only SELECT/WITH.</p>`;
      return;
    }
    let sql = raw.replace(/;\s*$/, "");
    if (currentOnlyCb.checked) sql = `SELECT * FROM (${sql}) t WHERE valid_to IS NULL`;
    sql = `SELECT * FROM (${sql}) t LIMIT ${ROW_CAP}`;
    out.innerHTML = `<p class="status" aria-busy="true">Running…</p>`;
    try {
      const conn = await getDuckDB();
      const t0 = performance.now();
      const res = await conn.query(sql);
      const ms = Math.round(performance.now() - t0);
      const rows = res.toArray().map((r) => r.toJSON());
      out.innerHTML = "";
      const p = document.createElement("p");
      p.className = "muted";
      p.textContent = `${rows.length} row(s) in ${ms} ms${rows.length === ROW_CAP ? ` (capped at ${ROW_CAP})` : ""}`;
      out.appendChild(p);
      out.appendChild(renderResultTable(rows));
    } catch (err) {
      out.innerHTML = `<p class="warn">${escapeHtml(err.message || String(err))}</p>`;
    }
  });

  return wrap;
}

function renderResultTable(rows) {
  const t = document.createElement("table");
  t.className = "cols";
  if (!rows.length) {
    t.innerHTML = "<tbody><tr><td>(no rows)</td></tr></tbody>";
    return t;
  }
  const cols = Object.keys(rows[0]);
  t.innerHTML = `<thead><tr>${cols.map((c) => `<th>${escapeHtml(c)}</th>`).join("")}</tr></thead>`;
  const tbody = document.createElement("tbody");
  for (const r of rows) {
    const tr = document.createElement("tr");
    tr.innerHTML = cols.map((c) => `<td>${escapeHtml(String(r[c]))}</td>`).join("");
    tbody.appendChild(tr);
  }
  t.appendChild(tbody);
  return t;
}

loadCatalog();
