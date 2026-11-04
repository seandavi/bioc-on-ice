# Lake migration map, 2026-11-01

**Status**: research note for #183 step 7, not a decision. It maps every table
in `publish.TABLES` (81 tables, release `v2026-11-01`) to its cdsci-lake
equivalent and proposes the order for step 9. Row counts come from
`https://data.bioconice.cancerdatasci.org/v2026-11-01/manifest.json`. Lake
names, columns and counts were read read-only from the shared lake
(`lake_connect(read_only=True)`, `CU_OPENALEX_LAKE_BACKEND=postgres`) on
2026-11-01. Source modules were read at cdsci-lake `631ffc9`. How the two
step-7 migrations were done is in [`migration-log-step7.md`](migration-log-step7.md).

## Summary

| effort | tables | what it means |
|---|---:|---|
| repoint-only | 3 | the lake table already has the release shape: change the SELECT. 1 done in step 7 (`signature_taxon`); `raw.metadata`; `pubmed__article` is already lake-backed and only needs the cdsci-lake#136 rename |
| small adapter | 15 | the lake already lands the source. The release SELECT casts, renames or unions, or the lake module gains one more dump |
| new source module | 44 | no lake source exists. Port the lander's EL half to `cdsci-lake/src/cdsci/lake/sources/<source>/`; the transform half becomes release SELECTs. 5 done in step 7 (HGNC, Complex Portal) |
| hard | 19 | Ensembl (scope), `identifier_mapping` (9-writer union), iCite, PubTator3, RNAcentral (size), `provenance.release` (step-10 design) |

By wave (the `#` column): **A** done or repoint-only (8), **B** small
self-contained new sources (14), **C** NCBI Gene family on existing lake
modules (12), **D** resource family (15), **E** interactions (3), **F** OBO
(10), **G** Ensembl and `identifier_mapping` (6), **H** iCite, PubTator3,
RNAcentral under wall-clock caps (12), **Z** `provenance.release` (1, step 10).

### Proposed order for step 9

1. **A**: merge step 7; repoint `raw.metadata` to `lake.icite.metadata`
   (same columns and types). `pubmed__article` follows cdsci-lake#136.
2. **B**: WikiPathways, GWAS Catalog, `raw.bugsigdb__full_dump`, MANE,
   Cellosaurus. All are small and need no other source. MANE also writes
   `identifier_mapping`; that branch stays on Iceberg until G. Cellosaurus
   writes none (its cross-references are `cellosaurus__xref`).
