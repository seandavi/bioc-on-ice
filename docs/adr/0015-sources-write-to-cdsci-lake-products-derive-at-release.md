# 0015 — Sources write to cdsci-lake; the product derives at release time

**Status**: Accepted — decided on the merits for step 0 of map #183 (step 3b), 2026-10-31; owner review is the PR. Settles #178, #185 and #162.
**Amends**: [ADR-0012](0012-cdsci-lake-is-the-source-not-raw-upstream.md). It says
where a migrated source writes, which ADR-0012 left open.

## Context

Map #183 moves every source into cdsci-lake and builds the public release from
the lake. Three open questions decide the shape of every migration after
BugSigDB, so they are settled once, here:

- **#178** (convert to shared DuckLake releases) predates ADR-0013 and ADR-0014.
  Its route covers release SCD2, Frozen DuckLake, a shared release builder and
  an optional Iceberg adapter.
- **#185** asks whether product-derived tables (`annotation.*`, `resource.*`,
  `ontology.*`, `clinical.*`) are written into the shared lake or stay with the
  product.
- **#162** asks whether RNAcentral's ENA composite ids belong in
  `annotation.identifier_mapping`.

Since #185 was filed, the release has become SQL. `publish.TABLES` is a list of
(schema, table, SELECT) entries, and `publish()` streams each SELECT to Parquet
through `COPY`, so moving a table to the lake means changing its SELECT
(`annotation.pubmed__article` already reads `lake.omicidx.pubmed_article`).

