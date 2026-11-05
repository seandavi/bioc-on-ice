#!/usr/bin/env bash
# The release gate on just the migrated tables (#183, migration-log-step7.md step 5).
#
#   partial-gate.sh <bioc-on-ice worktree> <manifest.json> <out dir> <schema.table> ...
#
# Run from the scratch dir with the ADR-0015 local-backend env set. Builds a bundle of
# only those tables from the lake the env points at, cuts the reference manifest to the
# same tables (its release, published_at and git_sha kept as published), then runs the
# cdsci.publish.gate checks `bioconice gate` runs, directly: `bioconice gate` refuses a
# partial bundle and a same-day reference. Writes nothing
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
from cdsci.publish.gate import (bundle_con, bundle_checks, key_checks, print_report, readme_queries,
                                reference_checks, reference_covered)
from bioconice import gate
bundle, ref = Path(sys.argv[1]), sys.argv[2]
m = json.loads((bundle / "manifest.json").read_text())
r = json.loads(Path(ref, "manifest.json").read_text())
print(f"release {m['release']} against {ref} (release {r['release']}, "
      f"published_at {r['published_at']}, git_sha {r['git_sha']})")
names = {f"{t['schema']}.{t['table']}" for t in m["tables"]}
con = bundle_con(bundle, m["tables"], "bioc")
checks = bundle_checks(con, m["tables"], readme_queries(gate.README))
checks += reference_checks(m["tables"], r["tables"],
                           allow_column_change=os.environ.get("ALLOW_COLUMN_CHANGE", "").split())
for name, key in gate.KEYS.items():
    if name in names:
        checks += key_checks(con, name, key)
for name, refs in gate.REFERENCES.items():
    if name in names:
        checks += [reference_covered(con, name, col, target, names) for col, target in refs.items()]
checks += list(gate.contract_policy(con, m, r))
print_report(checks)
failed = sum(c.required and not c.passed for c in checks)
print(f"FAIL: {failed} required check(s) failed" if failed else "PASS")
sys.exit(1 if failed else 0)
EOF
