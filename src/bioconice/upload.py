"""Upload a built release bundle to R2 (issue #192, map #187): `v{release}/`, then `latest/`.

The mechanics are `cdsci.publish.upload`, shared with cancer-on-ice (cdsci-lake ADR-0027):
each folder gets the bundle's Parquet plus its own views.sql, restore.sql and
bioconice.duckdb, regenerated for that folder's public URL, manifest.json last (it marks
the folder complete); every Parquet file is re-hashed against its manifest sha256 first; a
completed dated folder of other files is never overwritten; latest/ loses its manifest.json
before being overwritten; afterwards complete `v*/` folders the retention rule does not
keep are pruned. Dry run unless `apply`: everything is built and listed, nothing is written.

This module is the bioc policy: the bucket and its public URL, the retention rule (the
newest `keep` dated releases, this one counted, 4 by default: ADR-0016), `prune` on its own
around the release `latest/` serves, and the R2 credentials, from GSM as cdsci-lake resolves
them. gate.json is not uploaded: scripts/release.sh runs the gate before the upload.
"""

from pathlib import Path

import cdsci.publish.upload
from cdsci.publish.upload import keep_newest

from .publish import DB_FILE

BUCKET = "data-bioconice"
PUBLIC_URL = "https://data.bioconice.cancerdatasci.org"


def r2_filesystem():
    """pyarrow's S3 client on the cdsci R2 account, credentials as cdsci-lake resolves them.

    Each of CU_OPENALEX_R2_ACCOUNT_ID / _R2_ACCESS_KEY_ID / _R2_SECRET_ACCESS_KEY (env or
    .env) if set, else its cdsci-r2-* secret in GSM project cdsci-infra.
    """
    from cdsci.lake.config import get_settings
    from cdsci.lake.secrets import get_secret

    s = get_settings()
    return cdsci.publish.upload.r2_filesystem(
        s.r2_account_id or get_secret(s.r2_account_id_secret, s.gsm_project),
        s.r2_access_key_id or get_secret(s.r2_access_key_secret, s.gsm_project),
        s.r2_secret_access_key or get_secret(s.r2_secret_key_secret, s.gsm_project))


def _retention(keep):
    try:
        return keep_newest(keep)
    except ValueError:
        raise SystemExit(f"--keep must be an integer >= 1, got {keep!r}") from None


def prune(fs, root=BUCKET, apply=False, keep=4):
    """Only the pruning, around the release `latest/` serves; no bundle needed."""
    cdsci.publish.upload.prune(fs, root, retention=_retention(keep), apply=apply)


def upload(bundle: Path, fs, root=BUCKET, apply=False, keep=4):
    """Upload `bundle` (a `bioconice publish` directory) to `v{release}/` and `latest/`, then prune."""
    cdsci.publish.upload.upload(bundle, fs, root, public_url=PUBLIC_URL, db_file=DB_FILE,
                                retention=_retention(keep), apply=apply)
