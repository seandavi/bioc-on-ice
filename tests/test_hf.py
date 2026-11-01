"""`bioconice hf-push`: a gated bundle to a stand-in for the Hugging Face API, offline (#214)."""

import json
import shutil
import sys
from types import SimpleNamespace

import duckdb
import httpx2
import pytest
from huggingface_hub.errors import RepositoryNotFoundError
from test_gate import PROVENANCE, SIGNATURE_TAXON, _bundle

from bioconice import gate, hf, upload
from bioconice.__main__ import main


class FakeHub:
    """The HfApi calls hf_push makes; `files` None means the repo does not exist."""

    def __init__(self, tags=(), files=None, fail_tag=False):
        self.tags, self.files, self.calls, self.fail_tag = list(tags), files, [], fail_tag

    def create_repo(self, repo, **kw):
        self.calls.append(("create_repo", repo, kw))
        self.files = self.files if self.files is not None else [".gitattributes"]

    def _repo(self):
        if self.files is None:
            raise RepositoryNotFoundError("404", response=httpx2.Response(
                404, request=httpx2.Request("GET", "https://huggingface.co")))

    def list_repo_refs(self, repo, **kw):
        self._repo()
        return SimpleNamespace(tags=[SimpleNamespace(name=t) for t in self.tags])

    def list_repo_files(self, repo, **kw):
        self._repo()
        return self.files

    def create_commit(self, repo, ops, **kw):
        self.calls.append(("create_commit", repo, ops, kw))
        return SimpleNamespace(oid="abc123")

    def create_tag(self, repo, **kw):
        self.calls.append(("create_tag", repo, kw))
        if self.fail_tag:
            self.fail_tag = False
            raise OSError("tag failed")
        self.tags.append(kw["tag"])


@pytest.fixture
def gated(tmp_path):
    """A 2026-11-01 bundle that passed the gate against a 2026-10-31 reference."""
    reference = _bundle(tmp_path / "v2026-10-31", "2026-10-31", {
        "annotation.signature_taxon": SIGNATURE_TAXON.format(n=30),
        "provenance.release": PROVENANCE.format(n=1)})
    release = _bundle(tmp_path / "release", "2026-11-01", {
        "annotation.signature_taxon": SIGNATURE_TAXON.format(n=33),
        "provenance.release": PROVENANCE.format(n=1)})
    assert gate.gate(release, str(reference))
    return release


def test_gate_pass_writes_proof_bound_to_the_manifest(gated):
    proof = json.loads((gated / gate.GATE_FILE).read_text())
    assert proof["manifest_sha256"] == gate.manifest_sha256(gated)
    assert proof["release"] == "2026-11-01" and proof["reference_release"] == "2026-10-31"


def test_failed_gate_removes_a_stale_proof(gated):
    assert not gate.gate(gated, str(gated))  # a reference not older than the bundle
    assert not (gated / gate.GATE_FILE).exists()


def test_dry_run_writes_nothing_even_before_the_repo_exists(gated, capsys):
    hub = FakeHub()
    hf.hf_push(gated, hub)
    assert hub.calls == []
    out = capsys.readouterr().out
    assert "put    annotation/signature_taxon.parquet" in out and "put    gate.json" in out
    assert out.endswith("dry run: would push seandavis/bioconice@2026-11; nothing written; "
                        "--apply to push\n")


