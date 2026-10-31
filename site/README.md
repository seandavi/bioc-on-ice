# bioc-on-ice site Worker

One Cloudflare Worker (named `data-bioconice`), copied from omicidx `site/`,
serves the whole public site (#191):

| URL | Serves |
|---|---|
| `https://bioconice.cancerdatasci.org/` | pages in `public/` (Workers static assets): landing and `/releases` |
| `https://bioconice.cancerdatasci.org/data/*` | the `data-bioconice` R2 bucket, `/data` stripped from the key |
| `https://data.bioconice.cancerdatasci.org/*` | the same bucket at `/` |

The data host must keep serving bytes, never redirect: release bundle views
(`bioconice.duckdb`, `views.sql`) hard-code its URLs. Routing lives in
`src/route.ts`.

The bucket paths are the anonymous, credential-free read surface for the
frozen releases: plain HTTPS with range requests, CORS, directory listing
(HTML or JSON by `Accept`) and Analytics Engine logging to
`data_bioconice_usage`. The range, conditional-request and DuckDB-Wasm
`If-Match` handling came from omicidx with their tests.

The releases page lists each release folder (`latest/`, `v{YYYY-MM-DD}/`) and,
when one is opened, its tables from `manifest.json` (`schema`, `table`,
`row_count`, `bytes`); a table without `bytes` falls back to the bucket
listing of its `<schema>/` folder.

## Develop and deploy

```bash
cd site
npm ci
npm test               # route, range and conditional unit tests
npm run typecheck
npm run seed           # put test/fixture/manifest.json into local R2
npx wrangler dev       # local Worker, local R2: / and /releases
npx wrangler login
npx wrangler deploy    # needs the data-bioconice bucket
```

`npm run seed` writes the fixture as `latest/` and `v2026-10-30/` manifests,
plus a stand-in `latest/reference/genome.parquet` (the manifest's bytes) so
the table without `bytes` exercises the listing fallback. `wrangler dev` uses
local R2 by default; add `remote = true` to the `[[r2_buckets]]` block to read
the real bucket. To try the data host locally:
`npx wrangler dev --local-upstream data.bioconice.cancerdatasci.org`.