3. **C**: NCBI Gene on the lake modules that already exist (`ncbi_gene2go`,
   `ncbi_gene2pubmed`, `ncbi_gene`, `ncbi_gene2accession`), plus
   `gene_history`, `gene_orthologs` and `gene_group` dumps added to
   `ncbi_gene`. Four of these tables had mixed `valid_from` (below; moot after #222).
   ADR-0026 folds the `ncbi_gene2*` schemas into `ncbi_gene` at migration.
4. **D**: eQTL Catalogue, CELLxGENE, ENCODE, BEDbase, then
   `resource.resource_relationship` as one union SELECT.
5. **E**: BioGRID, IntAct, then `annotation.interaction` as one union SELECT.
6. **F**: OBO. **Owner decided (2026-11-01): land the OBO Graph JSON as a new
   `obo` source** (exact equivalence), not derived from the semantic-sql
   `lake.ontology.*`. Equivalence on the Iceberg versions (2026-11-03): exact
   for `ontology.*` and five raw tables. In `raw.obo__cl`, `uberon` and
   `mondo`, the published `meta` text changes byte for byte while its JSON
   value stays the same: 21,827, 34,357 and 40,284 rows now carry upstream's
   text verbatim, with upstream key order and no explicit nulls. Iceberg
   holds the #83 lander's `to_json` of an inferred struct for those three
   ontologies, and #92 replaced that read with JSON paths before the other
   five were landed. Every one of those rows pairs by
   `(kind, id, sub, pred, obj)` and differs in `meta` only, and the two
   strings parse to the same value once null keys are dropped. Release notes
   should say so for anyone string-matching `meta`.
7. **G**: Ensembl. **Owner decided (2026-11-01): publish today's 276
   assemblies** (the lake holds 359; memory `ensembl-one-assembly-per-taxon`);
   widening is a separate decision with #94. Then `identifier_mapping` as a union
   of per-writer branches, each switched to the lake as its writer migrates.
8. **H**: iCite, PubTator3, RNAcentral, last, under wall-clock caps.
9. **Z**: `provenance.release` is replaced at step 10 by the pinned lake
   snapshot ids in `manifest.json` (ADR-0015).

## Cross-cutting findings

- **#222 touches 77 of 81 tables, not ~45.** 35 tables carry
  `valid_from`/`valid_to` and 42 raw tables carry `landed_in`. #222 drops all
  three from every release, so a lake-backed SELECT carries none of them and
  needs no frozen constant.
- **8 SCD2 tables had mixed `valid_from`** (`2026.08` and `2026.09`, marked ⚑):
  `reference.genome`, `annotation.gene`, `annotation.transcript`,
  `annotation.exon`, `annotation.ncbi__gene`, `annotation.ncbi__gene_go`,
  `annotation.ncbi__gene_pubmed`, `annotation.identifier_mapping`. With #222
  this no longer blocks them.
- **Raw tables hold one version in the release**, so the lake's
  latest-snapshot tables match them row for row. No raw table depends on
  Iceberg accumulating versions.
- **Cross-source tables migrate branch by branch.** `identifier_mapping`
  (9 writers), `resource_relationship` (4) and `interaction` (2) are release
  SELECT unions (ADR-0015 b). A union can mix lake and `bioc.*` branches
  while its writers migrate. Until HGNC's branch moves, the HGNC rows in
  `identifier_mapping` stay at the Iceberg version (2026-09-18) while
  `hgnc__gene` follows the lake.
- **The lake is newer.** Every existing lake table is a later upstream dump
  than the Iceberg one (e.g. `ncbi_gene.gene` 73.7M vs 72.2M). A migration is
  checked on the same upstream version first (exact), then on the current
  one (differences explained); the gate's default bounds (0.9, 1.5) absorbed
  every difference seen so far.
- **Licences**: 37 of 78 declared tables carry no `bioc.license` property.
  `gate.declared_contract` needs `license=` for those.

## Per table

`reads now` is the SELECT after this PR. Lake paths are in
`seandavi/cdsci-lake`. `src/cdsci/lake/sources/<x>` is an EL source;
`transform/models/<x>/<t>.sql` is a lake transform model.

| # | table | rows v2026-11-01 | family | reads now | cdsci-lake equivalent | difference | effort |
|---|---|---:|---|---|---|---|---|
| A1 | `annotation.signature_taxon` | 59,486 | BugSigDB | lake (this PR; was `bioc.*`) | `bugsigdb.signature_taxon` (transform/models/bugsigdb/signature_taxon.sql) | none: 0-row diff, same columns/types (step 7) | repoint-only ✅ step 7 |
| A2 | `annotation.hgnc__gene` | 45,083 | HGNC | lake (this PR; was `bioc.*`) | `hgnc.gene` (src/cdsci/lake/sources/hgnc) | derived by release SELECT (`hgnc.RELEASE`); no row-history columns (#222) | new source module ✅ step 7 |
| A2 | `raw.hgnc__complete_set` | 45,083 | HGNC | lake (this PR; was `bioc.*`) | `hgnc.gene` (src/cdsci/lake/sources/hgnc, cdsci-lake PR) | no `landed_in` (#222); one version, not accumulated | new source module ✅ step 7 |
| A3 | `annotation.complexportal__complex` | 20,579 | Complex Portal | lake (this PR; was `bioc.*`) | `complexportal.complex` (src/cdsci/lake/sources/complexportal) | derived by release SELECT; no row-history columns (#222) | new source module ✅ step 7 |
| A3 | `annotation.complexportal__participant` | 101,561 | Complex Portal | lake (this PR; was `bioc.*`) | `complexportal.complex` (src/cdsci/lake/sources/complexportal) | participant explosion is a release SELECT; no row-history columns (#222) | new source module ✅ step 7 |
| A3 | `raw.complexportal__complex` | 20,579 | Complex Portal | lake (this PR; was `bioc.*`) | `complexportal.complex` (src/cdsci/lake/sources/complexportal, cdsci-lake PR) | no `landed_in` (#222) | new source module ✅ step 7 |
| A4 | `raw.metadata` | 40,737,811 | iCite | Iceberg `bioc.*` | `icite.metadata` (src/cdsci/lake/sources/icite) | same 17 columns and types (it is the lake table's old reverse-ETL copy); lake 41,172,752 rows vs 40,737,811 (newer snapshot, within bounds) | repoint-only |
| A5 | `annotation.pubmed__article` | 41,336,046 | PubMed | lake | `omicidx.pubmed_article` (omicidx producer) | already lake-backed; rename to `pubmed.article` with cdsci-lake#136 (ADR-0026) | repoint-only (rename) |
| B1 | `annotation.wikipathways__gene_pathway` | 76,046 | WikiPathways | lake (migrate/wikipathways; was `bioc.*`) | `wikipathways.gene_set` (src/cdsci/lake/sources/wikipathways, cdsci-lake PR) | release SELECT over the new lake table (`wikipathways.RELEASE`); no row-history columns (#222) | new source module |
| B1 | `annotation.wikipathways__pathway` | 1,957 | WikiPathways | lake (migrate/wikipathways; was `bioc.*`) | `wikipathways.gene_set` (src/cdsci/lake/sources/wikipathways, cdsci-lake PR) | release SELECT over the new lake table (`wikipathways.RELEASE`); no row-history columns (#222) | new source module |
| B1 | `raw.wikipathways__gmt` | 1,957 | WikiPathways | lake (migrate/wikipathways; was `bioc.*`) | `wikipathways.gene_set` (src/cdsci/lake/sources/wikipathways, cdsci-lake PR) | no `landed_in` (#222); one version, not accumulated | new source module |
| B2 | `clinical.gwas_catalog__association` | 1,192,472 | GWAS Catalog | lake (migrate/gwas_catalog; was `bioc.*`) | `gwas_catalog.association` (src/cdsci/lake/sources/gwas_catalog, cdsci-lake PR) | release SELECT over the new lake table (`gwas_catalog.RELEASE`); no row-history columns (#222) | new source module |
| B2 | `clinical.gwas_catalog__study` | 230,057 | GWAS Catalog | lake (migrate/gwas_catalog; was `bioc.*`) | `gwas_catalog.study` (src/cdsci/lake/sources/gwas_catalog, cdsci-lake PR) | release SELECT over the new lake table (`gwas_catalog.RELEASE`); no row-history columns (#222) | new source module |
| B2 | `raw.gwas_catalog__associations` | 1,192,604 | GWAS Catalog | lake (migrate/gwas_catalog; was `bioc.*`) | `gwas_catalog.association` (src/cdsci/lake/sources/gwas_catalog, cdsci-lake PR) | no `landed_in` (#222); the lake's key `line_number` is not published; one version, not accumulated | new source module |
| B2 | `raw.gwas_catalog__studies` | 230,057 | GWAS Catalog | lake (migrate/gwas_catalog; was `bioc.*`) | `gwas_catalog.study` (src/cdsci/lake/sources/gwas_catalog, cdsci-lake PR) | no `landed_in` (#222); one version, not accumulated | new source module |
| B3 | `raw.bugsigdb__full_dump` | 7,425 | BugSigDB | lake (this PR; was `bioc.*`) | `bugsigdb.signatures` (src/cdsci/lake/sources/bugsigdb) | release SELECT (`bugsigdb.RELEASE`) casts pmid/year/sample sizes back to text and `curated_date` back with `strftime('%-d %B %Y')` (upstream does not zero-pad the day); no `landed_in` (#222). 0-row diff at v1.3.1, same columns/types | small adapter ✅ |
| B4 | `annotation.mane__transcript` | 19,437 | MANE | lake (migrate/mane; was `bioc.*`) | `mane.transcript` (src/cdsci/lake/sources/mane, cdsci-lake PR) | release SELECT over the new lake table (`mane.RELEASE`); no row-history columns (#222) | new source module |
| B4 | `raw.ncbi__mane_summary` | 19,437 | MANE | lake (migrate/mane; was `bioc.*`) | `mane.transcript` (src/cdsci/lake/sources/mane, cdsci-lake PR) | no `landed_in` (#222); one version, not accumulated; MANE's `identifier_mapping` slice stays on Iceberg | new source module |
| B5 | `annotation.cellosaurus__cell_line` | 168,970 | Cellosaurus | lake (migrate/cellosaurus; was `bioc.*`) | `cellosaurus.line` (src/cdsci/lake/sources/cellosaurus, cdsci-lake PR) | release SELECT over the new lake table (`cellosaurus.RELEASE`); no row-history columns (#222) | new source module |
| B5 | `annotation.cellosaurus__disease` | 124,953 | Cellosaurus | lake (migrate/cellosaurus; was `bioc.*`) | `cellosaurus.line` (src/cdsci/lake/sources/cellosaurus, cdsci-lake PR) | release SELECT over the new lake table (`cellosaurus.RELEASE`); no row-history columns (#222) | new source module |
| B5 | `annotation.cellosaurus__xref` | 473,381 | Cellosaurus | lake (migrate/cellosaurus; was `bioc.*`) | `cellosaurus.line` (src/cdsci/lake/sources/cellosaurus, cdsci-lake PR) | release SELECT over the new lake table (`cellosaurus.RELEASE`); no row-history columns (#222) | new source module |
| B5 | `raw.cellosaurus__release` | 3,188,709 | Cellosaurus | lake (migrate/cellosaurus; was `bioc.*`) | `cellosaurus.line` (src/cdsci/lake/sources/cellosaurus, cdsci-lake PR) | no `landed_in` (#222); one version, not accumulated | new source module |
| C1 | `annotation.ncbi__gene_go` | 124,738,605 | NCBI gene2go | Iceberg `bioc.*` | `ncbi_gene2go.gene_go` (transform/models/ncbi_gene2go/gene_go.sql) | lake adds `go_label`, `go_obsolete`; `gene_id` BIGINT; 126.1M vs 124.7M; mixed `valid_from` | small adapter |
| C1 | `raw.ncbi__gene2go` | 124,738,605 | NCBI gene2go | Iceberg `bioc.*` | `ncbi_gene2go.gene2go` (src/cdsci/lake/sources/ncbi_gene2go) | lake `gene_id` BIGINT vs VARCHAR; adds `snapshot_version`, no `landed_in`; 126.1M vs 124.7M rows (newer dump) | small adapter |
| C2 | `annotation.ncbi__gene_pubmed` | 82,882,628 | NCBI gene2pubmed | Iceberg `bioc.*` | `ncbi_gene2pubmed.gene_publication` (transform/models/ncbi_gene2pubmed/gene_publication.sql) | rename `pmid`→`pubmed_id`, casts; 84.2M vs 82.9M; mixed `valid_from` | small adapter |
| C2 | `raw.ncbi__gene2pubmed` | 82,882,628 | NCBI gene2pubmed | Iceberg `bioc.*` | `ncbi_gene2pubmed.gene2pubmed` (src/cdsci/lake/sources/ncbi_gene2pubmed) | lake `pmid` BIGINT vs `pubmed_id` VARCHAR, `gene_id` BIGINT; `snapshot_version` vs `landed_in`; 84.2M vs 82.9M | small adapter |
| C3 | `annotation.ncbi__gene` | 72,153,077 | NCBI Gene | Iceberg `bioc.*` | `ncbi_gene.gene` (transform/models/ncbi_gene/gene.sql) | same columns, `gene_id` BIGINT vs VARCHAR; 73.7M vs 72.2M; mixed `valid_from` | small adapter |
| C3 | `raw.ncbi__gene2ensembl` | 19,922,787 | NCBI Gene | Iceberg `bioc.*` | `ncbi_gene.gene2ensembl` (src/cdsci/lake/sources/ncbi_gene) | lake `gene_id` BIGINT; `snapshot_version` vs `landed_in`; 20.0M vs 19.9M | small adapter |
| C3 | `raw.ncbi__gene_history` | 27,395,424 | NCBI Gene | Iceberg `bioc.*` | — (add `gene_history` to `ncbi_gene.DUMP_COLUMNS`) | dump not landed; `ncbi_gene.land` is dump-agnostic | small adapter |
| C3 | `raw.ncbi__gene_info` | 72,175,792 | NCBI Gene | Iceberg `bioc.*` | `ncbi_gene.gene_info` (src/cdsci/lake/sources/ncbi_gene) | lake `gene_id` BIGINT; `snapshot_version` vs `landed_in`; 73.7M vs 72.2M | small adapter |
| C4 | `annotation.ortholog` | 37,755,846 | NCBI orthologs | Iceberg `bioc.*` | — | release SELECT over the two dumps | small adapter |
| C4 | `raw.ncbi__gene_group` | 51,196 | NCBI orthologs | Iceberg `bioc.*` | — (add dump to `ncbi_gene`) | dump not landed | small adapter |
| C4 | `raw.ncbi__gene_orthologs` | 18,877,923 | NCBI orthologs | Iceberg `bioc.*` | — (add dump to `ncbi_gene`) | dump not landed | small adapter |
| C5 | `raw.ncbi__gene2accession` | 288,151,046 | NCBI gene2accession | Iceberg `bioc.*` | `ncbi_gene2accession.gene2accession` (src/cdsci/lake/sources/ncbi_gene2accession) | lake renames 8 columns (`rna_accession` vs `rna_nucleotide_accession_version` …) and types gi/positions BIGINT; release is all VARCHAR; 290.5M vs 288.2M | small adapter |
| D1 | `raw.eqtlcatalogue__dataset` | 758 | eQTL Catalogue | lake (migrate/eqtl_catalogue; was `bioc.*`) | `eqtl_catalogue.dataset` (src/cdsci/lake/sources/eqtl_catalogue, cdsci-lake PR) | no `landed_in` (#222); one release, not accumulated | new source module |
| D1 | `raw.eqtlcatalogue__tabix_ftp_paths` | 758 | eQTL Catalogue | lake (migrate/eqtl_catalogue; was `bioc.*`) | `eqtl_catalogue.tabix_ftp_path` (src/cdsci/lake/sources/eqtl_catalogue, cdsci-lake PR) | no `landed_in` (#222); one release, not accumulated | new source module |
| D1 | `resource.eqtlcatalogue__dataset` | 758 | eQTL Catalogue | lake (migrate/eqtl_catalogue; was `bioc.*`) | `eqtl_catalogue.dataset` + `eqtl_catalogue.tabix_ftp_path` (src/cdsci/lake/sources/eqtl_catalogue, cdsci-lake PR) | release SELECT over the new lake tables (`eqtlcatalogue.RELEASE`); no row-history columns (#222); its `resource_relationship` slice is a branch of D5 | new source module |
| D2 | `raw.cellxgene__dataset` | 2,228 | CELLxGENE | lake (migrate/cellxgene; was `bioc.*`) | `cellxgene.dataset` (src/cdsci/lake/sources/cellxgene, cdsci-lake PR) | no `landed_in` (#222); one crawl; the lake's `census_release` column is not published here | new source module |
| D2 | `resource.cellxgene__dataset` | 2,228 | CELLxGENE | lake (migrate/cellxgene; was `bioc.*`) | `cellxgene.dataset` (src/cdsci/lake/sources/cellxgene, cdsci-lake PR) | release SELECT over the lake table (`cellxgene.RELEASE`); `census_release` is stamped by the cdsci-lake ingest from the Census manifest's `stable` alias; no row-history columns (#222); its `resource_relationship` slice is a branch of D5 | new source module |
| D3 | `raw.encode__experiment` | 28,642 | ENCODE | lake (migrate/encode; was `bioc.*`) | `encode.experiment` (src/cdsci/lake/sources/encode, cdsci-lake PR) | no `landed_in` (#222); one crawl | new source module |
| D3 | `raw.encode__file` | 1,659,000 | ENCODE | lake (migrate/encode; was `bioc.*`) | `encode.file` (src/cdsci/lake/sources/encode, cdsci-lake PR) | no `landed_in` (#222); one crawl | new source module |
| D3 | `resource.encode__experiment` | 28,642 | ENCODE | lake (migrate/encode; was `bioc.*`) | `encode.experiment` (src/cdsci/lake/sources/encode, cdsci-lake PR) | release SELECT over the lake table (`encode.RELEASE`); no row-history columns (#222); its `resource_relationship` slice is a branch of D5 | new source module |
| D3 | `resource.encode__file` | 1,659,000 | ENCODE | lake (migrate/encode; was `bioc.*`) | `encode.file` (src/cdsci/lake/sources/encode, cdsci-lake PR) | release SELECT over the lake table (`encode.RELEASE`); no row-history columns (#222) | new source module |
| D4 | `raw.bedbase__bed` | 663,721 | BEDbase | lake (migrate/bedbase; was `bioc.*`) | `bedbase.bed` (src/cdsci/lake/sources/bedbase, cdsci-lake PR) | no `landed_in` (#222); one snapshot; the lake's `bedbase_version` column is not published here | new source module |
| D4 | `raw.bedbase__bedset` | 22,188 | BEDbase | lake (migrate/bedbase; was `bioc.*`) | `bedbase.bedset` (src/cdsci/lake/sources/bedbase, cdsci-lake PR) | as above | new source module |
| D4 | `raw.bedbase__bedset_membership` | 578,552 | BEDbase | lake (migrate/bedbase; was `bioc.*`) | `bedbase.bedset_membership` (src/cdsci/lake/sources/bedbase, cdsci-lake PR) | as above | new source module |
| D4 | `resource.bedbase__bedfile` | 663,721 | BEDbase | lake (migrate/bedbase; was `bioc.*`) | `bedbase.bed` (src/cdsci/lake/sources/bedbase, cdsci-lake PR) | release SELECT over the lake table (`bedbase.RELEASE`); no row-history columns (#222); its `resource_relationship` slice is a branch of D5 | new source module |
| D4 | `resource.bedbase__bedset` | 22,188 | BEDbase | lake (migrate/bedbase; was `bioc.*`) | `bedbase.bedset` (src/cdsci/lake/sources/bedbase, cdsci-lake PR) | release SELECT over the lake table (`bedbase.RELEASE`); no row-history columns (#222) | new source module |
| D5 | `resource.resource_relationship` | 3,734,643 | resource (cross-source) | lake (migrate/resource_relationship; was `bioc.*`) | — (union over D1–D4's lake tables) | one release SELECT, a UNION ALL of each writer's `RELATIONSHIP` (ADR-0015 b), the landers' own SQL. 0-row diff both ways on the Iceberg inputs; on the 2026-11-03 lake, eQTL Catalogue and ENCODE exact, BEDbase +6,233 (snapshot 2026-11-01 vs 2026-09-01: 1,928 new files, 2,175 new memberships), CELLxGENE +2,113 / −1,315 (crawl 2026-11-03 vs 2026-09-18: 98 new dataset versions, 57 retired) | small adapter (union) ✅ |
| E1 | `raw.biogrid__interactions` | 2,936,093 | BioGRID | lake (migrate/biogrid; was `bioc.*`) | `biogrid.interaction` (src/cdsci/lake/sources/biogrid, cdsci-lake PR) | no `landed_in` (#222); one release; its `interaction` slice is a branch of E3 (BioGRID writes no `identifier_mapping` rows) | new source module |
| E2 | `raw.intact__mitab` | 1,788,408 | IntAct | lake (migrate/intact; was `bioc.*`) | `intact.interaction` (src/cdsci/lake/sources/intact, cdsci-lake PR) | no `landed_in` (#222); one release; keyed `(file_name, line_number)` in the lake, not published (2,144 whole-row duplicate rows kept); its `interaction` slice is a branch of E3 | new source module |
| E3 | `annotation.interaction` | 4,722,393 | interaction (cross-source) | lake (migrate/interaction; was `bioc.*`) | — (union over E1–E2's lake tables) | one release SELECT, a UNION ALL of each writer's `INTERACTION` (ADR-0015 b), the landers' own SQL. 0-row diff both ways on the Iceberg inputs; on the 2026-11-03 lake, BioGRID +28,551 / −15 (5.0.263 vs 5.0.261: new and withdrawn ids only), IntAct +97,903 / −70,136 (2026-10-17 vs 2026-01-09: 12,822 new ACs, 8 withdrawn, 69,871 pairs re-identified or reordered on shared ACs, 255 attribute changes) | small adapter (union) ✅ |
| F1 | `ontology.relationship` | 514,649 | OBO ontologies | lake (migrate/obo; was `bioc.*`) | `obo.element` (src/cdsci/lake/sources/obo, cdsci-lake PR) | derived by release SELECT (`obo.RELEASE`), the lander's own SQL over every ontology's rows; no row-history columns (#222) | new source module |
| F1 | `ontology.term` | 273,258 | OBO ontologies | lake (migrate/obo; was `bioc.*`) | `obo.element` (src/cdsci/lake/sources/obo, cdsci-lake PR) | derived by release SELECT (`obo.RELEASE`), the lander's own SQL over every ontology's rows; no row-history columns (#222) | new source module |
| F1 | `raw.obo__cl` | 70,707 | OBO ontologies | lake (migrate/obo; was `bioc.*`) | `obo.element` (src/cdsci/lake/sources/obo, cdsci-lake PR) | the ontology's rows of the lake table, without its `ontology` column; no `landed_in` (#222); one release per ontology, not accumulated. Owner decided: a new `obo` source landing the JSON | new source module |
| F1 | `raw.obo__doid` | 41,594 | OBO ontologies | lake (migrate/obo; was `bioc.*`) | `obo.element` (src/cdsci/lake/sources/obo, cdsci-lake PR) | the ontology's rows of the lake table, without its `ontology` column; no `landed_in` (#222); one release per ontology, not accumulated. Owner decided: a new `obo` source landing the JSON | new source module |
| F1 | `raw.obo__efo` | 269,697 | OBO ontologies | lake (migrate/obo; was `bioc.*`) | `obo.element` (src/cdsci/lake/sources/obo, cdsci-lake PR) | the ontology's rows of the lake table, without its `ontology` column; no `landed_in` (#222); one release per ontology, not accumulated. Owner decided: a new `obo` source landing the JSON | new source module |
| F1 | `raw.obo__go` | 123,546 | OBO ontologies | lake (migrate/obo; was `bioc.*`) | `obo.element` (src/cdsci/lake/sources/obo, cdsci-lake PR) | the ontology's rows of the lake table, without its `ontology` column; no `landed_in` (#222); one release per ontology, not accumulated. Owner decided: a new `obo` source landing the JSON | new source module |
| F1 | `raw.obo__hsapdv` | 1,004 | OBO ontologies | lake (migrate/obo; was `bioc.*`) | `obo.element` (src/cdsci/lake/sources/obo, cdsci-lake PR) | the ontology's rows of the lake table, without its `ontology` column; no `landed_in` (#222); one release per ontology, not accumulated. Owner decided: a new `obo` source landing the JSON | new source module |
| F1 | `raw.obo__mmusdv` | 606 | OBO ontologies | lake (migrate/obo; was `bioc.*`) | `obo.element` (src/cdsci/lake/sources/obo, cdsci-lake PR) | the ontology's rows of the lake table, without its `ontology` column; no `landed_in` (#222); one release per ontology, not accumulated. Owner decided: a new `obo` source landing the JSON | new source module |
| F1 | `raw.obo__mondo` | 178,267 | OBO ontologies | lake (migrate/obo; was `bioc.*`) | `obo.element` (src/cdsci/lake/sources/obo, cdsci-lake PR) | the ontology's rows of the lake table, without its `ontology` column; no `landed_in` (#222); one release per ontology, not accumulated. Owner decided: a new `obo` source landing the JSON | new source module |
| F1 | `raw.obo__uberon` | 102,486 | OBO ontologies | lake (migrate/obo; was `bioc.*`) | `obo.element` (src/cdsci/lake/sources/obo, cdsci-lake PR) | the ontology's rows of the lake table, without its `ontology` column; no `landed_in` (#222); one release per ontology, not accumulated. Owner decided: a new `obo` source landing the JSON | new source module |
| G1 | `annotation.exon` | 143,333,679 | Ensembl | Iceberg `bioc.*` | `ensembl.exon` (transform/models/ensembl/exon.sql) | lake adds `species`, `ensembl_release`; 198.9M vs 143.3M rows (more assemblies) | hard |
| G1 | `annotation.gene` | 7,419,569 | Ensembl | Iceberg `bioc.*` | `ensembl.gene` (transform/models/ensembl/gene.sql) | lake adds coordinates, `species`, `ensembl_release`; 10.9M vs 7.4M rows (more assemblies) | hard |
| G1 | `annotation.transcript` | 14,092,671 | Ensembl | Iceberg `bioc.*` | `ensembl.transcript` (transform/models/ensembl/transcript.sql) | as above; 20.7M vs 14.1M | hard |
| G1 | `raw.ensembl__gtf` | 334,547,700 | Ensembl | Iceberg `bioc.*` | `ensembl.gtf` (src/cdsci/lake/sources/ensembl) | lake keys by assembly and adds `species`, `assembly`, `genome_accession`; `ncbitaxon_id` vs `taxon_id`; 466.4M rows (359 genomes) vs 334.5M (276): which assemblies to publish is a scope decision (memory ensembl-one-assembly-per-taxon) | hard |
| G1 | `reference.genome` | 276 | Ensembl | Iceberg `bioc.*` | `ensembl.genome` (transform/models/ensembl/genome.sql) | grain (species, release) vs (genome_id, taxon, source); `assembly_name`/`source` same; mixed `valid_from` | hard |
| G2 | `annotation.identifier_mapping` | 464,335,857 | identifier mapping (cross-source) | Iceberg `bioc.*` | `ncbi_gene.mapping`, `ncbi_gene2accession.mapping`, `uniprot.identifier_mapping` (transform/models/…) | union of 8 writers (NCBI, NCBI accession, Ensembl, HGNC, MANE, BioGRID, IntAct, RNAcentral); 464M rows; ENA rows drop under ADR-0015 c; mixed `valid_from`. Migrate as a release-SELECT union, one branch per migrated writer | hard |
| H1 | `annotation.icite__citation` | 928,458,585 | iCite | Iceberg `bioc.*` | — | 928M citation edges from `cited_by`/`references`; not in the lake | hard |
| H1 | `annotation.icite__metrics` | 41,035,229 | iCite | Iceberg `bioc.*` | — (subset of `icite.metadata`) | needs `molecular_cellular`, `provisional`, `snapshot` | hard |
| H1 | `annotation.icite__publication` | 41,035,229 | iCite | Iceberg `bioc.*` | — (subset of `icite.metadata`) | needs `authors` | hard |
| H1 | `raw.icite__metadata` | 41,035,229 | iCite | Iceberg `bioc.*` | `icite.metadata` (src/cdsci/lake/sources/icite) is a typed subset | release raw has 27 VARCHAR columns incl. `authors`, `cited_by`, `references`, `x_coord`; lake has 17 typed ones | hard |
| H2 | `annotation.pubtator3__mention` | 478,216,071 | PubTator3 | Iceberg `bioc.*` | — | 478M rows, sharded | hard |
| H2 | `raw.pubtator3__chemical` | 146,957,635 | PubTator3 | Iceberg `bioc.*` | — | no lake source; bulk BioCXML/TSV | hard |
| H2 | `raw.pubtator3__disease` | 168,305,967 | PubTator3 | Iceberg `bioc.*` | — | no lake source; bulk BioCXML/TSV | hard |
| H2 | `raw.pubtator3__gene` | 74,519,899 | PubTator3 | Iceberg `bioc.*` | — | no lake source; bulk BioCXML/TSV | hard |
| H2 | `raw.pubtator3__mutation` | 8,135,311 | PubTator3 | Iceberg `bioc.*` | — | no lake source; bulk BioCXML/TSV | hard |
| H2 | `raw.pubtator3__species` | 48,958,091 | PubTator3 | Iceberg `bioc.*` | — | no lake source; bulk BioCXML/TSV | hard |
| H3 | `annotation.rnacentral__rna` | 2,753,423 | RNAcentral | Iceberg `bioc.*` | — | release SELECT; `identifier_mapping` slice | hard |
| H3 | `raw.rnacentral__id_mapping` | 264,333,345 | RNAcentral | Iceberg `bioc.*` | — | 264M rows; ENA stays raw (ADR-0015 c) | hard |
| Z | `provenance.release` | 44 | provenance (product) | Iceberg `bioc.*` | `lake_ops.run` + snapshot ids | replaced at step 10 by pinned lake snapshot ids in `manifest.json` (ADR-0015); not a source | hard (step 10 design) |
