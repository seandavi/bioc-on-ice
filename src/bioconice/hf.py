"""Monthly Hugging Face archive (#214, ADR-0016): one gated release bundle pushed to
hf.co/datasets/seandavis/bioconice as one commit, tagged `YYYY-MM` of its release date.

The mechanics are `cdsci.publish.hf`, shared with cancer-on-ice (cdsci-lake ADR-0027): one
commit of the Parquet, manifest.json and gate.json as they are, plus generated views.sql
and restore.sql over paths relative to the download directory (the bundle's copies read R2
`latest/`) and README.md, the dataset card; bioconice.duckdb (whose views read R2) is not
pushed, and nothing points at `hf://` (anonymous reads there are rate-limited, HTTP 429).
Every Parquet file is re-hashed against its manifest sha256 first. If the tag exists with
this bundle's manifest.json the push does nothing, so a re-fired timer is harmless; with a
different one it fails. Tags are never deleted. Dry run unless `apply`.

This module is the bioc policy: the repo, the tag (`YYYY-MM`), the card, the token from
GSM, and that only a bundle whose gate.json certifies its manifest.json is pushed
(`check_proof`, before anything else). DOIs are not minted.
"""

import json
import os
from datetime import date
from pathlib import Path

import cdsci.publish.hf
from cdsci.publish.gate import GATE_FILE, check_proof

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
license_link: https://huggingface.co/datasets/{repo}/blob/{tag}/manifest.json
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
    gate = check_proof(release)
    release_id = json.loads((release / "manifest.json").read_text())["release"]
    tag = date.fromisoformat(release_id).strftime("%Y-%m")
    cdsci.publish.hf.hf_push(
        release, api, repo=repo, tag=tag, apply=apply, extra_files=(GATE_FILE,),
        card=lambda tag, manifest, tables: build_card(tag, manifest, gate, tables, repo),
        message=f"biocOnIce release {tag} (v{release_id})")
