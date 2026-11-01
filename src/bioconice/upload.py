"""Upload a built release bundle to R2 (issue #192, map #187): `v{release}/`, then `latest/`.

omicidx's publish_bundle without Prefect. Each folder gets the bundle's Parquet plus its
own views.sql, restore.sql and bioconice.duckdb, regenerated for that folder's public URL,
so a pinned `v{release}/` keeps reading its own Parquet and `latest/` reads `latest/`.
manifest.json goes last in each folder: it marks the folder complete. Then `v*/` folders
older than the retention window are pruned. Dry-run unless `apply`: everything is built
and listed, nothing is written to the bucket.
"""

import json
import re
import tempfile
import time
from datetime import UTC, datetime, timedelta
from pathlib import Path

from pyarrow import fs as pafs

from . import publish

BUCKET = "data-bioconice"
PUBLIC_URL = "https://data.bioconice.cancerdatasci.org"
DATED = re.compile(r"v(\d{4}-\d{2}-\d{2})")


def r2_filesystem():
    """pyarrow's S3 client on the cdsci R2 account, credentials as cdsci-lake resolves them.

    Each of CU_OPENALEX_R2_ACCOUNT_ID / _R2_ACCESS_KEY_ID / _R2_SECRET_ACCESS_KEY (env or
    .env) if set, else its cdsci-r2-* secret in GSM project cdsci-infra.
    """
    from cdsci.lake.config import get_settings
    from cdsci.lake.secrets import get_secret

    s = get_settings()
    account = s.r2_account_id or get_secret(s.r2_account_id_secret, s.gsm_project)
    return pafs.S3FileSystem(
        access_key=s.r2_access_key_id or get_secret(s.r2_access_key_secret, s.gsm_project),
        secret_key=s.r2_secret_access_key or get_secret(s.r2_secret_key_secret, s.gsm_project),
        endpoint_override=f"{account}.r2.cloudflarestorage.com", region="auto")


def _put(fs, local, key, apply, attempts=3):
    """One file to its exact key; retried, since that is idempotent.

    R2 rejects a multipart upload whose non-trailing parts differ in length (omicidx's
    buffered `.open("wb")` hit it). pyarrow 25 sends uniform 10 MiB parts: checked against
    a request-logging stand-in, a 3.7 GB file went as 352 x 10,485,760 B plus one shorter
    trailing part, and S3's 10,000-part cap puts the ceiling near 100 GiB per file.
    """
    print(f"put    {key}  ({Path(local).stat().st_size:,} bytes)", flush=True)
    if not apply:
        return
    for attempt in range(1, attempts + 1):
        try:
            pafs.copy_files(str(local), key, source_filesystem=pafs.LocalFileSystem(),
                            destination_filesystem=fs)
            return
        except OSError as e:
            if attempt == attempts or isinstance(e, FileNotFoundError):
                raise
            print(f"  retry {attempt}/{attempts - 1} after: {e}", flush=True)
            time.sleep(10 * attempt)


def _folder(fs, root, bundle, manifest, folder, apply):
    """Upload the bundle into `root/folder/`, with its SQL, DuckDB and manifest for that URL."""
    base = f"{PUBLIC_URL}/{folder}"
    # ponytail: latest/ gets a second upload from the local bundle, not a server-side copy
    # of v{release}/: R2 times out / 502s copying multi-GB objects (omicidx parquet_export).
    for t in manifest["tables"]:
        _put(fs, bundle / t["path"], f"{root}/{folder}/{t['path']}", apply)
    with tempfile.TemporaryDirectory() as tmp:
        tmp = Path(tmp)
        views = publish.views_sql(manifest["tables"], base)
        (tmp / "views.sql").write_text(views)
        (tmp / "restore.sql").write_text(publish.restore_sql(manifest["tables"], base))
        (tmp / "manifest.json").write_text(json.dumps({**manifest, "base_url": base}, indent=2) + "\n")
        publish._build_db(tmp / publish.DB_FILE, views, base, bundle)
        for name in ("views.sql", "restore.sql", publish.DB_FILE, "manifest.json"):
            _put(fs, tmp / name, f"{root}/{folder}/{name}", apply)


def _expired(fs, root, keep, retention_days, today):
    """`v{date}` folders under `root` older than the window; `keep` never is."""
    cutoff = today - timedelta(days=retention_days)
    names = [i.base_name for i in fs.get_file_info(pafs.FileSelector(root))
             if i.type == pafs.FileType.Directory]
    return sorted(n for n in names if (m := DATED.fullmatch(n)) and n != keep
                  and datetime.strptime(m[1], "%Y-%m-%d").date() < cutoff)


def _files(manifest):
    return sorted((t["path"], t.get("sha256")) for t in manifest["tables"])


def upload(bundle: Path, fs, root=BUCKET, apply=False, retention_days=30, today=None):
    """Upload `bundle` (a `bioconice publish` directory) to `v{release}/` and `latest/`, then prune."""
    manifest = json.loads((bundle / "manifest.json").read_text())
    dated = f"v{manifest['release']}"
    # A completed dated release (manifest.json present) is immutable: users pin it and the next
    # release is checked against it. A folder without a manifest is an interrupted upload: retry it.
    # A folder already complete with this bundle is skipped, so a re-run finishes what an
    # interrupted one left (latest/, or only the pruning) without sending the bundle again.
    folders = []
    for folder in (dated, "latest"):
        done = f"{root}/{folder}/manifest.json"
        if fs.get_file_info(done).type == pafs.FileType.NotFound:
            folders.append(folder)
            continue
        with fs.open_input_stream(done) as f:
            remote = json.loads(f.read())
        if remote.get("release") == manifest["release"] and _files(remote) == _files(manifest):
            print(f"{root}/{folder}/ already complete with this bundle", flush=True)
        elif folder == dated:
            raise SystemExit(f"{root}/{dated}/ is a completed release of other files; "
                             "dated releases are never overwritten")
        else:
            folders.append(folder)
    for folder in folders:
        _folder(fs, root, bundle, manifest, folder, apply)
    for name in _expired(fs, root, dated, retention_days, today or datetime.now(UTC).date()):
        print(f"delete {root}/{name}/", flush=True)
        if apply:
            fs.delete_dir(f"{root}/{name}")
    if not apply:
        print("dry run: nothing written; --apply to upload")
