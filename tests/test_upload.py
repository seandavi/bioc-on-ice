"""`bioconice upload`: a bundle into a local directory standing in for the R2 bucket, offline."""

import json
from datetime import date

import duckdb
from fsspec.implementations.local import LocalFileSystem
from pyarrow import fs as pafs

from bioconice import publish, upload

RELEASE = "2026-10-30"
# Like S3, writes a key without its parent "directory" existing first.
BUCKET_FS = pafs.PyFileSystem(pafs.FSSpecHandler(LocalFileSystem(auto_mkdir=True)))


def _bundle(tmp_path):
    out = tmp_path / "release"
    (out / "annotation").mkdir(parents=True)
    duckdb.sql(f"COPY (SELECT 1 AS gene_id) TO '{out}/annotation/gene.parquet' (FORMAT parquet)")
    table = {"schema": "annotation", "table": "gene", "path": "annotation/gene.parquet",
             "row_count": 1, "comment": None, "columns": []}
    manifest = {"release": RELEASE, "base_url": "https://example.org/x", "tables": [table]}
    (out / "manifest.json").write_text(json.dumps(manifest))
    return out


def _bucket(tmp_path):
    bucket = tmp_path / "bucket"
    for d in ("v2026-09-01", "v2026-09-30", "v2026-10-29", "vnotadate", "latest"):
        (bucket / d).mkdir(parents=True)
        (bucket / d / "manifest.json").write_text("{}")
    return bucket


def test_each_folder_reads_its_own_parquet_and_manifest_goes_last(tmp_path, capsys):
    bucket = _bucket(tmp_path)
    upload.upload(_bundle(tmp_path), BUCKET_FS, str(bucket), apply=True,
                  today=date(2026, 10, 30))

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
    # 30 days before 2026-10-30 is 2026-09-30: only the folder before that goes
    assert sorted(p.name for p in bucket.iterdir()) == [
        "latest", "v2026-09-30", "v2026-10-29", f"v{RELEASE}", "vnotadate"]


def test_dry_run_writes_nothing(tmp_path, capsys):
    bucket = _bucket(tmp_path)
    before = sorted(p.relative_to(bucket) for p in bucket.rglob("*"))
    upload.upload(_bundle(tmp_path), BUCKET_FS, str(bucket), today=date(2026, 10, 30))

    assert sorted(p.relative_to(bucket) for p in bucket.rglob("*")) == before
    out = capsys.readouterr().out
    assert f"delete {bucket}/v2026-09-01/" in out and "v2026-09-30/" not in out
    assert out.count("put ") == 10
