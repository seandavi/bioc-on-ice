# Deploying the gateway

biocOnIce is served by one Cloudflare Worker, **`icegate-bioconice`**, at
`https://icegate-bioconice.seandavi.workers.dev`. It is the unmodified
[icegate](https://github.com/seandavi/icegate) code with this repo's
`icegate.yaml` baked in as its config. Nothing in this repo runs on the
gateway; deploying means shipping a new config (or new icegate code) as a
new Worker version. Reconstructed on 2026-09-18 from the Worker's deployment
history, its secrets, the icegate operator guide and the August handoff notes.

## What the Worker is made of

| Piece | Where it comes from | Notes |
| --- | --- | --- |
| Code | the `icegate` checkout (`src/index.ts`, `wrangler.jsonc`) | `wrangler.jsonc` names the Worker `icegate`; the bioconice one is deployed with `--name icegate-bioconice`. **Never deploy without the name**: `icegate` is the live omicidx gateway. |
| Config | this repo's `icegate.yaml`, copied over icegate's `config.yaml` at deploy time | Bundled by wrangler's `Text` rule and memoised on the first `/v1/*` request; never re-read. Changing `icegate.yaml` on main changes nothing until redeployed. |
| Secrets | Workers secrets, set once with `wrangler secret put` | `CF_ACCOUNT_ID`, `R2_CATALOG_PREFIX`, `CF_API_TOKEN_RO` (bucket-scoped read-only vending token, ADR-0011; Secret Manager `bioconice-cf-vending-ro`), `CF_API_TOKEN` (the write token; still the legacy Admin token per the comment in `icegate.yaml`). Each `${VAR}` in the yaml resolves to one of these. |
| Logs | `logpush: true` in `wrangler.jsonc` → the account's Logpush job → GCS (ADR-0010, bioc-on-ice#32) | Watched by the `bioc-logpush-check` timer on onclappc02. |

Deploy credentials, both in Secret Manager project `cdsci-infra`:
`cdsci-cloudflare-workers-token` (Workers deploy rights) and
`cdsci-r2-account-id`. The write key for ingest is unrelated to deployment:
`bioconice-icegate-key-seandavi` (and `-ro`), whose SHA-256 digests are the
`api_keys` in `icegate.yaml`.

## The Worker family

| Worker | URL | Config | Deploy |
| --- | --- | --- | --- |
| `icegate-bioconice` | https://icegate-bioconice.seandavi.workers.dev | this repo's `icegate.yaml` + the icegate checkout | `scripts/deploy-icegate.sh` |
| `bioconice-explorer` | https://bioconice-explorer.seandavi.workers.dev | `explorer/wrangler.jsonc` (static assets only, no script) | `cd explorer && npx wrangler@4 deploy` with the same two credentials |

The explorer reads the catalog anonymously through icegate, so it has no secrets. Its
DuckDB-WASM query tab stays off by default until the `bioconice` R2 bucket gets a CORS
policy allowing browser reads (explorer/README.md). First deployed 2026-09-18.

The MCP server is **not** a Worker: it runs on onclappc02 as a docker compose service
behind Traefik (`mcp/README.md`), the platform's pattern for services that need a real
engine.

## Deploy

```sh
scripts/deploy-icegate.sh --dry-run   # bundles, prints size, deploys nothing
scripts/deploy-icegate.sh             # deploys, then verifies /health, /v1/config, namespaces
```

The script refuses to run with uncommitted changes to `icegate.yaml` (the
deployed config should be a git state), copies the yaml into the icegate
checkout (sibling directory, or `ICEGATE_DIR`), deploys under the right name,
restores icegate's own `config.yaml`, and probes the live endpoint. It does
not touch secrets. A deploy takes about a minute; there is no downtime, and
already-served requests are unaffected because config resolution is lazy.

Rollback is wrangler's: `npx wrangler rollback --name icegate-bioconice`
in the icegate checkout, with the same two environment variables.

## After a deploy

* `curl https://icegate-bioconice.seandavi.workers.dev/v1/bioconice/namespaces`
  lists the namespaces the **anonymous** principal may see; a namespace that
  exists in the catalog but is missing here is a config that was not deployed.
* A write to a newly granted namespace is the real test for a key change:
  `uv run bioconice tables` with `BIOCONICE_TOKEN` set lists through the key.
* Rotating or adding an API key: generate (`openssl rand -hex 32` with the
  `icegate_` prefix per the operator guide §3), store the plaintext in Secret
  Manager, put its SHA-256 in `icegate.yaml`, commit, deploy. Plaintext exists
  once, at issuance.
* Rotating a backend token: `printf %s "$VALUE" | npx wrangler secret put NAME --name icegate-bioconice`;
  that creates a new Worker version by itself, no code deploy needed.

## History

| When (UTC) | What |
| --- | --- |
| 2026-08-06 22:32 | first upload; secrets set minutes later |
| 2026-08-07 01:39 – 15:57 | secret changes and redeploys: the read-only Bioconductor key, the provenance grant, anonymous public read (ADR-0011, SPEC C4), the two-token backend |
| 2026-09-18 | `icegate.yaml` on main gains the `resource` namespace (#88); **not yet deployed** |

## Known gaps (2026-09-18)

1. **Config drift.** The live Worker predates the `resource` grants, so the
   CELLxGENE and BEDbase loads 403 until the next deploy.
2. **`logpush: true` is uncommitted** in the icegate checkout's `wrangler.jsonc`
   (enabled via the API on 2026-08-07). A fresh clone would deploy without it and
   silently stop the access logs. It belongs in a commit to the icegate repo.
3. **No uptime check.** The platform convention for always-on services
   (`monode/infrastructure/OBSERVABILITY.md`) is a GCP uptime check with
   alerting; the gateway has none. `/health` is the endpoint to watch, and
   `/v1/config?warehouse=bioconice` is the one that proves config resolution.
4. **`CF_API_TOKEN` is still Admin-level.** The write path should move to a
   bucket-scoped RW vending token once minted (icegate#34), after which nothing
   Admin-level remains in the Worker.
5. A stale wrangler OAuth token from January sits in
   `~/.config/.wrangler/config/default.toml`; deploys never used it.
