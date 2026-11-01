#!/usr/bin/env bash
# The weekly release (ADR-0016, #213): the README's "Release it" commands in order,
# stopping at the first failing step, so a bundle that fails the gate is never uploaded.
# Run by systemd/bioconice-release.service; by hand, `uv run scripts/release.sh`
# (`uv run` puts the venv's `bioconice` on PATH).
#
# Credentials are the manual flow's: cdsci-lake reads the lake and R2 secrets from GSM
# (project cdsci-infra) with gcloud. Reading the shared lake needs
# CU_OPENALEX_LAKE_BACKEND=postgres, which the unit sets.
#
# Safe to re-run on the same date. A bundle with manifest.json (publish writes it last) is
# complete, so it is gated and uploaded again rather than rebuilt; upload never overwrites a
# completed v<date>/ and only finishes latest/ if that was interrupted. Once latest/ is
# today's release there is nothing left to do (and the gate would refuse it as reference).
set -euo pipefail

public=https://data.bioconice.cancerdatasci.org
today=$(date -u +%F)
dir=${TMPDIR:?set TMPDIR to scratch with room for a bundle (~94 GB)}/bioconice-release-$today
exec > >(tee -a "$dir.log") 2>&1
log() { echo "$(date -u +%FT%TZ) release $today: $*"; }

latest=$(curl -fsS "$public/latest/manifest.json" | python3 -c 'import json, sys; print(json.load(sys.stdin)["release"])')
if [ "$latest" = "$today" ]; then
  log "latest/ is already release $today; nothing to do"
  exit 0
fi

if [ -f "$dir/manifest.json" ]; then
  log "complete bundle already in $dir; not rebuilding it"
else
  log "publish -> $dir"
  bioconice publish --out "$dir"
fi
log "gate against latest/ (release $latest)"
bioconice gate --release "$dir"
log "upload, dry run"
bioconice upload --release "$dir"
log "upload --apply"
bioconice upload --release "$dir" --apply
log "done"
