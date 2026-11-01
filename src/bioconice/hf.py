"""Monthly Hugging Face archive (#214, ADR-0016): one gated release bundle pushed to
hf.co/datasets/seandavis/bioconice as one commit, tagged `YYYY-MM` of its release date.

omicidx's flows/hf_release.py without Prefect or the R2 download: the bundle is the local
directory `bioconice publish` wrote and `bioconice gate` passed (its gate.json matches its
manifest.json, and each Parquet file its manifest sha256). It pushes the Parquet,
manifest.json and gate.json as they are. It also
generates three files: views.sql and restore.sql, which read paths relative to the download
directory (the bundle's copies read R2 `latest/`), and README.md, the dataset card. It does
not push bioconice.duckdb, because that file's views read R2 `latest/`. Users download a
tag and query it locally. Nothing points at `hf://`: anonymous reads there are
rate-limited (HTTP 429).
If the tag already exists, the push does nothing, so a re-fired timer is harmless. Tags
are never deleted. DOIs are not minted.
"""

import hashlib
import json
import os
from datetime import date
from pathlib import Path

from huggingface_hub import CommitOperationAdd, CommitOperationDelete
from huggingface_hub.errors import RepositoryNotFoundError

from . import publish
from .gate import GATE_FILE, manifest_sha256
from .upload import PUBLIC_URL

REPO = "seandavis/bioconice"
TOKEN_SECRET = "cdsci-huggingface-token"
CODE_URL = "https://github.com/seandavi/bioc-on-ice"


def hf_token():
    """HF_TOKEN if set, else GSM secret cdsci-huggingface-token in cdsci-infra, as omicidx."""
    from cdsci.lake.config import get_settings
    from cdsci.lake.secrets import get_secret

    return os.environ.get("HF_TOKEN") or get_secret(TOKEN_SECRET, get_settings().gsm_project)


def build_card(tag, manifest, gate, tables, repo=REPO):
    """Dataset card. `gate`: the bundle's gate.json; `tables`: the manifest's tables, each
    with its file's `bytes`."""
    configs = "\n".join(f"- config_name: {t['schema']}_{t['table']}\n  data_files: {t['path']}"
                        for t in tables)
    rows = "\n".join(f"| `{t['schema']}.{t['table']}` | `{t['path']}` | {t['row_count']:,} "
                     f"| {t['bytes'] / 1e9:.2f} |" for t in tables)
    sources = sorted({(p.get("source"), p.get("source_version"))
                      for p in manifest.get("provenance", [])}, key=str)
    source_rows = "\n".join(f"| {s} | {v or ''} |" for s, v in sources)
    gb = sum(t["bytes"] for t in tables) / 1e9
    release, local = manifest["release"], f"bioconice-{tag}"
    return f"""---
license: other
license_name: mixed-upstream-terms
license_link: manifest.json
pretty_name: biocOnIce
tags:
- biology
- genomics
- bioinformatics
- annotation
configs:
{configs}
---

# biocOnIce {tag}

biocOnIce is public biological annotation (genes, transcripts, exons, identifier
mappings, ontologies, interactions, publications and more) as flat Parquet, one file
per table. The code is at {CODE_URL}.

This is the monthly archive release **`{tag}`**. It is a copy of the weekly release
`{release}`, which passed `bioconice gate` (see [`gate.json`](gate.json)). Cite and pin
it by the `{tag}` revision. Tags are never deleted. For the newest release, queried in
place, use `{PUBLIC_URL}/latest/` instead.

## Download, then query

Download the release, then query the local files. Do not query `hf://` paths in place:
anonymous requests are rate-limited, and one query makes many requests, so repeated
queries fail with HTTP 429.

```bash
hf download {repo} --repo-type dataset --revision {tag} --local-dir {local}
```

One table only:

```bash
hf download {repo} --repo-type dataset --revision {tag} --local-dir {local} \\
  --include annotation/gene.parquet
```

DuckDB: `views.sql` creates one view per table over paths relative to the download
directory, so run it from there:

```bash
cd {local}
duckdb bioconice.duckdb < views.sql
duckdb bioconice.duckdb -c "SELECT count(*) FROM annotation.gene"
```

`restore.sql` materialises every table into the database instead and checks the row
counts against `manifest.json`: `duckdb bioconice-local.duckdb < restore.sql`, from the
same directory.

Python (polars):

```python
import polars as pl

genes = pl.scan_parquet("{local}/annotation/gene.parquet").head(10).collect()
```

R (arrow):

```r
genes <- arrow::read_parquet("{local}/annotation/gene.parquet")
```

## Provenance

| | |
|---|---|
| Release | {release} |
| Published at | {manifest.get("published_at", "")} |
| Code (git SHA) | `{manifest.get("git_sha") or ""}` |
| Gate reference | {gate.get("reference") or ""} |
| Gate passed at | {gate.get("passed_at", "")} |

Sources and their upstream versions:

| Source | Version |
|---|---|
{source_rows}

The full record is [`manifest.json`](manifest.json): per table its row count, sha256,
columns and their documentation, and every `provenance.release` row.

## Tables

{len(tables)} files, {gb:.1f} GB in total.

| Table | File | Rows | Size (GB) |
|---|---|---:|---:|
{rows}

## License

The terms are mixed. Each table keeps the terms of its upstream source; the sources and
versions are listed above and in `manifest.json` (`provenance`). Where a source requires
a notice to travel with its data, the table comment carries it (`views.sql`).

## Citation

DOIs are not minted. Cite the tag:

```bibtex
@misc{{bioconice_{tag.replace("-", "_")},
  title  = {{biocOnIce, release {tag}}},
  author = {{Davis, Sean}},
  year   = {{{tag[:4]}}},
  url    = {{https://huggingface.co/datasets/{repo}/tree/{tag}}}
}}
```
"""


