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

The bucket holds the whole-product bundle decided in map #187, in the omicidx
style: `latest/` plus dated `v{YYYY-MM-DD}/` folders, each with
`<schema>/<table>.parquet`, `bioconice.duckdb`, `views.sql`, `restore.sql` and
a `manifest.json`. The releases page lists those folders and, when one is
opened, its tables from `manifest.json` (`schema`, `table`, `row_count`,
`bytes`; a missing `bytes` falls back to the bucket listing of `<schema>/`, a
missing `row_count` shows "–"). The field names are read in one place,
`manifestOf` in `public/releases.html`, to reconcile with the builder (#189).

## Develop and deploy

```bash
cd site
npm ci
npm test               # route, range and conditional unit tests
npm run typecheck
npx wrangler dev       # local Worker, real bucket (remote R2 binding)
npx wrangler login
npx wrangler deploy
```

CI runs the tests and typecheck (`site` job). Deploy waits for the first
bundle (#192). To try the data host locally:
`npx wrangler dev --local-upstream data.bioconice.cancerdatasci.org`.
