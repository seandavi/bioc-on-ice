# 0016 — Release cadence: weekly on R2, monthly on Hugging Face

**Status**: Accepted — owner decision, 2026-10-31.
**Amends**: [ADR-0014](0014-public-releases-are-parquet-plus-duckdb-views.md)'s
retention line (dated folders kept 30 days), and SPEC.md's "pinned releases are
never pruned".

## Context

ADR-0014 put the public product on R2 as `latest/` plus dated `v{YYYY-MM-DD}/`
folders, kept 30 days, and left the cadence open. Nothing publishes on a
schedule yet: `bioconice publish` and `bioconice upload` are run by hand. The
release gate (`bioconice gate`, #210) is not merged.

A bundle is 57 GB (80 tables, `latest/manifest.json` of release 2026-10-31), and
about 94 GB once `annotation.pubmed__article` (37 GB) is in it (#194).
Consecutive releases share most of their bytes whenever their sources have not
updated in between. R2 does not deduplicate, so each dated folder is a full
copy.

SPEC.md also promises that pinned releases are never pruned. That came from
`cdsci.lake.publish`'s per-dataset releases (`pin_release` in
`cdsci/lake/publish/index.py`). The whole-product bundle that ADR-0014 adopted
has no pins: `upload` prunes by age alone. A bucket that keeps every pin would
grow without bound.

Users who need a release for years, such as a paper or a Bioconductor release,
need a permanent copy somewhere other than the query-in-place bucket.

omicidx already solved the same problem: it publishes to R2 for querying and
copies one release a month to Hugging Face as the permanent, citable copy
([omicidx ADR-0007](https://github.com/seandavi/omicidx/blob/main/docs/adrs/0007-retention-and-deletion.md)).

## Decision

**R2 is for querying in place. It gets a weekly release, and it keeps the
last 4 dated releases.**

- The scheduled release runs Sundays at 00:00 UTC: publish → gate → upload
  (dry run) → upload `--apply`. A release that fails the gate is not uploaded.
- `upload` keeps the 4 newest `v{YYYY-MM-DD}/` folders and deletes the rest.
  Retention is by count, not by age. One-off releases count toward the 4.
  `latest/` is not counted.

**Hugging Face is for archiving and citation. It gets a monthly release.**

- Each month, the first weekly release that passed the gate is copied to a new
  Hugging Face dataset repo, as one commit tagged `YYYY-MM`, with a dataset
  card.
- Users download a monthly release and query it locally. Docs never tell them
  to query `hf://` paths in place, because anonymous requests are rate-limited
  and repeated queries fail with HTTP 429. omicidx's docs give the limit as
  3000 requests per 5 minutes per IP, and say one query can make hundreds
  (`docs/guides/versions-and-citation.qmd`).
- Tags are never deleted. They replace SPEC's "pinned releases are never
  pruned". A Bioconductor release cites the nearest `YYYY-MM` tag.
- The first Hugging Face release is made only after a release has passed
  `bioconice gate` (#210).

## Alternatives considered

**Daily releases on R2.** Daily releases with the current 30-day window keep
31 dated folders plus `latest/`, about 3 TB at 94 GB per bundle. Most
of those uploads re-send the same bytes as the day before. Weekly with
keep 4 holds five copies, about 470 GB. The cost is that a source update can
take up to a week to reach `latest/`; the owner judged that acceptable.

**Quarterly releases on Hugging Face.** Fewer archive copies, but a user could
wait up to three months for a citable version. A Bioconductor release could
then cite a tag that is months older than the data it shipped with. Monthly
releases bound that gap to one month.

**Querying Hugging Face in place.** This would remove R2 and the Worker.
Rejected because of the 429s: anonymous rate limits break the
"no account, no key" query path that R2 serves today. Hugging Face holds the
archive; R2 serves queries.

**Keeping every pinned release on R2.** This is what SPEC said. It is
unbounded storage on the serving bucket, and nothing decided what counts as
pinned for the whole-product bundle. Monthly Hugging Face tags give the same permanence outside the
bucket.

## What is copied from omicidx

All paths are in [seandavi/omicidx](https://github.com/seandavi/omicidx) at
`30f884b`.

- **Scheduler**: systemd `--user` timers on onclappc02, one `.service` and
  `.timer` per job, `Type=oneshot`, `TimeoutStartSec=`, `Persistent=true`,
  `OnFailure=ntfy-notify@%N.service`, and `EnvironmentFile=` pointing at the
  repo `.env`. No Prefect: omicidx removed its Prefect server and worker in
  omicidx#158 and #160 (`systemd/README.md`). The convention is
  `monode/infrastructure/SCHEDULING.md`.
- **Monthly Hugging Face push**:
  `packages/omicidx-prefect/src/omicidx/prefect/flows/hf_release.py`, run by
  `systemd/omicidx-hf-release.{service,timer}`. It copies the newest complete
  dated folder (one with `manifest.json`) verbatim and pushes it as one
  `create_commit`, not `upload_folder`, which splits large uploads into
  several commits when Xet is installed. Files not in the release are deleted
  from `main`, so they stay reachable only at older tags. The tag is `YYYY-MM`
  of the publish date. If the tag already exists, the run does nothing, so a
  timer that fires twice is safe.
- **Xet**: the upload relies on `hf_xet` (pulled in by `huggingface-hub`;
  `hf-xet` 1.7.0 in omicidx's `uv.lock`). A failed run is re-run as-is,
  because the download keeps finished files and the upload is deduplicated
  (`hf_release.py` docstring; `omicidx-hf-release.service`).
- **Dataset card**: `build_card` in `hf_release.py` generates the card from
  `manifest.json` and the files: YAML `configs` per table, provenance, a table
  of rows and sizes, query examples, licence and a BibTeX entry.
- **Auth**: `HF_TOKEN` in the repo `.env`, which records the GSM secret
  `cdsci-huggingface-token` (project `cdsci-infra`) as its source. The token
  is read through pydantic settings (`hf_token` in
  `packages/omicidx-prefect/src/omicidx/prefect/config.py`).
- **DOIs** are minted by hand per tag in the Hugging Face UI, because minting
  one locks the repo (`hf_release.py` docstring).

## What differs from omicidx

- **Cadence and retention on R2.** omicidx publishes daily
  (`systemd/omicidx-downstream.timer`, 05:00 UTC) and prunes dated folders by
  age, 30 days (`PUBLIC_BUNDLE_RETENTION_DAYS`, `prune_dated_folders` in
  `flows/parquet_export.py`). bioc-on-ice publishes weekly and keeps 4 by
  count.
- **The gate.** omicidx copies the newest complete folder to Hugging Face.
  bioc-on-ice copies only a release that passed `bioconice gate`.
- **Querying in place.** omicidx's card and `views.sql` point at
  `hf://datasets/…@tag/` and offer in-place queries "for a quick look". The
  bioc-on-ice card documents download and local query only.
- **Timer date.** omicidx fires on the 2nd of each month at 12:00 UTC. The
  bioc-on-ice push follows the first Sunday release of the month instead.

## Consequences

- `upload --retention-days 30` becomes count-based retention, `keep = 4`.
- The weekly schedule cannot be enabled until #210 is merged. Until then,
  releases stay manual.
- A pinned `v{date}/` URL lasts about four weeks. Docs that offer pinning
  must say so and point to the monthly Hugging Face tag for anything longer.
- A one-off release pushes the oldest weekly release out early.
- Reproducing a release older than four weeks means downloading its monthly
  tag. Weekly releases between two monthly tags are not archived.
- Settled by the owner with #214: the repo is `seandavis/bioconice`. The archive
  ships the Parquet, `manifest.json`, `gate.json`, and a `views.sql` and
  `restore.sql` over paths relative to the download directory, but not
  `bioconice.duckdb`, whose views read R2 `latest/`. The card's licence is
  `other` (mixed upstream terms, see `manifest.json` provenance). DOIs are not
  minted for now.
- The push tells a gated release from an ungated one by proof the gate leaves
  in the bundle. A pass writes `gate.json` with the sha256 of the bundle's
  `manifest.json`, and `bioconice hf-push` refuses a bundle without a matching
  one. Re-running the gate at push time was rejected: after the upload,
  `latest/` is the bundle itself, and the gate refuses a reference that is not
  older.
