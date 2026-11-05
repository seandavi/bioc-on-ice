"""`bioconice upload`: a bundle into a local directory standing in for the R2 bucket, offline.

The upload mechanics are cdsci.publish.upload (cdsci-lake ADR-0027, tested there); these
tests cover what bioc passes: the public URL, the DB file, keep-the-newest-4 (ADR-0016),
prune-only around latest/, and `--keep`."""

import hashlib
import json

import duckdb
import pytest
from fsspec.implementations.local import LocalFileSystem
from pyarrow import fs as pafs

from bioconice import publish, upload

RELEASE = "2026-10-30"
# Like S3, writes a key without its parent "directory" existing first.
BUCKET_FS = pafs.PyFileSystem(pafs.FSSpecHandler(LocalFileSystem(auto_mkdir=True)))


def _bundle(tmp_path, release=RELEASE):
    out = tmp_path / f"release-{release}"
    (out / "annotation").mkdir(parents=True)
    duckdb.sql(f"COPY (SELECT 1 AS gene_id) TO '{out}/annotation/gene.parquet' (FORMAT parquet)")
    sha256 = hashlib.sha256((out / "annotation/gene.parquet").read_bytes()).hexdigest()
    table = {"schema": "annotation", "table": "gene", "path": "annotation/gene.parquet",
             "row_count": 1, "comment": None, "columns": [], "sha256": sha256}
    manifest = {"release": release, "base_url": "https://example.org/x", "tables": [table]}
    (out / "manifest.json").write_text(json.dumps(manifest))
    return out


def _bucket(tmp_path):
    bucket = tmp_path / "bucket"
    # weekly releases plus a one-off (10-29); with v{RELEASE} that is 6 dated folders
    for d in ("v2026-10-04", "v2026-10-11", "v2026-10-18", "v2026-10-25", "v2026-10-29",
              "vnotadate", "latest"):
        (bucket / d).mkdir(parents=True)
        (bucket / d / "manifest.json").write_text("{}")
    return bucket


def test_each_folder_reads_its_own_parquet_and_manifest_goes_last(tmp_path, capsys):
    bucket = _bucket(tmp_path)
    upload.upload(_bundle(tmp_path), BUCKET_FS, str(bucket), apply=True)

    for folder in (f"v{RELEASE}", "latest"):
        url = f"{upload.PUBLIC_URL}/{folder}/annotation/gene.parquet"
        assert f"read_parquet('{url}')" in (bucket / folder / "views.sql").read_text()
        assert f"read_parquet('{url}')" in (bucket / folder / "restore.sql").read_text()
        db = duckdb.connect(str(bucket / folder / publish.DB_FILE), read_only=True)
        assert url in db.sql("SELECT sql FROM duckdb_views() WHERE view_name = 'gene'").fetchone()[0]
        m = json.loads((bucket / folder / "manifest.json").read_text())
        assert m["base_url"] == f"{upload.PUBLIC_URL}/{folder}" and m["release"] == RELEASE
        assert (bucket / folder / "annotation/gene.parquet").exists()

    puts = [line.split()[1] for line in capsys.readouterr().out.splitlines() if line.startswith("put")]
    assert puts[4] == f"{bucket}/v{RELEASE}/manifest.json" and puts[-1] == f"{bucket}/latest/manifest.json"
    assert all("/latest/" not in p for p in puts[:5]) and all("/latest/" in p for p in puts[5:])
    # keep 4 of 6, the one-off counted; latest/ and non-dated names are untouched
    assert sorted(p.name for p in bucket.iterdir()) == [
        "latest", "v2026-10-18", "v2026-10-25", "v2026-10-29", f"v{RELEASE}", "vnotadate"]


def test_dry_run_writes_nothing(tmp_path, capsys):
    bucket = _bucket(tmp_path)
    before = sorted(p.relative_to(bucket) for p in bucket.rglob("*"))
    upload.upload(_bundle(tmp_path), BUCKET_FS, str(bucket))

    assert sorted(p.relative_to(bucket) for p in bucket.rglob("*")) == before
    out = capsys.readouterr().out
    assert [ln for ln in out.splitlines() if ln.startswith("delete")] == [
        f"delete {bucket}/latest/manifest.json", f"delete {bucket}/v2026-10-04/",
        f"delete {bucket}/v2026-10-11/"]
    assert out.count("put ") == 10







@pytest.mark.parametrize("keep", [0, -1, 2.0])
def test_keep_below_one_is_refused_before_touching_the_bucket(tmp_path, capsys, keep):
    bucket = _bucket(tmp_path)
    before = sorted(p.relative_to(bucket) for p in bucket.rglob("*"))
    with pytest.raises(SystemExit, match="--keep must be an integer >= 1"):
        upload.upload(_bundle(tmp_path), BUCKET_FS, str(bucket), apply=True, keep=keep)
    assert sorted(p.relative_to(bucket) for p in bucket.rglob("*")) == before
    assert capsys.readouterr().out == ""




def _live(bucket, release=RELEASE):
    (bucket / f"v{release}").mkdir(exist_ok=True)
    (bucket / "latest" / "manifest.json").write_text(json.dumps({"release": release}))


def test_prune_only_dry_run_lists_and_deletes_nothing(tmp_path, capsys):
    bucket = _bucket(tmp_path)
    _live(bucket)
    before = sorted(p.relative_to(bucket) for p in bucket.rglob("*"))
    upload.prune(BUCKET_FS, str(bucket))
    assert sorted(p.relative_to(bucket) for p in bucket.rglob("*")) == before
    out = capsys.readouterr().out
    assert "put " not in out
    assert [ln for ln in out.splitlines() if ln.startswith("delete")] == [
        f"delete {bucket}/v2026-10-04/", f"delete {bucket}/v2026-10-11/"]


def test_prune_only_apply_keeps_the_newest_four_and_latest(tmp_path):
    bucket = _bucket(tmp_path)
    _live(bucket)
    upload.prune(BUCKET_FS, str(bucket), apply=True)
    assert sorted(p.name for p in bucket.iterdir()) == [
        "latest", "v2026-10-18", "v2026-10-25", "v2026-10-29", f"v{RELEASE}", "vnotadate"]


def test_prune_only_keeps_the_release_latest_serves_even_if_older(tmp_path):
    bucket = _bucket(tmp_path)
    _live(bucket, "2026-10-04")
    upload.prune(BUCKET_FS, str(bucket), apply=True, keep=1)
    assert sorted(p.name for p in bucket.iterdir()) == [
        "latest", "v2026-10-04", "v2026-10-29", "vnotadate"]



@pytest.mark.parametrize("keep", [0, -1, 2.0])
def test_prune_only_refuses_keep_below_one(tmp_path, keep):
    bucket = _bucket(tmp_path)
    _live(bucket)
    with pytest.raises(SystemExit, match="--keep must be an integer >= 1"):
        upload.prune(BUCKET_FS, str(bucket), apply=True, keep=keep)
    assert (bucket / "v2026-10-04").exists()