def hf_push(release: Path, api, repo=REPO, apply=False):
    """Push the gated bundle `release` to `repo` as one commit tagged YYYY-MM. Dry run unless
    `apply`: the plan is printed, and nothing is created or written on Hugging Face."""
    manifest = json.loads((release / "manifest.json").read_text())
    tag = date.fromisoformat(manifest["release"]).strftime("%Y-%m")
    if apply:
        api.create_repo(repo, repo_type="dataset", private=False, exist_ok=True)
    try:
        tags = {t.name for t in api.list_repo_refs(repo, repo_type="dataset").tags}
        remote = api.list_repo_files(repo, repo_type="dataset")
    except RepositoryNotFoundError:  # a dry run before the first push
        tags, remote = set(), []
    if tag in tags:
        print(f"{repo}@{tag} already exists; nothing to do")
        return
    proof = release / GATE_FILE
    if not proof.exists():
        raise SystemExit(f"{release}: no {GATE_FILE}; only a release that passed "
                         "`bioconice gate` is pushed")
    gate = json.loads(proof.read_text())
    if gate.get("manifest_sha256") != manifest_sha256(release):
        raise SystemExit(f"{release}/{GATE_FILE} is for another manifest.json; "
                         "re-run `bioconice gate` on this bundle")
    # gate.json binds manifest.json, not the Parquet: re-hash every file against its manifest
    # sha256. Streamed. Disk-bound on onclappc02 (sha256 1.6 GB/s, cold reads ~250 MB/s):
    # about 6 minutes for a 94 GB bundle, against an hours-long upload.
    for t in manifest["tables"]:
        with (release / t["path"]).open("rb") as f:
            if hashlib.file_digest(f, "sha256").hexdigest() != t["sha256"]:
                raise SystemExit(f"{release}/{t['path']} does not match its manifest.json "
                                 "sha256; re-publish and re-gate this bundle")

    tables = [{**t, "bytes": (release / t["path"]).stat().st_size} for t in manifest["tables"]]
    generated = {
        "views.sql": publish.views_sql(tables, "."),
        "restore.sql": publish.restore_sql(tables, "."),
        "README.md": build_card(tag, manifest, gate, tables, repo),
    }
    files = [t["path"] for t in tables] + ["manifest.json", GATE_FILE]
    # create_commit, not upload_folder: with hf_xet installed, upload_folder splits a large
    # upload into several commits; a release is exactly one. Remote files not in this
    # release leave main (they stay reachable at older tags).
    ops = [CommitOperationAdd(p, release / p) for p in files]
    ops += [CommitOperationAdd(p, text.encode()) for p, text in generated.items()]
    keep = {*files, *generated}
    deletes = [CommitOperationDelete(p) for p in remote if p not in keep and p != ".gitattributes"]
    for p in files:
        print(f"put    {p}  ({(release / p).stat().st_size:,} bytes)", flush=True)
    for p, text in generated.items():
        print(f"put    {p}  (generated, {len(text.encode()):,} bytes)", flush=True)
    for d in deletes:
        print(f"delete {d.path_in_repo}", flush=True)
    message = f"biocOnIce release {tag} (v{manifest['release']})"
    if not apply:
        print(f"dry run: would push {repo}@{tag}; nothing written; --apply to push")
        return
    commit = api.create_commit(repo, ops + deletes, repo_type="dataset", commit_message=message)
    api.create_tag(repo, repo_type="dataset", tag=tag, revision=commit.oid, tag_message=message)
    print(f"pushed {repo}@{tag} ({commit.oid})")
