#!/usr/bin/env bash
# The release gate on just the migrated tables (#183, migration-log-step7.md step 5).
#
#   partial-gate.sh <bioc-on-ice worktree> <manifest.json> <out dir> <schema.table> ...
#
# Run from the scratch dir with the ADR-0015 local-backend env set. Builds a bundle of
# only those tables from the lake the env points at, cuts the reference manifest to the
# same tables (its release, published_at and git_sha kept as published), then calls
# `verify_release_over_release` and the bundle's own manifest checks directly:
# `bioconice gate` refuses a partial bundle and a same-day reference. Writes nothing
# outside <out dir>. ALLOW_COLUMN_CHANGE="<schema.table> ..." passes those tables'
# column change, as `bioconice gate --allow-column-change` does (owner approval, #183).
set -euo pipefail
W=$1 REF=$2 OUT=$3; shift 3
mkdir -p "$OUT/bundle" "$OUT/ref"
uv run --project "$W" bioconice publish --out "$OUT/bundle" --only "$@"
jq '.tables |= map(select((.schema + "." + .table) as $n | $ARGS.positional | index($n)))' \
   --args "$@" < "$REF" > "$OUT/ref/manifest.json"
uv run --project "$W" python - "$OUT/bundle" "$OUT/ref" <<'EOF'
import json, os, sys
from pathlib import Path
from bioconice import gate
bundle, ref = Path(sys.argv[1]), sys.argv[2]
m = json.loads((bundle / "manifest.json").read_text())
r = json.loads(Path(ref, "manifest.json").read_text())
print(f"release {m['release']} against {ref} (release {r['release']}, "
      f"published_at {r['published_at']}, git_sha {r['git_sha']})")
checks = list(gate.verify_release_over_release(
    "bioconice", bundle, ref, gate.CONTRACTS, gate.UNCONTRACTED_TABLES).checks)
checks, changes = gate.allow_changed_columns(
    checks, os.environ.get("ALLOW_COLUMN_CHANGE", "").split(), r, m)
for t, d in changes.items():
    print(f"column change allowed (owner approval, #183): {t}: {gate._describe(d)}")
checks += gate.bundle_checks(gate.release_con(bundle, m["tables"]), m["tables"],
                             gate.readme_queries())
for c in checks:
    status = "ok" if c.passed else "FAIL" if c.required else "warn"
    print(f"  {status:4}  {c.name}" + (f"  ({c.detail})" if c.detail else ""))
failed = sum(c.required and not c.passed for c in checks)
print(f"FAIL: {failed} required check(s) failed" if failed else "PASS")
sys.exit(1 if failed else 0)
EOF
