# Source backlog triage, 2026-10-31

**Status**: research note for #183 step 6, not a decision. It ranks the open
source backlog (#55 registry, every open `data-source` issue, plus #85, #140 and
#157, which carry no `data-source` label) and proposes the 3–4 sources to add in
phase 2. Licences, bulk URLs and sizes were fetched from the upstream sites on
2026-10-31 unless a cell says otherwise. Published keys come from
`https://data.bioconice.cancerdatasci.org/latest/manifest.json` (release
2026-10-31, 80 tables).

## What a candidate can join to

The release publishes these key families:

- **Genes**: Entrez `gene_id` + `taxon_id` (`annotation.ncbi__gene`), Ensembl gene ids (`annotation.gene`), Ensembl transcript/exon ids (`annotation.transcript`, `annotation.exon`), HGNC ids (`annotation.hgnc__gene`), MANE transcripts (`annotation.mane__transcript`), and every cross-reference in `annotation.identifier_mapping` (464M rows).
- **Literature**: `pmid` (`annotation.ncbi__gene_pubmed`, `annotation.icite__*`, `annotation.pubtator3__mention`).
- **Ontology CURIEs**: GO, MONDO, DOID, EFO, CL, UBERON, HsapDv, MmusDv in `ontology.term` / `ontology.relationship`. HP terms appear only as EFO imports.
- **Genome**: `reference.genome` (`genome_id`, `assembly_name`), and sequence names on `annotation.exon`.
- **Resources**: `resource.*` and `resource.resource_relationship` (BEDbase, ENCODE, CELLxGENE, eQTL Catalogue).

## Scoring

Each criterion is scored 0–3. Licence is a gate, not a score: a source is
`cleared` only when an owner-issued term that permits redistribution of the
file used is cited; otherwise it is `unknown`, with what is missing. Only
`cleared` rows can be picks.

| Criterion | 3 | 2 | 1 | 0 |
|---|---|---|---|---|
| **Joins** | ≥2 published key families, directly | 1 family, directly | needs a mapping step | none |
| **Demand**: which Bioconductor packages or workflows need it | an OrgDb/TxDb/GenomeInfoDb column or a widely used annotation package | a named Bioc/CRAN package or common workflow | general interest | none found |
| **Bulk** | one or a few files per release | export endpoint or object store | needs a new reader | API crawl |
| **Size** (raw) | < 500 MB | 0.5–10 GB | > 10 GB or > 100M rows | — |
| **Effort** in the phase-2 path (cdsci-lake EL, then a release projection) | EL already in cdsci-lake | one new DuckDB SQL module | one module with real parsing or volume | needs a design decision, a schema (#30) or a new dependency first |

A family is joined **directly** when rows carry its key verbatim or after a
trivial reformat (dropping an Ensembl version suffix, a doubled `MONDO:`
prefix, `EFO_` → `EFO:`). Renaming chromosomes (`chr1` → `1`) or looking a
name up (species name → `taxon_id`) is a mapping step. `taxon_id` is part of
the Genes and Genome keys, not a family of its own. Ties in the total are
broken by Effort, then Demand, then issue number.

## Ranked table

Sizes are HTTP `Content-Length` or FTP/GCS listings from 2026-10-31 unless
marked *est.* Join scores were checked against each file's header on
2026-10-31, except #140, #85, #138, #24, #136 and #52, which keep their
2026-09-18 reading. Licence cells give the class, the quoted term (or what is
missing) and the terms URL.

| # | Issue | Source | Joins | Demand | Bulk | Size score | Effort | **Total** | Licence | Terms URL | Bulk URL | Est. size |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | #54 | Reactome | 2 | 3 | 3 | 3 | 3 | **14** | **cleared**: data and derived files CC0, §1(c) | https://reactome.org/license | https://reactome.org/download/current/NCBI2Reactome_All_Levels.txt (+ ReactomePathways.txt, ReactomePathwaysRelation.txt) | ~100 MB, 788k gene-pathway rows (cdsci-lake, 2026-08-11) |
| 2 | #41 | MGI reports | 3 | 3 | 3 | 3 | 2 | **14** | **cleared**: "MGI data and annotations are licensed under … CC-BY" 4.0 | https://www.informatics.jax.org/mgihome/other/copyright.shtml | https://www.informatics.jax.org/downloads/reports/ — MRK_List2.rpt, MGI_EntrezGene.rpt, MGI_Gene_Model_Coord.rpt | 78.8 + 88.5 + 16.2 MB |
| 3 | #39 | UniProt ID mapping | 3 | 3 | 3 | 1 | 3 | **13** | **cleared**: CC BY 4.0 on all copyrightable parts | https://www.uniprot.org/help/license (read via rest.uniprot.org/help/license) | https://ftp.uniprot.org/pub/databases/uniprot/current_release/knowledgebase/idmapping/idmapping_selected.tab.gz | 7.10 GB gz; one row per UniProtKB entry, 150,006,383 in release 2026_03 (`relnotes.txt`) |
| 4 | #45 | ClinVar | 3 | 2 | 3 | 2 | 2 | **12** | **cleared**: "NCBI itself places no restrictions on the use or distribution of the data contained therein", for molecular databases including "molecular variation"; ClinVar's own use page sits behind a bot check and was **not read** | https://www.ncbi.nlm.nih.gov/home/about/policies/ | https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/variant_summary.txt.gz (+ submission_summary; monthly `archive/`) | 456.6 MB gz (+ 404.9 MB) |
| 5 | #127 | HGNC withdrawn.txt | 2 | 2 | 3 | 3 | 2 | **12** | **cleared**: "No restrictions are imposed on access to, or use of, the data provided by the HGNC" | https://www.genenames.org/about/ | https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/withdrawn.txt | 259 KB |
| 6 | #49 | ClinGen gene-disease validity | 3 | 1 | 3 | 3 | 2 | **12** | **cleared**: curated content CC0 1.0 | https://clinicalgenome.org/docs/terms-of-use/ | https://search.clinicalgenome.org/kb/gene-validity/download | 1.1 MB, ~3.7k curations (HGNC id + MONDO id per row) |
| 7 | #50 | CIViC | 3 | 1 | 3 | 3 | 2 | **12** | **cleared**: content CC0 1.0 | https://civic.readthedocs.io/en/latest/about/faq.html | https://civicdb.org/downloads/01-Oct-2026/01-Oct-2026-ClinicalEvidenceSummaries.tsv (+ VariantSummaries, AssertionSummaries) | 4.2 MB, 4,974 evidence rows (nightly) |
| 8 | #47 | Open Targets Platform | 3 | 1 | 3 | 2 | 2 | **11** | **cleared**: "marked with CC0 1.0" | https://platform-docs.opentargets.org/licence | https://ftp.ebi.ac.uk/pub/databases/opentargets/platform/26.09/output/ — association_overall_direct/, association_by_datatype_direct/, target/, disease/ | 814 + 837 + 41 + 9 MB Parquet |
| 9 | #18 | NCBI Taxonomy | 2 | 2 | 3 | 3 | 1 | **11** | **cleared**: NCBI, as for #45 | https://www.ncbi.nlm.nih.gov/home/about/policies/ | https://ftp.ncbi.nlm.nih.gov/pub/taxonomy/taxdump.tar.gz (dated `taxdump_archive/`) | 80.0 MB |
| 10 | #42 | NCBI Assembly reports | 2 | 3 | 3 | 3 | 0 | **11** | **cleared**: NCBI, as for #45 | https://www.ncbi.nlm.nih.gov/home/about/policies/ | https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/001/405/GCF_000001405.40_GRCh38.p14/GCF_000001405.40_GRCh38.p14_assembly_report.txt | 79 KB per assembly |
| 11 | #44 | RefSeq genomic GFF3 | 2 | 3 | 3 | 3 | 0 | **11** | **cleared**: NCBI, as for #45 | https://www.ncbi.nlm.nih.gov/home/about/policies/ | https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/001/405/GCF_000001405.40_GRCh38.p14/GCF_000001405.40_GRCh38.p14_genomic.gff.gz | 78.2 MB gz (human) |
| 12 | #38 | GO closure (GOALL / ONTOLOGYALL) | 3 | 3 | 3 | 1 | 0 | **10** | **cleared**: CC BY 4.0 | https://geneontology.org/docs/go-citation-policy/ | none: terms and edges are already published | closure × 124.7M `ncbi__gene_go` rows if materialised |
| 13 | #140 | GEO / SRA / BioSample | 2 | 3 | 2 | 1 | 1 | **9** | **cleared**: NCBI, as for #45 (submitter metadata; NCBI adds no restriction) | https://www.ncbi.nlm.nih.gov/home/about/policies/ | via cdsci-lake/omicidx (**not verified** in cdsci-lake `sources/` today) | largest proposed (per #140; not measured) |
| 14 | #85 | CELLxGENE Census cells | 3 | 2 | 2 | 1 | 0 | **8** | **cleared**: Discover data is published "subject to a CC-BY 4.0 license, meaning they can download, share, and use the data"; the Census pages themselves state no licence | https://cellxgene.cziscience.com/docs/032__Contribute%20and%20Publish%20Data | `s3://cellxgene-census-public-us-west-2/cell-census/<build>/soma/` (needs `tiledbsoma`) | ~100M primary cells (per #85) |
| — | #52 | HPO + phenotype.hpoa | 3 | 3 | 3 | 3 | 2 | gated (14) | **unknown**: the custom licence grants download and use, not redistribution, and forbids altering "logical relationships"; `hpo.jax.org/license` now 404s, and `hp.obo` (2026-09-01) still points at https://hpo.jax.org/app/license | https://hpo.jax.org/app/license | https://purl.obolibrary.org/obo/hp/hpoa/phenotype.hpoa | 35.8 MB |
| — | #48 | gnomAD constraint | 2 | 2 | 3 | 3 | 2 | gated (12) | **unknown** (CC0 provisional): the terms dedicate "the primary data from the gnomAD exomes and genomes" to CC0 and warn that "some annotations may have restrictions on usage"; no term names the constraint metrics. Missing: an owner statement that CC0 covers the constraint files | https://gnomad.broadinstitute.org/policies (site is JS-rendered; text read from its source, broadinstitute/gnomad-browser `browser/about/policies/terms.md`) | https://storage.googleapis.com/gcp-public-data--gnomad/release/4.1/constraint/gnomad.v4.1.constraint_metrics.tsv | 95.5 MB |
| — | #24 | AnnotationHub / ExperimentHub catalogs | 2 | 3 | 3 | 3 | 0 | gated (11) | **unknown**: no data licence on the metadata DBs; the package is Artistic-2.0, which covers code | https://bioconductor.org/packages/release/bioc/html/AnnotationHub.html | https://annotationhub.bioconductor.org/metadata/annotationhub.sqlite3, https://experimenthub.bioconductor.org/metadata/experimenthub.sqlite3 | 137.3 MB + 10.4 MB (9,736 EH resources) |
| — | #136 | DepMap | 2 | 2 | 2 | 2 | 2 | gated (10) | **unknown**: the portal terms are behind a bot check (unread again today); no full figshare release after 24Q4 found on 2026-09-18 | https://depmap.org/portal/terms/ | portal downloads section (not reachable by script) | not measured |
| — | #157 | Sidecar files (Cellosaurus, GWAS, eQTL, PubTator3 relations) | 3 | 1 | 3 | 1 | 2 | gated (10) | **unknown** as a set: Cellosaurus is cleared ("We have chosen to apply to Cellosaurus the … CC BY 4.0", https://www.cellosaurus.org/description.html); the GWAS Catalog files rest only on the EMBL-EBI terms of use, the gap #43 has; eQTL Catalogue and PubTator3 terms **not re-read** | per parent source | per item | PubTator3 `relation2pubtator3` 284M rows (per #157) |
| — | #159 | ENCODE Annotation datasets | 3 | 1 | 2 | 2 | 2 | gated (10) | **unknown**: users "may freely download, analyze and publish results based on any ENCODE data without restrictions"; that permits publishing results, not redistributing the data. The ENCODE source already landed needs the same check | https://www.encodeproject.org/help/citing-encode/ | the `report.tsv?type=Annotation` export the ENCODE lander already uses | 620,734 objects (per #159; not re-counted) |
| — | #43 | GENCODE GTF | 2 | 2 | 3 | 3 | 0 | gated (10) | **unknown**: "All the GENCODE project data is open access" grants access, not redistribution; EMBL-EBI "places no additional restrictions on the use or redistribution of the data … other than those provided by the original data owners", which defers to an owner term that was not found on the GENCODE site or in the FTP `_README.TXT` | https://www.gencodegenes.org/pages/data_access.html, https://www.ebi.ac.uk/about/terms-of-use/ | https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/latest_release/gencode.v50.annotation.gtf.gz | 119 MB gz (human v50) |
| — | #138 | 4DN Data Portal | 1 | 1 | 1 | 3 | 0 | gated (6) | **unknown**: "External data users may freely download, analyze, and publish results based on public 4DN datasets without restriction" (`4dn-data-release-and-use-policy.md`, re-read today) permits publishing results, not redistribution; file downloads need authentication | https://github.com/4dn-dcic/4dn-policies | https://data.4dnucleome.org metadata export | small (metadata only) |

#38, #42, #43, #44 and #24 carry an open decision or blocker, so their effort
is 0.

What the join scores rest on: Reactome's gene file carries Entrez ids, but its
pathways (`R-HSA-…`) are not a published family and its species are names, so
only Genes is direct. MGI_Gene_Model_Coord.rpt carries Entrez and Ensembl gene
ids with `GRCm39` (an `assembly_name`) and Ensembl-style chromosome names, so
Genes and Genome are both direct. The gnomAD v4.1 constraint file keys on
gene (Ensembl or Entrez `gene_id`, plus transcript) only. HGNC
`withdrawn.txt` carries HGNC ids only. Taxonomy offers `taxon_id` alone. An
assembly report carries the GenBank accession (`genome_id`) and sequence names
only. GENCODE's Ensembl ids are direct, but its `chr1`-style names need
renaming to match `annotation.exon`. HGNC's grant counts as cleared because it
places no restriction on use at all; ENCODE's and 4DN's grants list the
permitted uses, and redistribution is not one of them.

## Picks

1. **#54 Reactome**: the cheapest high-value source in the backlog. cdsci-lake
   already lands it (`lake.reactome.{gene_pathway,pathway,pathway_relation}`),
   so only the release projection is new. Rows join on Entrez `gene_id`; the
   pathways are Reactome's own ids. It is the open answer to OrgDb's PATH
   column, which SPEC D.2 drops because KEGG cannot be redistributed. Demand:
   `reactome.db`, `ReactomePA`, `graphite`, `clusterProfiler`. Its "blocked
   on #114" status is stale: #114 closed 2026-10-02. Reactome gives species
   as names, not taxon ids. The projection needs the 16-name map (or #18) for
   the `taxon_id` join check, the same problem WikiPathways has. It ties MGI
   on 14 and ranks first on effort.
2. **#41 MGI**: mouse nomenclature and the `org.Mm.eg.db` MGI column, which
   SPEC's parity goal covers. `MGI_Gene_Model_Coord.rpt` carries MGI id,
   Entrez id, Ensembl gene id and the `GRCm39` build on one row, so the join
   check is direct. Three TSVs, about 180 MB, CC BY 4.0. Needs a new
   cdsci-lake EL module, but only a small one.
3. **#39 UniProt ID mapping**: also landed in cdsci-lake, with a transform
   model already shaped for `annotation.identifier_mapping`
   (`transform/models/uniprot/identifier_mapping.sql`, ENTREZ→UNIPROT per
   taxon). It serves the OrgDb UNIPROT column that SPEC D.1 promises and that
   today rests on #153's 34,223 hand-pushed rows. Demand: every `org.*.eg.db`
   user, `UniProt.ws`. The #114 block is stale here too. The raw file is
   150M rows (size score 1), but the lake already pays for it, which is why
   its effort is 3.
4. **#45 ClinVar**: SPEC's "Selected for the first release" names it. No
   Bioconductor package ships it, so Demand is 2 (filtering variant calls by
   clinical significance is a common workflow), not 3, and its total is 12.
   That ties #127, #49 and #50; ClinVar keeps fourth place on the tie-break
   (Demand over ClinGen and CIViC, issue number over #127, which folds into
   the HGNC migration anyway). `variant_summary` joins on Entrez `GeneID`,
   `HGNC_ID`, MONDO/OMIM ids in `PhenotypeIDS` and `Assembly`
   (GRCh37/GRCh38). At 457 MB gz (862 MB with `submission_summary`) it is
   the largest pick not already landed in cdsci-lake; UniProt's 7.10 GB file
   is larger. Pin the monthly archive, as SPEC requires. Parsing note: MONDO ids arrive double-prefixed
   (`MONDO:MONDO:0013342`).

**Not a runner-up: #48 gnomAD constraint.** It scores 12, but its licence is
`unknown`, so it is gated. The CC0 reading is provisional. gnomAD's terms
have dedicated "the data released by gnomAD" to CC0 since at least the
2021-02-26 version in broadinstitute/gnomad-browser; on 2023-08-31 (#1171)
that narrowed to "the primary data from the gnomAD exomes and genomes", with
a warning that "some annotations may have restrictions on usage". None of
the seven versions read mentions ODbL, and the closed #27 cites no source for
its ODbL finding. But no version names the constraint metrics either, and they are
derived data, not primary data. SPEC.md's "Ruled out" entry stays as written;
this note does not propose amending it. What would clear it: a statement from
the gnomAD team (gnomad@broadinstitute.org) that the CC0 dedication covers the
constraint files. SpliceAI is CC BY-NC 4.0 and stays out either way.

**Fold into migrations, not new picks**: #127 (HGNC `withdrawn.txt`) should
ride along with HGNC's step-9 migration, #159 with ENCODE's, and the #157
sidecars with their parent sources. Each is a second file of a source already
being moved, so a separate pick would duplicate the workflow run. #159 and the
non-Cellosaurus #157 files need a redistribution term cited first.

**Cheap next tranche**: #49 ClinGen and #50 CIViC. Both are CC0, a few MB, and
join HGNC/Entrez to MONDO/DOID. Bioconductor demand is thin, which is the only
reason they rank below the picks.

## Drops (`license:blocked`)

**None.** The only `license:blocked` issue, #48 gnomAD, moves to `unknown`:
the ODbL basis for the block has no citation, and the CC0 terms do not name
the constraint file (see above).

These are excluded from the picks but not dropped, because their licence is
`unknown`, not blocked:

- **#52 HPO**: the grant is download-and-use only, and it forbids altering
  logical relationships, which normalising into `ontology.relationship`
  arguably does. Unchanged since 2026-09-18. Upstream (pnrobinson,
  obophenotype#7252) defended the custom licence as deliberate. The terms page
  moved and now 404s at `hpo.jax.org/license`.
- **#48 gnomAD constraint**: CC0 provisional; needs the gnomAD team to confirm
  that it covers the constraint files.
- **#136 DepMap**: the terms are still unreadable by script.
- **#24 AnnotationHub/ExperimentHub**: no licence is stated for the catalog
  metadata. It is Bioconductor's own data, so one question to the AnnotationHub
  maintainers settles it. The issue is also blocked on #30.
- **#43 GENCODE**: "open access" and the EMBL-EBI terms defer to an owner
  redistribution term that was not found.
- **#159 ENCODE Annotations** and **#138 4DN**: both grants permit publishing
  results, not redistribution.
- **#157 sidecars**: cleared for the Cellosaurus files only.

The #55 "Rejected on licence" list (KEGG, OMIM, STRING, MSigDB, DrugBank,
COSMIC, DisGeNET, dbSNP deferred) was **not re-read** today. None of those has
an open issue.

## Proposed labels

The orchestrator applies these; nothing was changed on GitHub. `priority:*`
labels **do not exist yet** in the repo and must be created first.

| Issue | Licence label | Priority | Other label changes |
|---|---|---|---|
| #54 Reactome | license:cleared (no change) | priority:high | status:blocked → status:ready (#114 closed) |
| #39 UniProt | license:cleared (no change) | priority:high | status:blocked → status:ready (#114 closed) |
| #41 MGI | license:cleared (no change) | priority:high | — |
| #45 ClinVar | license:cleared (no change) | priority:high | — |
| #48 gnomAD constraint | license:blocked → **license:unknown** (CC0 provisional) | priority:medium | — (status:needs-decision stays) |
| #127 HGNC withdrawn | license:cleared (no change) | priority:medium | — (fold into HGNC migration) |
| #49 ClinGen | license:cleared (no change) | priority:medium | — |
| #50 CIViC | license:cleared (no change) | priority:medium | — |
| #18 NCBI Taxonomy | license:cleared (no change) | priority:medium | — |
| #42 Assembly reports | license:cleared (no change) | priority:medium | — (still on #94) |
| #38 GO closure | license:cleared (no change) | priority:medium | — (still needs-decision) |
| #159 ENCODE Annotations | license:cleared → **license:unknown** | priority:medium | — (fold into ENCODE migration) |
| #157 sidecars | add **license:unknown** (Cellosaurus files cleared) | priority:low | add data-source |
| #47 Open Targets | license:cleared (no change) | priority:low | — |
| #43 GENCODE | license:unknown (no change) | priority:low | — (still needs-decision) |
| #44 RefSeq GFF3 | license:cleared (no change) | priority:low | — |
| #24 AnnotationHub/ExperimentHub | license:cleared → **license:unknown** | priority:low | — |
| #140 GEO/SRA/BioSample | add license:cleared | priority:low | add data-source; status:blocked → status:ready (#114 closed) |
| #85 CELLxGENE Census cells | add license:cleared | priority:low | add data-source |
| #138 4DN | license:cleared → **license:unknown** | priority:low | — |
| #52 HPO | license:unknown (no change) | priority:low | — |
| #136 DepMap | license:unknown (no change) | priority:low | — |
