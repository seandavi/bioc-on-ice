# systemd units for biocOnIce's scheduled jobs

Source of truth for the units; the copies under `~/.config/systemd/user/` on
onclappc02 are deployment artifacts. The pattern is omicidx's
(`omicidx/systemd/`), and the convention and its rationale are
`monode/infrastructure/SCHEDULING.md`.

| Unit | Runs | Cadence |
|---|---|---|
| `bioconice-release` | `scripts/release.sh`: publish → gate → upload dry run → upload `--apply` | Sundays 00:00 UTC ([ADR-0016](../docs/adr/0016-release-cadence-weekly-r2-monthly-hf.md)) |

## What the release does

`scripts/release.sh` runs the README's "Release it" commands on
`$TMPDIR/bioconice-release-<UTC date>/` and stops at the first failing step,
so a bundle that fails the gate is never uploaded. Its output goes to the
journal and to `$TMPDIR/bioconice-release-<date>.log`. After a successful
upload it points the symlink `/data/davsean/bioconice-release` at the bundle;
`bioconice-hf-push.service` pushes whatever that names, so it only ever sees a
gated, uploaded bundle.

- Credentials come from GSM (project `cdsci-infra`) through `gcloud`, the same
  way as the manual flow, so the gcloud login on this host must be current.
  The unit sets `CU_OPENALEX_LAKE_BACKEND=postgres`; the repo needs no `.env`.
- The unit runs the checkout at `~/Documents/git/bioc-on-ice`, whatever is
  checked out there. Keep it on an up-to-date `main`.
- `TMPDIR=/data/davsean/tmp`: a bundle is 57–94 GB and `/tmp` here is 30 GB.
  `/data/davsean/tmp`'s 7-day cleanup removes old bundles.

## Failure signal

`OnFailure=ntfy-notify@%N.service`: a failed step or a `TimeoutStartSec`
timeout posts to the `cdsci-lake-ops` ntfy topic. The template is the shared
one from `cdsci-lake/systemd/` (already installed on onclappc02) and is not
copied here. The alert title is `cdsci-lake job failed:
bioconice-release.service`.

The notifier fetches its ntfy token from GSM through the same `gcloud` login
the release uses, so an expired login fails the release *and* silences its
alert. A missing alert is not a clean run: check `systemctl --user status
bioconice-release.service` and the notifier's own journal, `journalctl --user
-u ntfy-notify@bioconice-release.service`.

## Install (by hand: a live-system change, not CI)

```bash
# the units run from their own clone, reset to origin/main before each run:
git clone https://github.com/seandavi/bioc-on-ice ~/Documents/git/bioc-on-ice-release
cd ~/Documents/git/bioc-on-ice
cp systemd/bioconice-{release,hf-push}.{service,timer} ~/.config/systemd/user/
# once, shared, if not already present:
cp ~/Documents/git/cdsci-lake/systemd/ntfy-notify@.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start ntfy-notify@bioconice-release.service   # a test alert: check it arrives on cdsci-lake-ops
systemctl --user start bioconice-release.service     # one real run, end to end, first
journalctl --user -u bioconice-release.service -f
systemctl --user enable --now bioconice-release.timer bioconice-hf-push.timer
systemctl --user list-timers 'bioconice-*'           # next Sunday 00:00 UTC
```

Start the service once by hand before trusting the timer: `systemd-analyze
verify` passes a unit whose `ExecStart=` cannot be found. That run also
times the upload, which `TimeoutStartSec=` was not sized from; tighten it
from the journal. Copy, don't symlink: a symlinked unit goes dangling when
the repo moves and systemd silently stops loading it. Re-copy and
`daemon-reload` when a unit changes.

## Recovery

A failed run leaves its bundle in `/data/davsean/tmp/bioconice-release-<date>/`.
Only one run at a time: a run started while another holds
`$TMPDIR/bioconice-release.lock` waits for it, then sees `latest/` is today's
release and only prunes.

- **Upload failed** (network, R2): re-run `systemctl --user start
  bioconice-release.service`. A complete bundle (one whose `manifest.json`
  parses) is not rebuilt. `upload` skips a `v<date>/` already complete with
  this bundle (and never overwrites one of other files), and always rewrites
  `latest/`, so a retry also repairs a `latest/` half-overwritten by a later
  interrupted upload.
- **Pruning failed** (after `latest/` was uploaded): a re-run sees `latest/`
  is today's and runs only `upload --prune-only --apply`, which needs no
  bundle and prunes around the release `latest/` serves.
- **Gate failed**: read the report in the journal. A re-run gates the same
  bundle again. If the cause is in the data, fix it and delete the bundle
  directory so the re-run rebuilds it.
- **Publish failed or was killed**: the bundle has no `manifest.json` (publish
  removes a stale one first and writes the new one atomically, last), so a
  re-run rebuilds it. A truncated `manifest.json` from an older publish does
  not parse and is rebuilt too.

## One-off: a release that drops columns (#222)

The weekly script gates without `--allow-schema-change`, so a release that
drops the row-history columns (`valid_from`, `valid_to`, `landed_in`) is run
once by hand, serialized with the timer: pause it, wait out any run in
progress, and hold the script's own lock from publish to the applied upload.
Do this before #183 wave C, on a UTC date after `latest/`'s release.

Run it in one shell, step by step. `set -euo pipefail` stops at the first failure, and the
script's own lock is taken *first*: a weekly run in progress holds it for its whole
run, so `flock` waits that run out (the service is `Type=oneshot`, so `is-active`
would not).

```bash
set -euo pipefail
export TMPDIR=/data/davsean/tmp CU_OPENALEX_LAKE_BACKEND=postgres
systemctl --user stop bioconice-release.timer
exec 9>"$TMPDIR/bioconice-release.lock" && flock 9          # held until this shell exits
cd ~/Documents/git/bioc-on-ice-release
git fetch -q origin && git checkout -q --detach origin/main
dir=$TMPDIR/bioconice-release-$(date -u +%F)
# every table of latest/ that still has a row-history column (77 for v2026-11-01)
tables=$(curl -fsS https://data.bioconice.cancerdatasci.org/latest/manifest.json | python3 -c '
import json, sys
print(" ".join(t["schema"] + "." + t["table"] for t in json.load(sys.stdin)["tables"]
               if {"valid_from", "valid_to", "landed_in"} & {c["name"] for c in t["columns"]}))')
uv run bioconice publish --out "$dir"
uv run bioconice gate --release "$dir" --allow-schema-change $tables
uv run bioconice upload --release "$dir"           # dry run: read the plan before going on
uv run bioconice upload --release "$dir" --apply
ln -sfn "$dir" /data/davsean/bioconice-release
# latest/ is this bundle's release, and no table has a row-history column
curl -fsS https://data.bioconice.cancerdatasci.org/latest/manifest.json | python3 -c '
import json, sys
m, want = json.load(sys.stdin), json.load(open(sys.argv[1] + "/manifest.json"))["release"]
left = [t["schema"] + "." + t["table"] for t in m["tables"]
        if {"valid_from", "valid_to", "landed_in"} & {c["name"] for c in t["columns"]}]
assert m["release"] == want, (m["release"], want)
assert not left, left' "$dir"
systemctl --user start bioconice-release.timer
exit                                                 # releases the lock
```

If any step fails, `set -e` ends the shell there: the lock is released and the timer
stays stopped. Fix the cause, then run the block again from the top.
