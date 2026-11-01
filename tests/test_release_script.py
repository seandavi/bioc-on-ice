"""scripts/release.sh, the weekly release chain (#213), with `bioconice` and `curl` stubbed."""

import fcntl
import os
import subprocess
from datetime import UTC, datetime
from pathlib import Path

import pytest

SCRIPT = Path(__file__).parents[1] / "scripts" / "release.sh"
TODAY = datetime.now(UTC).date().isoformat()


def _env(tmp_path, latest="2026-10-25", fail=None):
    """PATH with stubs: `curl` serves `latest`, `bioconice` logs its arguments and exits 1 on `fail`."""
    bin_ = tmp_path / "bin"
    bin_.mkdir()
    (bin_ / "curl").write_text(f"#!/bin/sh\necho '{{\"release\": \"{latest}\"}}'\n")
    (bin_ / "bioconice").write_text(
        f'#!/bin/sh\necho "$*" >> {tmp_path}/calls\n[ "$1" != "{fail}" ]\n')
    for f in bin_.iterdir():
        f.chmod(0o755)
    return {**os.environ, "PATH": f"{bin_}:{os.environ['PATH']}", "TMPDIR": str(tmp_path),
            "BIOCONICE_RELEASE_LINK": str(tmp_path / "bioconice-release")}


def _calls(tmp_path):
    calls = tmp_path / "calls"
    return calls.read_text().splitlines() if calls.exists() else []


def _run(tmp_path, latest="2026-10-25", fail=None):
    r = subprocess.run([str(SCRIPT)], env=_env(tmp_path, latest, fail), capture_output=True, text=True)
    return r.returncode, _calls(tmp_path)


def test_release_runs_publish_gate_dry_run_then_apply(tmp_path):
    rc, calls = _run(tmp_path)
    d = tmp_path / f"bioconice-release-{TODAY}"
    assert rc == 0
    assert calls == [f"publish --out {d}", f"gate --release {d}",
                     f"upload --release {d}", f"upload --release {d} --apply"]
    assert f"done; {tmp_path}/bioconice-release -> {d}" in (
        tmp_path / f"bioconice-release-{TODAY}.log").read_text()
    assert (tmp_path / "bioconice-release").resolve() == d.resolve()


@pytest.mark.parametrize("fail", ["publish", "gate"])
def test_release_never_uploads_after_a_failed_step(tmp_path, fail):
    rc, calls = _run(tmp_path, fail=fail)
    assert rc != 0
    assert calls[-1].startswith(fail)
    assert not (tmp_path / "bioconice-release").exists()  # hf-push never sees an ungated bundle
    assert not any(c.startswith("upload") for c in calls)


def _bundle(tmp_path, manifest='{"tables": []}'):
    d = tmp_path / f"bioconice-release-{TODAY}"
    d.mkdir()
    (d / "manifest.json").write_text(manifest)
    return d


def test_release_reuses_a_complete_bundle(tmp_path):
    d = _bundle(tmp_path)
    rc, calls = _run(tmp_path)
    assert rc == 0
    assert calls[0] == f"gate --release {d}"


def test_release_rebuilds_a_bundle_with_a_truncated_manifest(tmp_path):
    d = _bundle(tmp_path, '{"release": "20')
    rc, calls = _run(tmp_path)
    assert rc == 0
    assert calls[0] == f"publish --out {d}"


def test_release_only_prunes_once_latest_is_today(tmp_path):
    _bundle(tmp_path)
    assert _run(tmp_path, latest=TODAY) == (0, ["upload --prune-only --apply"])


def test_release_prunes_once_latest_is_today_without_a_bundle(tmp_path):
    assert _run(tmp_path, latest=TODAY) == (0, ["upload --prune-only --apply"])


def test_a_second_release_run_waits_for_the_lock_then_only_prunes(tmp_path):
    with open(tmp_path / "bioconice-release.lock", "w") as lock:
        fcntl.flock(lock, fcntl.LOCK_EX)
        second = subprocess.Popen([str(SCRIPT)], env=_env(tmp_path, latest=TODAY),
                                  stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        with pytest.raises(subprocess.TimeoutExpired):
            second.wait(timeout=1)
        assert _calls(tmp_path) == []
    assert second.wait(timeout=30) == 0
    assert _calls(tmp_path) == ["upload --prune-only --apply"]
