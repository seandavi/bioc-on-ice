# bioconice-mcp: hosting the MCP server

The server is `src/bioconice/mcp.py` (issue #100) — one implementation, two
transports. This directory is only the packaging to run it as an always-on
docker-compose service on onclappc02, behind Traefik, per the platform's
standard pattern (`monode/infrastructure/compose/ntfy/` is the template this
follows).

## Run it locally

```sh
uv run bioconice-mcp              # stdio — what uvx/Claude Desktop/Code use
uv run bioconice-mcp --http       # streamable HTTP on :8000, plus GET /health
```

## Docker

```sh
docker build -f mcp/Dockerfile -t bioconice-mcp .   # context is the repo root
docker run --rm -p 8000:8000 bioconice-mcp
curl http://localhost:8000/health                    # {"status":"ok","release":"2026.09"}
```

Both were run and verified live on 2026-09-18: `docker build` succeeds
(~40s, no cache), the container's `/health` returns the current release from
`provenance.release` through the real anonymous icegate endpoint, and a raw
`POST /mcp` `initialize` request gets a normal MCP handshake back. Compose
itself (`docker compose up -d`) was not run here — that happens on
onclappc02 in the deploy step below, not in this checkout.

## Deploy to onclappc02

Follows `monode/infrastructure/compose/README.md`'s "Adding a New Service"
checklist exactly (grey-cloud hostname, Let's Encrypt via the `cloudflare`
certresolver — no Cloudflare Access, no proxying). None of this was run by
this change; it is the checklist for whoever deploys it.

1. **DNS**: add an A record `bioconice-mcp.cancerdatasci.org` → `140.226.4.71`
   in Cloudflare, **grey cloud** (DNS-only) — done in `monode/terraform`, not
   here.
2. **Copy this directory** to
   `monode/infrastructure/compose/bioconice-mcp/` (keep `Dockerfile` and
   `docker-compose.yml` together; the compose file's `build.context: ..`
   assumes it sits one level below a checkout of this repo — adjust the
   context to wherever this repo is cloned on the host, e.g. a sibling
   `bioc-on-ice/` checkout, matching how `scripts/deploy-icegate.sh` expects
   a sibling `icegate/` checkout).
3. `docker compose up -d --build` from that directory. Traefik auto-discovers
   the container from its labels and requests the LE cert on first HTTPS hit.
4. **Add a row to `monode/infrastructure/INDEX.md`**'s "Public services"
   table: hostname `bioconice-mcp.cancerdatasci.org`, container
   `bioconice-mcp`, port 8000, grey, LE, this compose file.
5. **Add a GCP uptime check** on `/health` per
   `monode/infrastructure/terraform/OBSERVABILITY.md` — copy its
   `google_monitoring_uptime_check_config` + `google_monitoring_alert_policy`
   pattern, `validate_ssl: true`, probing `https://bioconice-mcp.cancerdatasci.org/health`.

No auth in v1: every tool is read-only against data that's already
anonymously public through icegate. If that ever needs to change, Cloudflare
Access in front of the same hostname (flip to orange-cloud + an Access
policy) is the platform's existing option — no code change.

## Ask an assistant

**Remote (once deployed):**

```json
{"mcpServers": {"bioconice": {"url": "https://bioconice-mcp.cancerdatasci.org/mcp"}}}
```

**Local (no deploy needed), same config either client reads:**

```json
{"mcpServers": {"bioconice": {"command": "uvx", "args": ["--from", "git+https://github.com/seandavi/bioc-on-ice", "bioconice-mcp"]}}}
```

See the repository README's "Ask an assistant" section for exactly where
each client (Claude Desktop, Claude Code) reads this from.

## Paths not taken

Three other hosting shapes were spiked before landing on the docker-compose
service above; recorded here so nobody repeats the dead ends.

**Cloudflare Worker + R2 SQL** (query tables directly via Cloudflare's
serverless SQL engine over R2 Data Catalog, no DuckDB at all): ruled out
before implementation — neither of the project's two Cloudflare API tokens is
authorised for the R2 SQL API (`80013 Unauthorized`), and R2 SQL's engine
lacks recursive CTEs, which the cell-type ontology rollup recipe needs.

**Cloudflare Python Worker running DuckDB directly** (Pyodide's bundled
`duckdb` package, no container): spiked locally with `npx wrangler dev`
(wrangler 4.134.0, `compatibility_flags: ["python_workers"]`). A bare
`async def on_fetch(request)` module is rejected (current wrangler expects a
`class Default(WorkerEntrypoint)` with a `fetch` method); fixing that, the
Worker runs, but `import duckdb` fails with `ModuleNotFoundError` even after
declaring `duckdb` in a `pyproject.toml`/`requirements.txt` next to the
Worker — `wrangler dev` lists `requirements.txt` as an attached module in its
bundle report but does not actually vendor the package into the Pyodide
runtime the way `micropip`/a real build step would. Whether a full
`wrangler deploy` (not just local `dev`) resolves this is untested — doing so
needs an actual Cloudflare account and deploy credentials, which this change
deliberately does not touch. Given `duckdb`'s `iceberg`/`httpfs` *extensions*
are separately-fetched native binaries on top of the base package, and
Workers' sandboxed dev runtime already can't resolve the base import, this
path did not look close enough to working to chase further under a 30-minute
time-box.

**Cloudflare Worker + Container** (a TypeScript Worker routing to a
Cloudflare Container running this same Python image): architecturally sound
and was partially scaffolded, but superseded before finishing once the
onclappc02 docker-compose pattern — already the platform's standard place for
an always-on service like this one, per `monode/infrastructure/INDEX.md` —
turned out to need nothing this repo doesn't already have (docker, this
Dockerfile) and no new Cloudflare product surface.

## Health and logs

| Endpoint | Meaning | Use |
| --- | --- | --- |
| `GET /health/live` | the process is up; never touches the lake | the external uptime check — a failure here means the container died |
| `GET /health` (alias `/health/ready`) | icegate reachable and a release resolvable through DuckDB | the compose healthcheck; a 503 here with `/health/live` green means the gateway, not us |

Logs on stdout (`docker logs bioconice-mcp`): uvicorn's access lines, plus **one JSON line per
tool call** — `{"tool", "args", "ms", "ok", "rows", "truncated", "error"}` — which is what tells
you how the server is used. Request-level access logs are Traefik's, already shipped to
ClickHouse by Vector (monode `TELEMETRY.md`).


## How it is reached (as deployed 2026-09-18)

`https://bioconice-mcp.cancerdatasci.org` is a **proxied** (orange-cloud) Cloudflare DNS record
pointing at onclappc02. Cloudflare terminates TLS at its edge; Traefik serves its default Origin CA
certificate to Cloudflare and routes `Host(bioconice-mcp.cancerdatasci.org)` to this container on
the `proxy` network. This is the orange-cloud row of monode's `compose/README.md` "TLS strategy":
no `certresolver` label. The grey-cloud + Let's Encrypt row was tried first and Traefik never
initiated issuance for this host (no ACME entry, nothing logged at INFO), so we switched rather
than debug the shared proxy. Consequence to know: Cloudflare's proxy closes idle responses after
100 s; MCP tool calls finish in seconds, but a very long streamed response would be cut.

Client config: `{"mcpServers": {"bioconice": {"url": "https://bioconice-mcp.cancerdatasci.org/mcp"}}}`.
Health: `/health/live` (process), `/health` (lake reachable). Logs: `docker logs bioconice-mcp`.