def test_apply_is_one_commit_tagged_yyyy_mm_with_local_views(gated, tmp_path, monkeypatch):
    hub = FakeHub(tags=["2026-10"], files=[".gitattributes", "README.md", "old/dropped.parquet"])
    hf.hf_push(gated, hub, apply=True)
    assert [c[0] for c in hub.calls] == ["create_repo", "create_commit", "create_tag"]
    _, _, ops, _ = hub.calls[1]
    added = {o.path_in_repo: o.path_or_fileobj for o in ops if hasattr(o, "path_or_fileobj")}
    assert set(added) == {"annotation/signature_taxon.parquet", "provenance/release.parquet",
                          "manifest.json", "gate.json", "views.sql", "restore.sql", "README.md"}
    assert [o.path_in_repo for o in ops if o.path_in_repo not in added] == ["old/dropped.parquet"]
    assert hub.calls[2][2]["tag"] == "2026-11" and hub.calls[2][2]["revision"] == "abc123"
    card = added["README.md"].decode()
    assert "hf download seandavis/bioconice --repo-type dataset --revision 2026-11" in card
    assert "hf://" not in card.replace("Do not query `hf://` paths in place", "")

    # The archived SQL reads the downloaded files, relative to the download directory.
    download = tmp_path / "download"
    shutil.copytree(gated, download)
    monkeypatch.chdir(download)
    con = duckdb.connect()
    con.execute(added["views.sql"].decode())
    assert con.execute("SELECT count(*) FROM annotation.signature_taxon").fetchone()[0] == 33
    duckdb.connect().execute(added["restore.sql"].decode())  # errors if a row count differs from manifest


def test_an_existing_tag_is_left_alone_gated_or_not(gated, capsys):
    (gated / gate.GATE_FILE).unlink()
    hub = FakeHub(tags=["2026-11"], files=[".gitattributes"])
    hf.hf_push(gated, hub, apply=True)
    assert [c[0] for c in hub.calls] == ["create_repo"]
    assert capsys.readouterr().out == "seandavis/bioconice@2026-11 already exists; nothing to do\n"


def test_an_ungated_bundle_is_refused(gated):
    (gated / gate.GATE_FILE).unlink()
    with pytest.raises(SystemExit, match="only a release that passed `bioconice gate`"):
        hf.hf_push(gated, FakeHub())


def test_a_proof_for_another_manifest_is_refused(gated):
    m = json.loads((gated / "manifest.json").read_text())
    (gated / "manifest.json").write_text(json.dumps({**m, "git_sha": "edited"}))
    hub = FakeHub(files=[".gitattributes"])
    with pytest.raises(SystemExit, match="is for another manifest.json"):
        hf.hf_push(gated, hub, apply=True)
    assert [c[0] for c in hub.calls] == ["create_repo"]


def test_parquet_changed_after_the_gate_is_refused(gated):
    (gated / "annotation/signature_taxon.parquet").write_bytes(b"not the gated file")
    hub = FakeHub(files=[".gitattributes"])
    with pytest.raises(SystemExit, match="signature_taxon.parquet does not match its manifest"):
        hf.hf_push(gated, hub, apply=True)
    assert [c[0] for c in hub.calls] == ["create_repo"]


def test_a_failed_tag_is_retried_by_the_next_run(gated):
    hub = FakeHub(files=[".gitattributes"], fail_tag=True)
    with pytest.raises(OSError, match="tag failed"):
        hf.hf_push(gated, hub, apply=True)
    hf.hf_push(gated, hub, apply=True)  # no tag yet: commit again (a no-op on the Hub), then tag
    assert [c[0] for c in hub.calls] == ["create_repo", "create_commit", "create_tag"] * 2
    assert hub.tags == ["2026-11"]
    hf.hf_push(gated, hub, apply=True)  # tagged: nothing more
    assert [c[0] for c in hub.calls][6:] == ["create_repo"]


def test_upload_ignores_the_gate_proof(gated, tmp_path, capsys):
    (tmp_path / "bucket").mkdir()
    upload.upload(gated, upload.pafs.LocalFileSystem(), str(tmp_path / "bucket"))
    assert "gate.json" not in capsys.readouterr().out


def test_cli_dry_run(gated, monkeypatch, capsys):
    hub = FakeHub()
    monkeypatch.setattr(hf, "hf_token", lambda: "token")
    monkeypatch.setattr("huggingface_hub.HfApi", lambda token: hub)
    monkeypatch.setattr(sys, "argv", ["bioconice", "hf-push", "--release", str(gated)])
    main()
    assert hub.calls == [] and "dry run" in capsys.readouterr().out
