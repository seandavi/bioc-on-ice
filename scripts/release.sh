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
# Safe to re-run on the same date, but one run at a time: a second one exits 1 while the
# first holds the lock, so it cannot rewrite a bundle the first has gated. A bundle whose
# manifest.json parses is complete (publish writes it last, atomically), so it is gated
# and uploaded again rather than rebuilt; upload skips a folder already complete with
# that bundle and never overwrites a completed v<date>/. Once latest/ is today's release
# the gate would refuse it as reference, so a re-run only re-runs upload, which then just
# prunes expired releases (an earlier prune may have failed).
set -euo pipefail

public=https://data.bioconice.cancerdatasci.org
today=$(date -u +%F)
dir=${TMPDIR:?set TMPDIR to scratch with room for a bundle (~94 GB)}/bioconice-release-$today
exec 9>"$TMPDIR/bioconice-release.lock"
flock -n 9 || { echo "another release run holds $TMPDIR/bioconice-release.lock" >&2; exit 1; }
exec > >(tee -a "$dir.log") 2>&1
log() { echo "$(date -u +%FT%TZ) release $today: $*"; }
complete() { python3 -c 'import json, sys; json.load(open(sys.argv[1]))["tables"]' "$dir/manifest.json" 2>/dev/null; }

latest=$(curl -fsS "$public/latest/manifest.json" | python3 -c 'import json, sys; print(json.load(sys.stdin)["release"])')
if [ "$latest" = "$today" ]; then
  if complete; then
    log "latest/ is already release $today; upload --apply to finish pruning"
    bioconice upload --release "$dir" --apply
  else
    log "latest/ is already release $today and $dir has no complete bundle; pruning waits for next week"
  fi
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
log "done"
