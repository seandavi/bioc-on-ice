# biocOnIce Explorer

A static page that reads the live biocOnIce catalog through icegate,
anonymously, and shows what's in it: namespaces, tables, column docs,
business keys, partition specs, snapshot history, and a copyable DuckDB
snippet per table — plus the same fixed recipe queries issue #100's MCP
`recipes()` tool serves. Plain HTML/CSS/JS, no framework, no build step.
Nothing here writes to the catalog; nothing here is a backend — it makes the
same anonymous, read-only REST calls a browser could always make against
`https://icegate-bioconice.seandavi.workers.dev` (`icegate.yaml`'s
`cors: origins: ["*"]` exists for exactly this).

## Serve it locally

```sh
cd explorer/public
python -m http.server 8000
# open http://localhost:8000
```

No server-side code, no build, no `npm install` for phase 1 — everything is
fetched from the live catalog and (for the opt-in Query tab) a CDN at
request time.

## Deploy

This ships as a Cloudflare Worker with **Workers Static Assets**
(`explorer/wrangler.jsonc`: `assets.directory: ./public`, no Worker script —
an assets-only Worker is allowed), the same account and deploy credentials
`docs/DEPLOY.md` uses for the gateway, kept as a separate Worker so a change
to one never risks the other:

```sh
cd explorer
npx wrangler deploy
```

Credentials: Secret Manager project `cdsci-infra`,
`cdsci-cloudflare-workers-token` (Workers deploy rights) and
`cdsci-r2-account-id` — set them the same way `scripts/deploy-icegate.sh`
does before running `wrangler deploy` here. First deploy creates the Worker
under the name in `wrangler.jsonc` (`bioconice-explorer`) and serves it at
`https://bioconice-explorer.<account-subdomain>.workers.dev`; a later deploy
just ships a new version, no downtime. Validated with
`npx wrangler deploy --dry-run` from `explorer/` (this session, 2026-09-18):
reads 4 files from `public/`, no bindings, no errors — **not deployed**, per
this task's instructions.

`wrangler pages deploy explorer/public --project-name bioconice-explorer`
(Cloudflare Pages) or `gh-pages`/GitHub Pages Actions on `explorer/public`
both work too, if the Worker path is ever not preferred — the page has zero
platform-specific code — but Workers Static Assets is what this repo is
configured for.

## Phase 2 gate: in-browser DuckDB-WASM queries

**Finding (tested live in a real browser, 2026-09-18): `ATTACH` succeeds
anonymously; reading table *data* fails, because the `bioconice` R2 bucket
serves no CORS headers.** This is the blocker the issue predicted, confirmed
end to end:

1. `duckdb-wasm` (`@duckdb/duckdb-wasm@1.33.1-dev57.0`, the version npm's
   `latest` dist-tag resolves to as of 2026-09-18 — no plain `1.33.0` is
   published) loads in the browser via ESM from jsdelivr. Its browser build
   imports the bare specifier `apache-arrow`, which needs a static
   `<script type="importmap">` declared before any module script runs
   (`index.html`) — that's undocumented in the issue and worth recording.
2. `INSTALL iceberg; LOAD iceberg;` succeeds — the WASM build at
   `extensions.duckdb.org/.../iceberg.duckdb_extension.wasm` the issue names
   is real and loads.
3. `INSTALL httpfs; LOAD httpfs;` is *also* required before `ATTACH` — the
   default secret provider DuckDB tries for `s3://` needs it — again not
   called out in the issue.
4. `ATTACH 'bioconice' AS bioc (TYPE ICEBERG, ENDPOINT '...', AUTHORIZATION_TYPE 'none')`
   **succeeds** — the anonymous credential-vending path works from a browser
   origin, exercising icegate's CORS handling end to end for metadata.
5. Any query that touches table *data* — e.g.
   `SELECT * FROM bioc.provenance.release LIMIT 5` — fails:
   ```
   HTTP Error: Full download failed to to URL
   "https://<account>.r2.cloudflarestorage.com/bioconice/__r2_data_catalog/.../snap-....avro":
   404 (Please consult the browser console for details, might be potentially
   a CORS error)
   ```
   The "404" is misleading — `curl -X OPTIONS` against the same R2 URL with
   an `Origin` header returns `403 Forbidden` with **no**
   `access-control-allow-origin` header at all (checked directly, not just
   inferred from the browser error), so the browser's CORS preflight is
   rejected before a real GET is even attempted. icegate's own CORS
   (`access-control-allow-origin: *` on every `/v1/*` response) is
   confirmed working; this is the R2 bucket, not the gateway.

**Fix, not done here per the task's instructions**: add a CORS policy to the
`bioconice` R2 bucket allowing `GET`/`HEAD` (and `Range`, which DuckDB's
httpfs needs for partial reads) from the explorer's origin(s) — e.g.
`wrangler r2 bucket cors put bioconice --rules '[...]'` or the dashboard.
This is an infrastructure change to the bucket, tracked separately from this
PR and from icegate's own config.

**Consequence for this page**: the Query tab exists (`app.js`,
`buildQueryPanel`) but is **off by default**, gated behind a checkbox on the
Recipes page (persisted in `localStorage`, per-browser only — nothing is
sent anywhere). Turning it on lets a visitor see the exact failure above
rather than hiding it. `provenance.release`'s release×source×version matrix
(acceptance criterion 2) is therefore shown as a copyable `PIVOT` query, not
rendered inline, for the same reason — rendering it needs the same blocked
data read.

If a CORS policy is ever added to the bucket, flip the default in
`app.js` (`localStorage.getItem("bioconice_query_tab")`) and re-verify; the
code path is already exercised and only the bucket setting stands in the
way.

### Future option, not built here

A remote MCP server on Workers using R2 SQL (Cloudflare's SQL engine over R2
Data Catalog) is being built separately. If it lands, it could serve as a
server-side query fallback for this page's Query tab — issuing the read from
Cloudflare's edge instead of the visitor's browser, sidestepping the R2 CORS
gap entirely — but that is a design decision for whenever that server
exists, not something this PR implements.

## What's verified vs. what isn't

Every column doc, table comment, partition spec and snapshot on the page is
read live from the REST catalog at view time (`app.js`'s `api()`), never
copied from `src/bioconice/schemas.py`. The four recipe queries in
`public/recipes.js` were run against the live catalog with the `duckdb` CLI
on 2026-09-18 and the numbers in the "Verified" line under each one are what
DuckDB actually returned that day — re-run them yourself to check currency
(the human gene/transcript/exon counts in particular will drift as sources
update).

Acceptance criterion 4 ("every recipe's snippet ... checked in CI for one
recipe") and 4b (a live in-browser query returning the README's number) are
**not** wired into CI: this repo's tests stay offline (AGENTS.md — fixtures
over network calls), and criterion 4b additionally requires phase 2's data
path, which is blocked per the finding above. What ships instead:
`tests/test_explorer_recipes.py` checks that every recipe's SQL — and the
provenance matrix `PIVOT` — parses in DuckDB (`extract_statements`, no
network, no binding to real tables). The live numbers above were verified by
hand this session; they are not re-verified automatically.
