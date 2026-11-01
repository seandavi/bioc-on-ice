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
journal and to `$TMPDIR/bioconice-release-<date>.log`.

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
cd ~/Documents/git/bioc-on-ice
cp systemd/bioconice-release.{service,timer} ~/.config/systemd/user/
# once, shared, if not already present:
cp ~/Documents/git/cdsci-lake/systemd/ntfy-notify@.service ~/.config/systemd/user/
systemctl --user daemon-reload
systemctl --user start ntfy-notify@bioconice-release.service   # a test alert: check it arrives on cdsci-lake-ops
systemctl --user start bioconice-release.service     # one real run, end to end, first
journalctl --user -u bioconice-release.service -f
systemctl --user enable --now bioconice-release.timer
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
`$TMPDIR/bioconice-release.lock` exits 1 (and alerts) without touching anything.

- **Upload failed** (network, R2): re-run `systemctl --user start
  bioconice-release.service`. A complete bundle (one whose `manifest.json`
  parses) is not rebuilt, and `upload` never overwrites a completed
  `v<date>/`: it skips any folder already complete with this bundle and
  finishes the rest.
- **Pruning failed** (after `latest/` was uploaded): a re-run sees `latest/`
  is today's and re-runs only `upload --apply`, which skips both folders and
  prunes. If the bundle is gone, next week's run prunes instead.
- **Gate failed**: read the report in the journal. A re-run gates the same
  bundle again. If the cause is in the data, fix it and delete the bundle
  directory so the re-run rebuilds it.
- **Publish failed or was killed**: the bundle has no `manifest.json` (publish
  removes a stale one first and writes the new one atomically, last), so a
  re-run rebuilds it. A truncated `manifest.json` from an older publish does
  not parse and is rebuilt too.
