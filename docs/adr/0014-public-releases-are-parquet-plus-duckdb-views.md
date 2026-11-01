# 0014 — Public releases are Parquet + DuckDB views

**Status**: Accepted — owner decision, 2026-10-30 (map #187, task #188).
**Supersedes**: the serving parts of [ADR-0005](0005-cloudflare-account-is-the-trust-domain.md), [ADR-0009](0009-biocOnIce-moves-accounts-not-the-logs.md) and [ADR-0011](0011-bucket-scoped-vending-tokens.md), once the Iceberg retirement lands. They are marked now; their bodies are unchanged.

## Context

The public surface is an Iceberg warehouse on R2 behind the icegate gateway:
anonymous read, an R2 Data Catalog, and vended credentials. ADR-0005, 0009 and
0011 are the record of making that safe: which account holds it, which tokens
the gateway vends, how a vended credential is kept from reaching anything else.
All of that work exists because a catalog hands out credentials.

ADR-0013 already moved the data to versioned dataset releases, which makes the
catalog's main job, tracking a table that changes under readers, unnecessary.
omicidx reached the same point first and published the format this ADR adopts
([omicidx ADR-0004](https://github.com/seandavi/omicidx/blob/main/docs/adrs/0004-public-serving-parquet-plus-views-sql.md),
re-affirmed 2026-10-21 in omicidx#203).

## Decision

The public surface is **dated, immutable releases of flat Parquet plus
`bioconice.duckdb`, `views.sql`, `restore.sql` and `manifest.json`**, in the R2
bucket `data-bioconice`, served by a Worker on `bioconice.cancerdatasci.org`.

- Layout: `latest/` and `v{YYYY-MM-DD}/`. Dated folders are kept 30 days
  (amended by [ADR-0016](0016-release-cadence-weekly-r2-monthly-hf.md): weekly
  releases, the last 4 kept, monthly archive on Hugging Face). Each
  folder's views point at that folder's own URLs, and each carries its own
  `manifest.json`. A completed dated folder is never overwritten (2026-10-31, #203):
  users pin it and the next release is checked against it, so a second build on
  the same day waits for the next date. Bucket, host
  and retention are confirmed by map #187.
- For the public product this replaces [ADR-0013](0013-versioned-datasets-not-versioned-rows.md)'s
  release layout and verification, namely per-dataset `cdsci.lake.publish`
  output, `latest.json` and the frozen DuckLake, with one whole-product bundle
  in the omicidx layout. ADR-0013's principle, versioned datasets and not
  versioned rows, stands, and so does current rows only.
- Releases carry current rows only (`valid_to IS NULL`). History follows
  ADR-0013. The Type-2 Iceberg tables stay read-only behind the existing
  catalog until phase 2 of #187 exports them once to Parquet as an archive
  release; then the R2 Data Catalog, icegate and the PyIceberg writer are
  removed.
- The name stays. "On ice" now means frozen releases.
- Iceberg, icegate and credential vending are retired. This ADR does not retire
  them; that is the later migration step.

## Alternatives considered

**Frozen DuckLake.** A file-catalog DuckLake published beside the Parquet, as
omicidx did first. Dropped there (omicidx ADR-0004, 2026-10-21 note): three months of Worker analytics showed no
real `ATTACH` traffic while the flat Parquet took 99% of requests, and the
catalog's `data/` copy more than doubled each dated folder (108 GB on top of
73 GB). Nothing here suggests bioc-on-ice readers differ, and the Parquet
already serves DuckDB, Polars, Arrow and R without a catalog. Not measured on
bioc-on-ice traffic.

**Keep Iceberg and icegate.** The owner has observed no consumers of the
catalog path; that is not yet measured on bioc-on-ice traffic. Every query pays catalog round trips before it reads a byte. Anonymous
read needs credential vending, which is the whole of ADR-0005, 0009 and 0011:
three ADRs spent containing a mechanism the Parquet layout does not have, since
a public bucket of static files vends nothing.

## Consequences

- The trust-domain, account-move and token-composition reasoning in ADR-0005,
  0009 and 0011 stops applying to serving once Iceberg is gone. The history
  stays; the ADRs are marked, not rewritten.
- icegate is retired from this project's footprint, and with it credential
  vending. ADR-0011's conformance-record idea for the federation registry (#59)
  loses its basis and needs a fresh look.
- Existing clients (README examples, explorer, MCP) must be repointed from the
  catalog to release URLs (#193).
- SPEC.md and AGENTS.md still describe Iceberg and PyIceberg as the lake and its
  only writer. They change in the retirement step, not in this PR, so for now
  they describe the writers that still exist.
- A release is only as good as its parity with the live tables; #190 gates the
  first publication on that. *(Since replaced by the release-over-release gate,
  `bioconice gate`, which checks each release against the previous published one:
  #183 step 5. The order is publish → gate → upload dry run → upload `--apply`;
  README "Release it".)*
