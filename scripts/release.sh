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
# Safe to re-run on the same date, but one run at a time: a second one waits for the
# first's lock, so it cannot rewrite a bundle the first has gated, then finds today's
# release done and only prunes. A bundle whose manifest.json parses is complete (publish
# writes it last, atomically), so it is gated and uploaded again rather than rebuilt;
# upload never overwrites a completed v<date>/ and always rewrites latest/. Once latest/
# is today's release the gate would refuse it as reference, so a re-run only runs
# `upload --prune-only --apply` (an earlier prune may have failed).
set -euo pipefail

public=https://data.bioconice.cancerdatasci.org
today=$(date -u +%F)
dir=${TMPDIR:?set TMPDIR to scratch with room for a bundle (~94 GB)}/bioconice-release-$today
# The newest uploaded (so gated) bundle, for the monthly hf-push unit (#214), which runs the
# day after the first Sunday's release: well inside TMPDIR's 7-day cleanup.
link=${BIOCONICE_RELEASE_LINK:-/data/davsean/bioconice-release}
exec 9>"$TMPDIR/bioconice-release.lock"
flock 9
exec > >(tee -a "$dir.log") 2>&1
log() { echo "$(date -u +%FT%TZ) release $today: $*"; }
complete() { python3 -c 'import json, sys; json.load(open(sys.argv[1]))["tables"]' "$dir/manifest.json" 2>/dev/null; }

latest=$(curl -fsS "$public/latest/manifest.json" | python3 -c 'import json, sys; print(json.load(sys.stdin)["release"])')
if [ "$latest" = "$today" ]; then
  log "latest/ is already release $today; only pruning"
  bioconice upload --prune-only --apply
  exit 0
fi

if complete; then
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
ln -sfn "$dir" "$link"
log "done; $link -> $dir"
