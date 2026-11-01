// biocOnIce Explorer — reads the public release (ADR-0014), anonymously.
// Plain JS, no framework, no build step: this file is served as-is.
import { RECIPES, PROVENANCE_MATRIX_SQL, RELEASE_URL, VERIFIED_ON } from "./recipes.js";

// The page reads the release same-origin, through the site Worker's /data/.
// RELEASE_URL (the data host) stays in the copyable snippets, which run
// outside the browser; the views in bioconice.duckdb read their Parquet from
// the data host either way.
const DATA_URL = new URL("/data/latest", location.href).href;

// Every metadata field this page shows (comments, `doc`, types, row counts)
// is read from the release's manifest.json at view time — never copied into
// this file — so the page can never drift from what the release serves.
async function loadManifest() {
  const res = await fetch(`${DATA_URL}/manifest.json`);
  if (!res.ok) throw new Error(`manifest.json -> HTTP ${res.status}`);
  return res.json();
}

document.getElementById("catalog-endpoint").textContent = DATA_URL;

// ---------- sidebar: namespaces -> tables ----------

const tree = document.getElementById("catalog-tree");
let currentTableEl = null;

async function loadCatalog() {
  try {
    const manifest = await loadManifest();
    document.getElementById("catalog-endpoint").textContent = `${DATA_URL} (release ${manifest.release})`;
    const byNs = {};
    for (const t of manifest.tables) (byNs[t.schema] ??= []).push(t);
    tree.innerHTML = "";
    tree.removeAttribute("aria-busy");
    for (const ns of Object.keys(byNs).sort()) {
      tree.appendChild(renderNamespaceGroup(ns, byNs[ns].sort((a, b) => a.table.localeCompare(b.table))));
    }
  } catch (err) {
    tree.innerHTML = `<p class="status">Could not read the release: ${escapeHtml(err.message)}</p>`;
  }
}

