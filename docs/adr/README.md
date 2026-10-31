# Architecture decisions

One file per decision, `NNNN-kebab-title.md`, numbered in the order taken and
never renumbered. A superseded ADR is not deleted or edited into agreement with
the present — it gets a `Superseded by` line and stays, because the reasoning
that turned out wrong is the useful part.

## What belongs here, and what does not

These three carry different things, and duplicating between them is how they rot:

| | holds |
| --- | --- |
| **SPEC.md** | what the system MUST do — normative behaviour, current and complete |
| **ADRs** | why a decision went the way it did, and what was rejected |
| **[wayfinder issues](https://github.com/seandavi/bioc-on-ice/issues/1)** | how the work is being found and sequenced |

An ADR earns its place when the obvious alternative was rejected for a reason
that is not obvious from the result. If the decision is self-evident from
reading SPEC.md, it does not need one. The test: would somebody six months from
now, or an architecture review, propose the rejected option again? If yes,
write the ADR.

ADRs are read by `/improve-codebase-architecture` precisely so that settled
decisions are not re-suggested. Contradicting one is allowed — reopening it
deliberately is the point — but it should be argued, not drifted into.

## Status values

`Accepted` · `Superseded by NNNN` · `Reopened` (argued against and being
revisited; say by whom and where).

## Index

| ADR | decision | status |
| --- | --- | --- |
| [0001](0001-point-in-time-lives-in-the-rows.md) | Point-in-time lives in the rows, not in Iceberg snapshots | Accepted; superseded by 0013 for sources moved to dataset releases |
| [0002](0002-land-raw-then-derive.md) | Land sources verbatim, derive downstream | Accepted |
| [0003](0003-declared-schemas.md) | Declare Iceberg schemas; never infer from Arrow | Accepted |
| [0004](0004-merge-recomputes-scope.md) | Merge recomputes the scope rather than using PyIceberg upsert | Accepted |
| [0005](0005-cloudflare-account-is-the-trust-domain.md) | The Cloudflare account is the trust domain | Accepted (direction superseded by 0009; Note's claim retired by 0011); serving superseded by 0014 |
| [0006](0006-full-type-2-history.md) | Attribute changes close a row and open a new one | Accepted; superseded by 0013 for sources moved to dataset releases |
| [0007](0007-release-manifest.md) | A release manifest, with the provenance of the version string | Accepted |
| [0008](0008-ncbi-gene-is-keyed-by-entrez-id.md) | NCBI gene attributes live in their own Entrez-keyed table | Accepted |
| [0009](0009-biocOnIce-moves-accounts-not-the-logs.md) | biocOnIce moves accounts, not the logs | Superseded by 0010; serving superseded by 0014 |
| [0010](0010-logs-leave-cloudflare.md) | The logs leave Cloudflare: GCS destination, BigQuery analytics | Accepted |
| [0011](0011-bucket-scoped-vending-tokens.md) | Anonymous read is contained by token composition, not account hygiene | Accepted; serving superseded by 0014 |
| [0012](0012-cdsci-lake-is-the-source-not-raw-upstream.md) | cdsci-lake is the source, not raw upstream | Accepted (amended by 0013, 0015) |
| [0013](0013-versioned-datasets-not-versioned-rows.md) | Versioned datasets, not versioned rows | Accepted; release layout superseded by 0014 |
| [0014](0014-public-releases-are-parquet-plus-duckdb-views.md) | Public releases are Parquet + DuckDB views | Accepted |
| [0015](0015-sources-write-to-cdsci-lake-products-derive-at-release.md) | Sources write to cdsci-lake; the product derives at release time | Accepted |
