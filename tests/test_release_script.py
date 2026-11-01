"""scripts/release.sh, the weekly release chain (#213), with `bioconice` and `curl` stubbed."""

import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "release.sh"
TODAY = datetime.now(UTC).date().isoformat()


def _run(tmp_path, latest="2026-10-25", fail=None):
    """Run the script; the stub `bioconice` logs its arguments and exits 1 on `fail`."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "curl").write_text(f"#!/bin/sh\necho '{{\"release\": \"{latest}\"}}'\n")
    (bin_ / "bioconice").write_text(
        f'#!/bin/sh\necho "$*" >> {tmp_path}/calls\n[ "$1" != "{fail}" ]\n')
    for f in bin_.iterdir():
        f.chmod(0o755)
    env = {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "TMPDIR": str(tmp_path)}
    r = subprocess.run([str(SCRIPT)], env=env, capture_output=True, text=True)
    calls = tmp_path / "calls"
    return r.returncode, calls.read_text().splitlines() if calls.exists() else []


def test_release_runs_publish_gate_dry_run_then_apply(tmp_path):
    rc, calls = _run(tmp_path)
    d = tmp_path / f"bioconice-release-{TODAY}"
    assert rc == 0
    assert calls == [f"publish --out {d}", f"gate --release {d}",
                     f"upload --release {d}", f"upload --release {d} --apply"]
    assert (tmp_path / f"bioconice-release-{TODAY}.log").read_text().endswith("done\n")


@pytest.mark.parametrize("fail", ["publish", "gate"])
def test_release_never_uploads_after_a_failed_step(tmp_path, fail):
    rc, calls = _run(tmp_path, fail=fail)
    assert rc != 0
    assert calls[-1].startswith(fail)
    assert not any(c.startswith("upload") for c in calls)


def test_release_reuses_a_complete_bundle(tmp_path):
    d = tmp_path / f"bioconice-release-{TODAY}"
    d.mkdir()
    (d / "manifest.json").write_text("{}")
    rc, calls = _run(tmp_path)
    assert rc == 0
    assert calls[0] == f"gate --release {d}"


def test_release_is_a_no_op_once_latest_is_today(tmp_path):
    assert _run(tmp_path, latest=TODAY) == (0, [])
