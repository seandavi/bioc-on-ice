"""`bioconice upload`: a bundle into a local directory standing in for the R2 bucket, offline."""

import json
import duckdb
import pytest
from fsspec.implementations.local import LocalFileSystem
from pyarrow import fs as pafs

from bioconice import publish, upload

RELEASE = "2026-10-30"
# Like S3, writes a key without its parent "directory" existing first.
BUCKET_FS = pafs.PyFileSystem(pafs.FSSpecHandler(LocalFileSystem(auto_mkdir=True)))


def _bundle(tmp_path, release=RELEASE, gene_id=1):
    out = tmp_path / f"release-{release}"
    (out / "annotation").mkdir(parents=True)
    duckdb.sql(f"COPY (SELECT {gene_id} AS gene_id) TO '{out}/annotation/gene.parquet' (FORMAT parquet)")
    table = {"schema": "annotation", "table": "gene", "path": "annotation/gene.parquet",
             "row_count": 1, "comment": None, "columns": [], "sha256": str(gene_id)}
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
        f"delete {bucket}/v2026-10-04/", f"delete {bucket}/v2026-10-11/"]
    assert out.count("put ") == 10


def test_a_completed_dated_release_of_other_files_is_never_overwritten(tmp_path):
    bucket = _bucket(tmp_path)
    (bucket / f"v{RELEASE}").mkdir()
    other = {"release": RELEASE, "tables": [{"path": "annotation/gene.parquet", "sha256": "other"}]}
    (bucket / f"v{RELEASE}" / "manifest.json").write_text(json.dumps(other))
    with pytest.raises(SystemExit, match="completed release of other files"):
        upload.upload(_bundle(tmp_path), BUCKET_FS, str(bucket), apply=True)
    assert json.loads((bucket / f"v{RELEASE}" / "manifest.json").read_text()) == other
    assert not (bucket / "latest" / "views.sql").exists()


def test_an_interrupted_latest_upload_is_finished(tmp_path, capsys, monkeypatch):
    bucket, bundle = _bucket(tmp_path), _bundle(tmp_path)
    real = upload._folder

    def fail_on_latest(fs, root, bundle, manifest, folder, apply):
        if folder == "latest":
            raise OSError("connection reset")
        real(fs, root, bundle, manifest, folder, apply)
    monkeypatch.setattr(upload, "_folder", fail_on_latest)
    with pytest.raises(OSError):
        upload.upload(bundle, BUCKET_FS, str(bucket), apply=True)
    monkeypatch.setattr(upload, "_folder", real)
    dated = (bucket / f"v{RELEASE}" / "manifest.json").read_text()
    capsys.readouterr()

    upload.upload(bundle, BUCKET_FS, str(bucket), apply=True)
    assert (bucket / f"v{RELEASE}" / "manifest.json").read_text() == dated
    assert json.loads((bucket / "latest" / "manifest.json").read_text())["release"] == RELEASE
    assert f"/v{RELEASE}/" not in "".join(ln for ln in capsys.readouterr().out.splitlines()
                                           if ln.startswith("put"))


def test_the_release_being_uploaded_is_kept_even_if_older(tmp_path):
    bucket = _bucket(tmp_path)
    assert upload._expired(BUCKET_FS, str(bucket), "v2026-10-01", keep=2) == [
        "v2026-10-04", "v2026-10-11", "v2026-10-18"]


def test_an_interrupted_dated_upload_is_retried(tmp_path):
    bucket = _bucket(tmp_path)
    (bucket / f"v{RELEASE}" / "annotation").mkdir(parents=True)
    (bucket / f"v{RELEASE}" / "annotation" / "gene.parquet").write_text("partial")
    upload.upload(_bundle(tmp_path), BUCKET_FS, str(bucket), apply=True)
    assert json.loads((bucket / f"v{RELEASE}" / "manifest.json").read_text())["release"] == RELEASE


