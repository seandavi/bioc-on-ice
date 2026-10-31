# biocOnIce Explorer

A static page that reads the public biocOnIce release
(`https://data.bioconice.cancerdatasci.org/latest`, ADR-0014) anonymously and
shows what's in it: namespaces, tables, table comments, column docs and types,
row counts, a copyable DuckDB snippet per table, an in-browser query box, and
the same fixed recipe queries issue #100's MCP `recipes()` tool serves. Plain
HTML/CSS/JS, no framework, no build step. Metadata comes from the release's
`manifest.json`; queries run in DuckDB-WASM, which `ATTACH`es the release's
`bioconice.duckdb` over https. The release Worker serves CORS on every file,
so nothing here needs a backend.

Before #193 the page read the icegate REST catalog instead, which also gave it
snapshot history, partition specs, business keys and licences. The release
does not carry those, so the page no longer shows them.

## Serve it locally

```sh
cd explorer/public
python -m http.server 8000
# open http://localhost:8000
```

No server-side code, no build, no `npm install` — the manifest and the data
come from the release and DuckDB-WASM from a CDN at request time.

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

## The Query tab

DuckDB-WASM is `@duckdb/duckdb-wasm@1.33.1-dev57.0` (npm's `latest` dist-tag;
DuckDB v1.5.4 inside), loaded from jsdelivr on the first Run. Its browser
build imports the bare specifier `apache-arrow`, which needs the static
`<script type="importmap">` in `index.html`. It attaches the release with

```sql
ATTACH 'https://data.bioconice.cancerdatasci.org/latest/bioconice.duckdb' AS bioc (READ_ONLY);
```

and the views read the Parquet files with HTTP range requests. The box takes
one read-only `SELECT`/`WITH` and caps results at 1,000 rows. `ATTACH` probes
for `bioconice.duckdb.wal`, which 404s and shows as one console error; DuckDB
treats a missing WAL as normal.

The 2026-09 attempt against icegate failed on CORS: the Iceberg bucket served
no CORS headers, so the tab shipped off by default. The release does not have
that problem, and the tab is now always on.

## What's verified vs. what isn't

Every comment, column doc and row count on the page is read from the release's
`manifest.json` at view time (`app.js`'s `loadManifest()`), never copied from
`src/bioconice/schemas.py`. The four recipe queries in `public/recipes.js` were
run against the 2026-10-31 release with Python duckdb 1.5.5, and the
"Verified" line under each is what DuckDB returned (unchanged from the
2026-09-18 run against icegate). On 2026-10-30 the page was served locally
(`python -m http.server`) and driven in headless Chromium: 80 tables in 7
namespaces loaded from the manifest, and the Query tab returned 78,348 for
`SELECT count(*) FROM bioc.annotation.gene WHERE taxon_id = 10090`, with the
`.wal` 404 above as the only console error.

`tests/test_explorer_recipes.py` checks offline that every recipe's SQL, and
the provenance matrix `PIVOT`, parses in DuckDB (`extract_statements`, no
network, no binding to real tables). The live numbers are not re-verified
automatically; this repo's tests stay offline (AGENTS.md).