function renderNamespaceGroup(ns, tables) {
  const group = document.createElement("div");
  group.className = "ns-group";

  const btn = document.createElement("button");
  btn.className = "ns-button";
  btn.setAttribute("aria-expanded", "true");
  btn.innerHTML = `${escapeHtml(ns)} <span class="count">${tables.length}</span>`;
  group.appendChild(btn);

  const ul = document.createElement("ul");
  ul.className = "tables";
  for (const t of tables) {
    const li = document.createElement("li");
    const tbtn = document.createElement("button");
    tbtn.textContent = t.table;
    tbtn.setAttribute("aria-current", "false");
    tbtn.addEventListener("click", () => selectTable(t, tbtn));
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

function selectTable(t, el) {
  if (currentTableEl) currentTableEl.setAttribute("aria-current", "false");
  el.setAttribute("aria-current", "true");
  currentTableEl = el;
  showBrowseView();
  renderTable(t);
}

// ---------- top nav ----------

const views = ["browse", "query", "recipes"];

function showView(name) {
  for (const v of views) {
    const nav = document.getElementById(`nav-${v}`);
    if (v === name) nav.setAttribute("aria-current", "page");
    else nav.removeAttribute("aria-current");
    document.getElementById(`view-${v}`).hidden = v !== name;
  }
}
const showBrowseView = () => showView("browse");
const viewRecipes = document.getElementById("view-recipes");
document.getElementById("nav-browse").addEventListener("click", showBrowseView);
document.getElementById("nav-query").addEventListener("click", () => showView("query"));
document.getElementById("nav-recipes").addEventListener("click", () => {
  showView("recipes");
  if (!viewRecipes.dataset.rendered) {
    renderRecipes();
    viewRecipes.dataset.rendered = "1";
  }
});

// ---------- table view ----------

const tableView = document.getElementById("table-view");

function attachSnippet(ns, table) {
  return [
    `ATTACH '${RELEASE_URL}/bioconice.duckdb' AS bioc (READ_ONLY);`,
    "",
    `SELECT * FROM bioc.${ns}.${table} LIMIT 10;`,
  ].join("\n");
}

function renderTable(t) {
  const ns = t.schema;
  const table = t.table;

  const el = document.createElement("div");

  const h2 = document.createElement("h2");
  h2.textContent = `${ns}.${table}`;
  el.appendChild(h2);

  const commentP = document.createElement("p");
  commentP.className = "text-muted";
  commentP.textContent = t.comment || "(no table comment)";
  el.appendChild(commentP);

  // tabs
  const tabIds = ["overview", "columns", "query"];
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
  overview.appendChild(sectionTitle("Rows"));
  const rows = document.createElement("p");
  rows.textContent = `${t.row_count.toLocaleString()} current rows · ${(t.bytes / 1e6).toFixed(1)} MB of Parquet (${t.path})`;
  overview.appendChild(rows);

  overview.appendChild(sectionTitle("Copyable DuckDB snippet"));
  overview.appendChild(sqlBlock(attachSnippet(ns, table)));

  if (ns === "provenance" && table === "release") {
    overview.appendChild(sectionTitle("Release × source matrix"));
    const note = document.createElement("p");
    note.className = "text-muted";
    note.textContent =
      "Rows are releases, columns are sources, cells are each source's own version string; the current " +
      "release is the lexicographically greatest one (SPEC.md's release ordering). Paste the PIVOT (without the " +
      "ATTACH line) into the Query tab to run it here.";
    overview.appendChild(note);
    overview.appendChild(sqlBlock(PROVENANCE_MATRIX_SQL));
  }

  panels.overview = overview;
  el.appendChild(overview);

  // Columns panel
  const columns = document.createElement("div");
  columns.hidden = true;
  const ct = document.createElement("table");
  ct.className = "table table-sm cols";
  ct.innerHTML = `<thead><tr><th>Column</th><th>Type</th><th>Doc</th></tr></thead>`;
  const tbody = document.createElement("tbody");
  for (const c of t.columns) {
    const tr = document.createElement("tr");
    tr.innerHTML = `
      <td><code>${escapeHtml(c.name)}</code></td>
      <td><code>${escapeHtml(c.type)}</code></td>
      <td>${escapeHtml(c.doc || "")}</td>`;
    tbody.appendChild(tr);
  }
  ct.appendChild(tbody);
  columns.appendChild(ct);
  panels.columns = columns;
  el.appendChild(columns);

  // Query panel: DuckDB-WASM, loaded on first Run
  const queryPanel = document.createElement("div");
  queryPanel.hidden = true;
  queryPanel.appendChild(buildQueryPanel(`SELECT * FROM bioc.${ns}.${table} LIMIT 25;`));
  panels.query = queryPanel;
  el.appendChild(queryPanel);

  tableView.innerHTML = "";
  tableView.appendChild(el);
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
  btn.className = "copy-btn btn btn-sm btn-outline-secondary";
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
  intro.className = "text-muted";
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
    result.className = "result text-muted small";
    result.textContent = `Verified ${VERIFIED_ON}: ${r.verified}`;
    div.appendChild(result);
    container.appendChild(div);
  }
}

// ---------- in-browser DuckDB-WASM query panel ----------

let duckdbDbPromise = null;

async function getDuckDB(log) {
  if (!duckdbDbPromise) {
    duckdbDbPromise = (async () => {
      const duckdb = await import(
        "https://cdn.jsdelivr.net/npm/@duckdb/duckdb-wasm@1.33.1-dev57.0/+esm"
      );
      const bundle = await duckdb.selectBundle(duckdb.getJsDelivrBundles());
      const workerUrl = URL.createObjectURL(
        new Blob([`importScripts("${bundle.mainWorker}");`], { type: "text/javascript" })
      );
      const worker = new Worker(workerUrl);
      const db = new duckdb.AsyncDuckDB(new duckdb.ConsoleLogger(), worker);
      await db.instantiate(bundle.mainModule, bundle.pthreadWorker);
      URL.revokeObjectURL(workerUrl);
      // Read byte ranges only. By default DuckDB-Wasm downloads each whole
      // Parquet file before querying it. All three flags are needed (omicidx
      // found leaving forceFullHTTPReads unset breaks every open).
      await db.open({
        filesystem: { reliableHeadRequests: true, allowFullHTTPReads: false, forceFullHTTPReads: false },
      });
      const conn = await db.connect();
      await conn.query(`ATTACH '${DATA_URL}/bioconice.duckdb' AS bioc (READ_ONLY);`);
      await conn.query("USE bioc");
      return conn;
    })();
  }
  return duckdbDbPromise;
}

const WRITE_STATEMENT = /^\s*(insert|update|delete|merge|attach|detach|copy|pragma|create|drop|alter|call|export|import|install|load|set|vacuum)\b/i;

function buildQueryPanel(initialSql) {
  const wrap = document.createElement("div");
  const box = document.createElement("textarea");
  box.className = "form-control font-monospace sql-box";
  box.setAttribute("aria-label", "SQL query");
  box.rows = 8;
  box.value = initialSql;
  wrap.appendChild(box);

  const controls = document.createElement("div");
  controls.style.display = "flex";
  controls.style.gap = "0.75rem";
  controls.style.alignItems = "center";
  controls.style.margin = "0.5rem 0";

  const runBtn = document.createElement("button");
  runBtn.className = "btn btn-primary btn-sm";
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
      out.innerHTML = `<div class="alert alert-danger">Refused: this box only runs a single read-only SELECT/WITH.</div>`;
      return;
    }
    const sql = `SELECT * FROM (${raw.replace(/;\s*$/, "")}) t LIMIT ${ROW_CAP}`;
    out.innerHTML = `<p class="status" aria-busy="true">Running…</p>`;
    try {
      const conn = await getDuckDB();
      const t0 = performance.now();
      const res = await conn.query(sql);
      const ms = Math.round(performance.now() - t0);
      const rows = res.toArray().map((r) => r.toJSON());
      out.innerHTML = "";
      const p = document.createElement("p");
      p.className = "text-muted small";
      p.textContent = `${rows.length} row(s) in ${ms} ms${rows.length === ROW_CAP ? ` (capped at ${ROW_CAP})` : ""} · `;
      const shell = document.createElement("a");
      shell.href = window.bioconiceShellUrl(raw); // from /shell-links.js
      shell.target = "_blank";
      shell.rel = "noopener";
      shell.textContent = "Open in DuckDB Shell ↗";
      p.appendChild(shell);
      out.appendChild(p);
      const scroll = document.createElement("div");
      scroll.className = "table-responsive";
      scroll.appendChild(renderResultTable(rows));
      out.appendChild(scroll);
    } catch (err) {
      out.innerHTML = `<div class="alert alert-danger">${escapeHtml(err.message || String(err))}</div>`;
    }
  });

  return wrap;
}

function renderResultTable(rows) {
  const t = document.createElement("table");
  t.className = "table table-sm cols";
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

document.getElementById("query-box").appendChild(buildQueryPanel(`SELECT gene_id, symbol, source, gene_type
FROM annotation.gene
WHERE taxon_id = 9606 AND symbol = 'TP53' AND valid_to IS NULL;`));
loadCatalog();