def test_folders_that_are_not_real_dates_are_never_pruned_or_counted(tmp_path):
    bucket = _bucket(tmp_path)
    for d in ("v2026-00-00", "v9999-99-99", "v2026-02-30"):
        (bucket / d).mkdir()
    # the same two go as without them: v9999-99-99 holds no retention slot
    assert upload._expired(BUCKET_FS, str(bucket), f"v{RELEASE}", keep=4) == [
        "v2026-10-04", "v2026-10-11"]


@pytest.mark.parametrize("keep", [0, -1, 2.0])
def test_keep_below_one_is_refused_before_touching_the_bucket(tmp_path, capsys, keep):
    bucket = _bucket(tmp_path)
    before = sorted(p.relative_to(bucket) for p in bucket.rglob("*"))
    with pytest.raises(SystemExit, match="--keep must be an integer >= 1"):
        upload.upload(_bundle(tmp_path), BUCKET_FS, str(bucket), apply=True, keep=keep)
    assert sorted(p.relative_to(bucket) for p in bucket.rglob("*")) == before
    assert capsys.readouterr().out == ""


def test_a_partial_listing_prunes_less_never_more(tmp_path):
    bucket = _bucket(tmp_path)
    full = upload._expired(BUCKET_FS, str(bucket), f"v{RELEASE}", keep=4)

    class Partial:  # a truncated listing: the newest folders missing
        def get_file_info(self, selector):
            return [i for i in BUCKET_FS.get_file_info(selector) if i.base_name < "v2026-10-25"]
    assert upload._expired(Partial(), str(bucket), f"v{RELEASE}", keep=4) == []
    assert full == ["v2026-10-04", "v2026-10-11"]


def test_retrying_a_after_an_interrupted_b_repairs_latest(tmp_path, monkeypatch):
    bucket = _bucket(tmp_path)
    a, b = _bundle(tmp_path), _bundle(tmp_path, release="2026-11-06", gene_id=2)
    upload.upload(a, BUCKET_FS, str(bucket), apply=True)
    real = upload._put

    def cut_after_latest_parquet(fs, local, key, apply):
        real(fs, local, key, apply)
        if key.endswith("/latest/annotation/gene.parquet"):
            raise OSError("connection reset")
    monkeypatch.setattr(upload, "_put", cut_after_latest_parquet)
    with pytest.raises(OSError):
        upload.upload(b, BUCKET_FS, str(bucket), apply=True)
    monkeypatch.setattr(upload, "_put", real)
    latest = bucket / "latest"
    # B's bytes under A's manifest: the state a skip on matching manifests would have left
    assert json.loads((latest / "manifest.json").read_text())["release"] == RELEASE
    assert (latest / "annotation/gene.parquet").read_bytes() != (a / "annotation/gene.parquet").read_bytes()

    upload.upload(a, BUCKET_FS, str(bucket), apply=True)
    assert json.loads((latest / "manifest.json").read_text())["release"] == RELEASE
    assert (latest / "annotation/gene.parquet").read_bytes() == (a / "annotation/gene.parquet").read_bytes()


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


@pytest.mark.parametrize("latest", [None, "{}", '{"release": "nope"}'])
def test_prune_only_refuses_without_a_dated_latest(tmp_path, latest):
    bucket = _bucket(tmp_path)
    if latest is None:
        (bucket / "latest" / "manifest.json").unlink()
    else:
        (bucket / "latest" / "manifest.json").write_text(latest)
    before = sorted(p.relative_to(bucket) for p in bucket.rglob("*"))
    with pytest.raises(SystemExit):
        upload.prune(BUCKET_FS, str(bucket), apply=True)
    assert sorted(p.relative_to(bucket) for p in bucket.rglob("*")) == before


@pytest.mark.parametrize("keep", [0, -1, 2.0])
def test_prune_only_refuses_keep_below_one(tmp_path, keep):
    bucket = _bucket(tmp_path)
    _live(bucket)
    with pytest.raises(SystemExit, match="--keep must be an integer >= 1"):
        upload.prune(BUCKET_FS, str(bucket), apply=True, keep=keep)
    assert (bucket / "v2026-10-04").exists()
