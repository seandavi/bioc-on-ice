#!/usr/bin/env bash
# Deploy this catalog's gateway config to the `icegate-bioconice` Cloudflare Worker.
#
# The Worker is the icegate code with THIS repo's icegate.yaml baked in as its
# config.yaml (wrangler's Text rule bundles it at deploy time; it is never read
# from disk at runtime). Redeploy = rerun this after changing icegate.yaml.
#
# Never deploy under the name `icegate`: that is the live omicidx gateway.
#
# Needs: gcloud access to project cdsci-infra, node + npx, and a sibling
# checkout of https://github.com/seandavi/icegate (ICEGATE_DIR to override).
# Secrets already set on the Worker (CF_ACCOUNT_ID, CF_API_TOKEN,
# CF_API_TOKEN_RO, R2_CATALOG_PREFIX) are untouched — see docs/DEPLOY.md to
# rotate one.
#
# Usage: scripts/deploy-icegate.sh [--dry-run]
set -euo pipefail
HERE=$(cd "$(dirname "$0")/.." && pwd)
ICEGATE_DIR=${ICEGATE_DIR:-$HERE/../icegate}
WORKER=icegate-bioconice
DRY=${1:-}

[ -f "$ICEGATE_DIR/wrangler.jsonc" ] || { echo "no icegate checkout at $ICEGATE_DIR" >&2; exit 1; }
if [ -n "$(git -C "$HERE" status --short icegate.yaml)" ]; then
  echo "icegate.yaml has uncommitted changes; commit (or stash) first so the deployed config is a git state" >&2
  exit 1
fi
grep -q '\${CF_ACCOUNT_ID}' "$HERE/icegate.yaml" || { echo "icegate.yaml lacks \${CF_ACCOUNT_ID}; refusing" >&2; exit 1; }

export CLOUDFLARE_API_TOKEN=${CLOUDFLARE_API_TOKEN:-$(gcloud secrets versions access latest --secret cdsci-cloudflare-workers-token --project cdsci-infra)}
export CLOUDFLARE_ACCOUNT_ID=${CLOUDFLARE_ACCOUNT_ID:-$(gcloud secrets versions access latest --secret cdsci-r2-account-id --project cdsci-infra)}

cd "$ICEGATE_DIR"
cp config.yaml config.yaml.pre-bioconice
trap 'mv -f config.yaml.pre-bioconice config.yaml' EXIT
cp "$HERE/icegate.yaml" config.yaml
echo "==> deploying $HERE/icegate.yaml ($(git -C "$HERE" rev-parse --short HEAD)) as Worker $WORKER"
if [ "$DRY" = "--dry-run" ]; then
  npx wrangler deploy --name "$WORKER" --dry-run --outdir "$(mktemp -d)"
  exit 0
fi
npx wrangler deploy --name "$WORKER"

echo "==> verifying"
URL=https://$WORKER.seandavi.workers.dev
curl -sf "$URL/health" >/dev/null && echo "    /health ok"
curl -sf "$URL/v1/config?warehouse=bioconice" | python3 -c 'import json,sys; c=json.load(sys.stdin); print("    /v1/config prefix:", c["overrides"]["prefix"])'
curl -sf "$URL/v1/bioconice/namespaces" | python3 -c 'import json,sys; print("    anonymous namespaces:", [n[0] for n in json.load(sys.stdin)["namespaces"]])'