cdsci-lake's ADRs bear on this:
[ADR-0013](https://github.com/seandavi/cdsci-lake/blob/main/docs/adr/0013-lake-write-path-is-el-only.md)
makes the EL write path upsert-only, with keyed projections of a raw source
only. [ADR-0015](https://github.com/seandavi/cdsci-lake/blob/main/docs/adr/0015-transform-and-reverse-etl-layer.md)
and [ADR-0021](https://github.com/seandavi/cdsci-lake/blob/main/docs/adr/0021-plain-sql-transforms-and-sqlglot-lineage.md)
add a plain-SQL transform layer (`transform/models/<schema>/<table>.sql`) with
lineage in `lake_ops`. ADR-0021 also says each producer owns its derived tables
and that other producers consume them by data contract.
[ADR-0019](https://github.com/seandavi/cdsci-lake/blob/main/docs/adr/0019-shared-sqlmesh-state-and-single-internal-ducklake.md)
decision 2 keeps published products independent of the lake. The lake already
has source-scoped transform models for the sources bioc-on-ice uses:
`ncbi_gene.mapping`, `ncbi_gene2go.gene_go`, `ensembl.gene`/`transcript`/`exon`,
`uniprot.identifier_mapping` and `bugsigdb.signature_taxon`.

## Decision

### (a) #178 is superseded; one thing survives

ADR-0013 and ADR-0014 replace #178's destination: no release SCD2, no Frozen
DuckLake, no optional Iceberg adapter, and no shared release builder. The public
bundle is bioc-on-ice's `publish.py`. #178's remaining route is #183.

What survives is **where writers live**. Code that fetches upstream and lands it
(raw tables landed whole, plus keyed source-scoped curation such as parsing,
typing and per-source cleanup) lives in **cdsci-lake**, as a source ingestor and
that source's transform models. Each migrated bioc-on-ice lander (`ncbi.py`,
`rnacentral.py`, ...) retires once its source is in the lake. bioc-on-ice keeps
no write path to the shared lake. #178's invariants also stand: SPEC.md is the
biological authority, domain schemas, identifier namespaces and biological joins
stay in bioc-on-ice (decision b says where), there is no source framework, and
there are no gateway concerns.

### (b) #185: the product derives its tables at release time

Product tables are **SELECTs in `publish.TABLES`** over lake tables. They are
not written back into the shared lake. Source-shaped curation belongs in the
lake. Product shape belongs in the release:

| lives in cdsci-lake (EL or that source's transform model) | lives in `publish.TABLES` (a release SELECT) |
| --- | --- |
| one source, its own keys and grain | anything across sources (the `identifier_mapping` union, `annotation.interaction`, `resource.resource_relationship`) |
| parsing, typing, null cleanup | bioc-on-ice's namespace vocabulary and renames (`ENSEMBL_TRANSCRIPT`, `REFSEQ_RNA`) |
| landed whole, all taxa | scoping and filtering to what the product publishes (decision c) |

A derived table is materialised in the lake only when one of these is true:

1. **A second consumer.** Another producer (omicidx, cancer-on-ice) needs the
   table. It then becomes a cdsci-lake transform model owned by bioc-on-ice
   (ADR-0021), and the release SELECT becomes `SELECT * FROM lake.<schema>.<table>`.
2. **Too expensive to rebuild per release.** The SELECT cannot stream within the
   build host's memory or the release's wall-clock budget.

Neither is true today. ADR-0021 records no cross-producer consumption, and every
table in release 2026-10-31 (80 tables, 4.23B rows) was built by `COPY (SELECT
...)` from a live catalog.

Why on the merits:

- **Reuse sits where it already lives.** What other consumers can reuse is the
  per-source curation, and that is already lake-side (`ncbi_gene.mapping` is
  "deliberately NOT shaped for bioc-on-ice's annotation.identifier_mapping"). The
  product union is bioc-on-ice's vocabulary, and nobody else asks for it.
  Materialising it would store a second copy of ~464M rows that only the release
  reads.
- **cdsci-lake ADR-0013.** The EL path cannot take these tables at all. They are
  cross-table, so the only lake home would be the transform layer, and running
  models there buys `rebuild` plus lake-side lineage for tables with no lake
  reader.
- **Lineage stays recoverable.** Each SELECT is in git at `manifest.json`'s
  `git_sha`, and the lake tables it reads are named in the SQL. The missing piece
  is the lake version. A release built from the lake must read **one pinned lake
  snapshot** (`AT (VERSION => sid)`, as `release.py` does for BugSigDB) and
  record that snapshot id in `manifest.json` provenance. That lands with step 10
  of #183, when the release first builds from the lake. Release-time SELECTs do
  not appear in `lake_ops.column_lineage`. That is accepted: ADR-0021 treats
  lineage as observability, and the release manifest is the public record.
- **One gate.** Step 5's release-over-release gate checks what users actually
  get, the release Parquet, so an intermediate lake copy would be a second thing
  to verify.

### (c) #162: ENA composite ids stay in raw, out of `identifier_mapping`

`annotation.identifier_mapping`'s RNAcentral branch excludes `database = 'ENA'`.
ENA rows stay landed whole (SPEC "Raw and derived are separate layers") and stay
published in `raw.rnacentral__id_mapping` with `urs` and `taxon_id`, so a user
who wants them has them. This applies to every taxon, including the human and
mouse ENA rows already in the table. Measured on release 2026-10-31:

| | rows |
| --- | --- |
| `raw.rnacentral__id_mapping` | 264,333,345 |
| of which `database = 'ENA'` | 228,569,979 (86.5%) |
| all other databases, all taxa | 35,763,366 |
| `identifier_mapping` today (all sources) | 464,335,857 |
| of which `source = 'RNACENTRAL'` (9606, 10090) | 5,911,834 |
| of which ENA targets | 1,918,777 (32%) |

The join check was run against the human and mouse slice. The 1,918,777 ENA
targets have **0** exact matches among the 2,024,806 distinct ids that any other
source puts in `identifier_mapping` for those taxa. Cut to the accession before
the first `:`, 3,652 of 231,472 distinct accessions (1.6%) match, and 10 match
unversioned. The composite `accession:range:feature` names a feature span inside
an INSDC record. No other source uses that form, and the stored value cannot
join without parsing.

The all-taxa derive therefore adds about 30M rows (the 35.8M non-ENA rows, less
the human and mouse rows already present and duplicates), not about 260M.
`identifier_mapping` stays under ~500M rows instead of growing past ~720M, a
growth of roughly 7% rather than 55% (upper bounds from raw counts, before
dedup).

The size argument in #162 was partly about sharding. That concern goes away,
because a release SELECT streams through `COPY` with no PyIceberg merge. The
decision rests on the remaining reasons: nothing joins to the ids, they would
make up about a third of the table, and raw already serves them. #162's
other worry, that rows become permanent Type-2 history, no longer applies under
ADR-0013. Each release is a full snapshot, so a later release can add ENA back
if a consumer asks. A separate `annotation.rnacentral__ena` table (#162 option 3)
is not built until someone asks for it. #162's side notes (`REFSEQ_RNA`
versioning, bare vs `URS_taxid`) are not decided here.

## Scratch catalog recipe

Until step 10 of #183, migrations write only to a local scratch DuckLake. The
`CU_OPENALEX_LOCAL_*` names in #183 do not exist. cdsci-lake v0.3.1's local
backend reads the settings below. `Settings` also reads `./.env`, so run from
the empty scratch directory:

```bash
D=$(mktemp -d /data/davsean/tmp/bioconice-scratch-lake.XXXXXX)
cd "$D"
export CU_OPENALEX_LAKE_BACKEND=local
export CU_OPENALEX_STORAGE_BASE_URI="file://$D"
export CU_OPENALEX_LAKE_CATALOG="$D/catalog.ducklake"
export CU_OPENALEX_LAKE_DATA_PREFIX=data
export CU_OPENALEX_DUCKDB_TEMP_DIRECTORY="$D/duckdb_tmp"
# ... lake_connect() writes to $D only; the ops ledger is $D/ops.duckdb
rm -rf "$D"
```

This was verified on 2026-10-31 (cdsci-lake v0.3.1, DuckDB 1.5.5). `lake_connect()`
attached `lake` as `$D/catalog.ducklake` and `ops` as `$D/ops.duckdb`, with
nothing else attached. Creating `lake.scratch_check.t` (3 rows), reading it back,
and re-reading it through `lake_connect(read_only=True)` all worked. The
directory was then removed. A table that small is inlined into the catalog file,
so no Parquet appeared under `data/`. `CU_OPENALEX_LAKE_BACKEND=postgres` is the
shared lake and is never set for a write before the owner's go at step 10.

## Consequences

- bioc-on-ice is a read-only lake consumer. Its write code shrinks to
  `publish.py` plus the release SELECTs, and the PyIceberg landers retire source
  by source (#183). SPEC.md is not edited here. Its raw/derived section ("derived
  tables are computed by reading `raw` back out") still holds, and its Iceberg
  wording changes at step 11, as ADR-0014 already arranged.
- A migrated table's SELECT, not a lander, is where bioc-on-ice's biological
  rules live. Those SELECTs need offline tests against a fixture attached as
  `lake`, the same discipline `tests/tiny.gtf` gives the landers.
- The first release with (c) applied drops the 1,918,777 ENA rows from
  `identifier_mapping` and, with the all-taxa derive, adds about 30M. Step 5's
  row-count bounds must expect that change once.
- `uniprot.identifier_mapping` in cdsci-lake is already shaped for this product.
  It can stay, but new product-shaped models do not go into the lake unless a
  trigger in (b) fires.
- If a second producer asks for a bioc-on-ice derived table, trigger 1 applies,
  and that does not need a new ADR.
