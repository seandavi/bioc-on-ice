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

## Usage

Every bucket request on either host writes one Analytics Engine data point
(`src/analytics.ts`): `blob1` object key (the same on both hosts, which are
not told apart), `blob2` user agent, `blob3` hashed IP, `blob4` method,
`double1` status, `double2` body bytes (a range's length; 0 for HEAD). Query
it with the SQL API using an API token with *Account Analytics: Read*
(account id from `npx wrangler whoami`):

```bash
curl -s "https://api.cloudflare.com/client/v4/accounts/$CF_ACCOUNT_ID/analytics_engine/sql" \
  -H "Authorization: Bearer $CF_API_TOKEN" \
  --data-binary @query.sql
```

Rows are sampled: count with `sum(_sample_interval)`, never `count()`;
`count(DISTINCT blob3)` is a lower bound. Retention is three months. The
dialect has no `CASE` (use `if`) and `position` is `position(x IN s)`.

Requests and bytes per release table (`latest` or `v<date>`):

```sql
SELECT
  substring(blob1, 1, position('/' IN blob1) - 1) AS release,
  substring(blob1, position('/' IN blob1) + 1,
            length(blob1) - position('/' IN blob1) - 8) AS schema_table,
  sum(_sample_interval) AS requests,
  sum(_sample_interval * double2) AS bytes
FROM data_bioconice_usage
WHERE endsWith(blob1, '.parquet') AND blob4 = 'GET'
  AND timestamp > NOW() - INTERVAL '30' DAY
GROUP BY release, schema_table
ORDER BY bytes DESC
FORMAT TabSeparated
```

Distinct clients (hashed IPs) per table per week:

```sql
SELECT toStartOfWeek(timestamp) AS week, blob1 AS object_key,
       count(DISTINCT blob3) AS clients
FROM data_bioconice_usage
WHERE endsWith(blob1, '.parquet') AND timestamp > NOW() - INTERVAL '90' DAY
GROUP BY week, object_key
ORDER BY week DESC, clients DESC
FORMAT TabSeparated
```

Requests by client (DuckDB sends `duckdb/<version>(<platform>) <api>`):

```sql
SELECT
  if(startsWith(blob2, 'duckdb/'),
     if(position('r-dbi' IN blob2) > 0, 'DuckDB from R',
        if(position('python' IN blob2) > 0, 'DuckDB from Python', 'DuckDB, other')),
     if(position('bot' IN lower(blob2)) > 0, 'bot',
        if(startsWith(blob2, 'Mozilla/'), 'browser', 'other'))) AS client,
  sum(_sample_interval) AS requests,
  count(DISTINCT blob3) AS clients
FROM data_bioconice_usage
WHERE timestamp > NOW() - INTERVAL '30' DAY
GROUP BY client
ORDER BY requests DESC
FORMAT TabSeparated
```

## Develop and deploy

```bash
cd site
npm ci
npm test               # route, range, conditional and analytics unit tests
npm run typecheck
npx wrangler dev       # local Worker, real bucket (remote R2 binding)
npx wrangler login
npx wrangler deploy
```

CI runs the tests and typecheck (`site` job). Deploy waits for the first
bundle (#192). To try the data host locally:
`npx wrangler dev --local-upstream data.bioconice.cancerdatasci.org`.
