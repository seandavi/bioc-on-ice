#!/usr/bin/env bash
# The release gate on just the migrated tables (#183, migration-log-step7.md step 5).
#
#   partial-gate.sh <bioc-on-ice worktree> <manifest.json> <out dir> <schema.table> ...
#
# Run from the scratch dir with the ADR-0015 local-backend env set. Builds a bundle of
# only those tables from the lake the env points at, cuts the reference manifest to the
# same tables and dates it yesterday (`bioconice gate` refuses a reference that is not
# older than the bundle), then runs `bioconice gate`. Writes nothing outside <out dir>.
set -euo pipefail
W=$1 REF=$2 OUT=$3; shift 3
mkdir -p "$OUT/bundle" "$OUT/ref"
uv run --project "$W" bioconice publish --out "$OUT/bundle" --only "$@"
jq --arg d "$(date -u -d yesterday +%F)" \
   '.release = $d | .tables |= map(select((.schema + "." + .table) as $n | $ARGS.positional | index($n)))' \
   --args "$@" < "$REF" > "$OUT/ref/manifest.json"
uv run --project "$W" bioconice gate --release "$OUT/bundle" --reference "$OUT/ref"
