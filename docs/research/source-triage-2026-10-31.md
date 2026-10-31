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

- **Genes**: Entrez `gene_id` + `taxon_id` (`annotation.ncbi__gene`, `annotation.gene`), Ensembl gene/transcript/exon ids (`annotation.transcript`, `annotation.exon`), HGNC ids (`annotation.hgnc__gene`), MANE transcripts (`annotation.mane__transcript`), and every cross-reference in `annotation.identifier_mapping` (464M rows).
- **Literature**: `pmid` (`annotation.ncbi__gene_pubmed`, `annotation.icite__*`, `annotation.pubtator3__mention`).
- **Ontology CURIEs**: GO, MONDO, DOID, EFO, CL, UBERON, HsapDv, MmusDv in `ontology.term` / `ontology.relationship`. HP terms appear only as EFO imports.
- **Genome**: `reference.genome` (`genome_id`, `assembly_name`), and sequence names on `annotation.exon`.
- **Resources**: `resource.*` and `resource.resource_relationship` (BEDbase, ENCODE, CELLxGENE, eQTL Catalogue).

## Scoring

Each criterion is scored 0–3. Licence is a gate, not a score: only `cleared`
rows can be picks.

| Criterion | 3 | 2 | 1 | 0 |
|---|---|---|---|---|
| **Joins** | ≥2 published key families, directly | 1 family, directly | needs a mapping step | none |
| **Demand**: which Bioconductor packages or workflows need it | an OrgDb/TxDb/GenomeInfoDb column or a widely used annotation package | a named Bioc/CRAN package or common workflow | general interest | none found |
| **Bulk** | one or a few files per release | export endpoint or object store | needs a new reader | API crawl |
| **Size** (raw) | < 500 MB | 0.5–10 GB | > 10 GB or > 100M rows | — |
| **Effort** in the phase-2 path (cdsci-lake EL, then a release projection) | EL already in cdsci-lake | one new DuckDB SQL module | one module with real parsing or volume | needs a design decision, a schema (#30) or a new dependency first |

## Ranked table

Sizes are HTTP `Content-Length` or FTP/GCS listings from 2026-10-31 unless
marked *est.* Licence cells give the class, a one-line reason and the terms URL.

| # | Issue | Source | Joins | Demand | Bulk | Size score | Effort | **Total** | Licence | Terms URL | Bulk URL | Est. size |
|---|---|---|---|---|---|---|---|---|---|---|---|---|
| 1 | #54 | Reactome | 3 | 3 | 3 | 3 | 3 | **15** | **cleared**: data and derived files CC0, §1(c) | https://reactome.org/license | https://reactome.org/download/current/NCBI2Reactome_All_Levels.txt (+ ReactomePathways.txt, ReactomePathwaysRelation.txt) | ~100 MB, 788k gene-pathway rows (cdsci-lake, 2026-08-11) |
| 2 | #39 | UniProt ID mapping | 3 | 3 | 3 | 2 | 3 | **14** | **cleared**: CC BY 4.0 on all copyrightable parts | https://www.uniprot.org/help/license (read via rest.uniprot.org/help/license) | https://ftp.uniprot.org/pub/databases/uniprot/current_release/knowledgebase/idmapping/idmapping_selected.tab.gz | 7.10 GB gz |
| 3 | #41 | MGI reports | 3 | 3 | 3 | 3 | 2 | **14** | **cleared**: "MGI data and annotations are licensed under … CC-BY" 4.0 | https://www.informatics.jax.org/mgihome/other/copyright.shtml | https://www.informatics.jax.org/downloads/reports/ — MRK_List2.rpt, MGI_EntrezGene.rpt, MGI_Gene_Model_Coord.rpt | 78.8 + 88.5 + 16.2 MB |
| 4 | #45 | ClinVar | 3 | 3 | 3 | 2 | 2 | **13** | **cleared**: NCBI places no restrictions on its molecular databases; ClinVar's own use page sits behind a bot check and was **not read** | https://www.ncbi.nlm.nih.gov/home/about/policies/ | https://ftp.ncbi.nlm.nih.gov/pub/clinvar/tab_delimited/variant_summary.txt.gz (+ submission_summary; monthly `archive/`) | 456.6 MB gz (+ 404.9 MB) |
| 5 | #48 | gnomAD constraint | 3 | 2 | 3 | 3 | 2 | **13** | **cleared** (was blocked): primary data CC0; only SpliceAI annotations are CC BY-NC, and they are not in the constraint file | https://gnomad.broadinstitute.org/policies (site is JS-rendered; text read from its source, broadinstitute/gnomad-browser `browser/about/policies/terms.md`) | https://storage.googleapis.com/gcp-public-data--gnomad/release/4.1/constraint/gnomad.v4.1.constraint_metrics.tsv | 95.5 MB |
| 6 | #127 | HGNC withdrawn.txt | 3 | 2 | 3 | 3 | 2 | **13** | **cleared**: "No restrictions are imposed on access to, or use of, the data" | https://www.genenames.org/about/ | https://storage.googleapis.com/public-download-files/hgnc/tsv/tsv/withdrawn.txt | 259 KB |
| 7 | #49 | ClinGen gene-disease validity | 3 | 1 | 3 | 3 | 2 | **12** | **cleared**: curated content CC0 1.0 | https://clinicalgenome.org/docs/terms-of-use/ | https://search.clinicalgenome.org/kb/gene-validity/download | 1.1 MB, ~3.7k curations (HGNC id + MONDO id per row) |
| 8 | #50 | CIViC | 3 | 1 | 3 | 3 | 2 | **12** | **cleared**: content CC0 1.0 | https://civic.readthedocs.io/en/latest/about/faq.html | https://civicdb.org/downloads/01-Oct-2026/01-Oct-2026-ClinicalEvidenceSummaries.tsv (+ VariantSummaries, AssertionSummaries) | 4.2 MB, 4,974 evidence rows (nightly) |
| 9 | #18 | NCBI Taxonomy | 3 | 2 | 3 | 3 | 1 | **12** | **cleared**: NCBI places no restrictions on its molecular databases | https://www.ncbi.nlm.nih.gov/home/about/policies/ | https://ftp.ncbi.nlm.nih.gov/pub/taxonomy/taxdump.tar.gz (dated `taxdump_archive/`) | 80.0 MB |
| 10 | #42 | NCBI Assembly reports | 3 | 3 | 3 | 3 | 0 | **12** | **cleared**: NCBI, as above | https://www.ncbi.nlm.nih.gov/home/about/policies/ | https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/001/405/GCF_000001405.40_GRCh38.p14/GCF_000001405.40_GRCh38.p14_assembly_report.txt | 79 KB per assembly |
| 11 | #38 | GO closure (GOALL / ONTOLOGYALL) | 3 | 3 | 3 | 2 | 0 | **11** | **cleared**: CC BY 4.0 | https://geneontology.org/docs/go-citation-policy/ | none: terms and edges are already published | closure × 124.7M `ncbi__gene_go` rows if materialised |
| 12 | #47 | Open Targets Platform | 3 | 1 | 3 | 2 | 2 | **11** | **cleared**: "marked with CC0 1.0" | https://platform-docs.opentargets.org/licence | https://ftp.ebi.ac.uk/pub/databases/opentargets/platform/26.09/output/ — association_overall_direct/, association_by_datatype_direct/, target/, disease/ | 814 + 837 + 41 + 9 MB Parquet |
| 13 | #24 | AnnotationHub / ExperimentHub catalogs | 2 | 3 | 3 | 3 | 0 | **11** | **unknown**: no data licence on the metadata DBs; the package is Artistic-2.0, which covers code | https://bioconductor.org/packages/release/bioc/html/AnnotationHub.html | https://annotationhub.bioconductor.org/metadata/annotationhub.sqlite3, https://experimenthub.bioconductor.org/metadata/experimenthub.sqlite3 | 137.3 MB + 10.4 MB (9,736 EH resources) |
| 14 | #43 | GENCODE GTF | 3 | 2 | 3 | 3 | 0 | **11** | **cleared**: "All the GENCODE project data is open access"; EMBL-EBI adds no restriction beyond the data owner's | https://www.gencodegenes.org/pages/data_access.html, https://www.ebi.ac.uk/about/terms-of-use/ | https://ftp.ebi.ac.uk/pub/databases/gencode/Gencode_human/latest_release/gencode.v50.annotation.gtf.gz | 119 MB gz (human v50) |
| 15 | #44 | RefSeq genomic GFF3 | 2 | 3 | 3 | 3 | 0 | **11** | **cleared**: NCBI, as above | https://www.ncbi.nlm.nih.gov/home/about/policies/ | https://ftp.ncbi.nlm.nih.gov/genomes/all/GCF/000/001/405/GCF_000001405.40_GRCh38.p14/GCF_000001405.40_GRCh38.p14_genomic.gff.gz | 78.2 MB gz (human) |
| 16 | #159 | ENCODE Annotation datasets | 3 | 1 | 2 | 2 | 2 | **10** | **cleared**: "freely download, analyze and publish results based on any ENCODE data without restrictions" | https://www.encodeproject.org/help/citing-encode/ | the `report.tsv?type=Annotation` export the ENCODE lander already uses | 620,734 objects (per #159; not re-counted) |
| 17 | #157 | Sidecar files (Cellosaurus, GWAS, eQTL, PubTator3 relations) | 3 | 1 | 3 | 1 | 2 | **10** | **cleared** per parent source (Cellosaurus CC BY 4.0: https://www.cellosaurus.org/description.html; the others inherit their landed source's terms, **not re-read**) | per parent source | per item | PubTator3 `relation2pubtator3` 284M rows (per #157) |
| 18 | #140 | GEO / SRA / BioSample | 2 | 3 | 2 | 1 | 1 | **9** | **cleared**: NCBI, as above (submitter metadata; NCBI adds no restriction) | https://www.ncbi.nlm.nih.gov/home/about/policies/ | via cdsci-lake/omicidx (**not verified** in cdsci-lake `sources/` today) | largest proposed (per #140; not measured) |
| 19 | #85 | CELLxGENE Census cells | 3 | 2 | 2 | 1 | 0 | **8** | **cleared** as for #84 (CC BY 4.0); Census licence page **not re-read** today | https://chanzuckerberg.github.io/cellxgene-census/cellxgene_census_docsite_data_release_info.html | `s3://cellxgene-census-public-us-west-2/cell-census/<build>/soma/` (needs `tiledbsoma`) | ~100M primary cells (per #85) |
| 20 | #138 | 4DN Data Portal | 1 | 1 | 1 | 3 | 0 | **6** | **cleared for metadata** per the 2026-09-18 reading in #138 (4dn-policies, "freely download, analyze, and publish"); **not re-read**; file downloads need authentication | https://github.com/4dn-dcic/4dn-policies | https://data.4dnucleome.org metadata export | small (metadata only) |
| — | #52 | HPO + phenotype.hpoa | 3 | 3 | 3 | 3 | 2 | gated | **unknown**: the custom licence grants download and use, not redistribution, and forbids altering "logical relationships"; `hpo.jax.org/license` now 404s, and `hp.obo` (2026-09-01) still points at https://hpo.jax.org/app/license | https://hpo.jax.org/app/license | https://purl.obolibrary.org/obo/hp/hpoa/phenotype.hpoa | 35.8 MB |
| — | #136 | DepMap | 2 | 2 | 2 | 2 | 2 | gated | **unknown**: the portal terms are behind a bot check (unread again today); no full figshare release after 24Q4 found on 2026-09-18 | https://depmap.org/portal/terms/ | portal downloads section (not reachable by script) | not measured |

#38, #42, #43, #44 and #24 score well but carry an open decision or blocker,
so their effort is 0.

## Picks

1. **#54 Reactome**: the cheapest high-value source in the backlog. cdsci-lake
   already lands it (`lake.reactome.{gene_pathway,pathway,pathway_relation}`),
   so only the release projection is new. Every row joins on Entrez `gene_id`.
   It is the open answer to OrgDb's PATH column, which SPEC D.2 drops because
   KEGG cannot be redistributed. Demand: `reactome.db`, `ReactomePA`,
   `graphite`, `clusterProfiler`. Its "blocked on #114" status is stale: #114
   closed 2026-10-02. Reactome gives species as names, not taxon ids. The
   projection needs the 16-name map (or #18) for the `taxon_id` join check,
   the same problem WikiPathways has.
2. **#39 UniProt ID mapping**: also landed in cdsci-lake, with a transform
   model already shaped for `annotation.identifier_mapping`
   (`transform/models/uniprot/identifier_mapping.sql`, ENTREZ→UNIPROT per
   taxon). It serves the OrgDb UNIPROT column that SPEC D.1 promises and that
   today rests on #153's 34,223 hand-pushed rows. Demand: every `org.*.eg.db`
   user, `UniProt.ws`. The #114 block is stale here too. Size is the 7.1 GB
   raw file, already paid for in the lake.
3. **#41 MGI**: mouse nomenclature and the `org.Mm.eg.db` MGI column, which
   SPEC's parity goal covers. `MGI_Gene_Model_Coord.rpt` carries MGI id,
   Entrez id and Ensembl gene id on one row, so the join check is direct.
   Three TSVs, about 180 MB, CC BY 4.0. Needs a new cdsci-lake EL module, but
   only a small one.
4. **#45 ClinVar**: SPEC's "Selected for the first release" names it, and no
   Bioconductor package ships it. `variant_summary` joins on Entrez `GeneID`,
   `HGNC_ID`, MONDO/OMIM ids in `PhenotypeIDS` and `Assembly`
   (GRCh37/GRCh38). It is the biggest pick (457 MB gz). Pin the monthly
   archive, as SPEC requires. Parsing note: MONDO ids arrive double-prefixed
   (`MONDO:MONDO:0013342`).

**Runner-up: #48 gnomAD constraint.** It ties ClinVar on score and has the
better licence-to-effort ratio, but SPEC.md lists gnomAD under "Ruled out" for
ODbL. That reason is now factually wrong (see below). Promoting it means
amending SPEC's ruled-out entry in the same PR that adds it, and SPEC asks for
that to be "revisited deliberately", so it goes first in the next tranche
rather than into this one. Land only the constraint TSV. SpliceAI is CC BY-NC
and must stay out.

**Fold into migrations, not new picks**: #127 (HGNC `withdrawn.txt`) should
ride along with HGNC's step-9 migration, #159 with ENCODE's, and the #157
sidecars with their parent sources. Each is a second file of a source already
being moved, so a separate pick would duplicate the workflow run.

**Cheap next tranche**: #49 ClinGen and #50 CIViC. Both are CC0, a few MB, and
join HGNC/Entrez to MONDO/DOID. Bioconductor demand is thin, which is the only
reason they rank below the picks.

## Drops (`license:blocked`)

**None.** The only `license:blocked` issue, #48 gnomAD, is unblocked by today's
reading. gnomAD's terms say "The primary data from the gnomAD exomes and
genomes are available free of restrictions under the Creative Commons Zero
Public Domain Dedication". The ODbL finding from the closed #27 describes an
earlier licence. The share-alike question #48 was waiting on no longer applies.

These are excluded from the picks but not dropped, because their licence is
`unknown`, not blocked:

- **#52 HPO**: the grant is download-and-use only, and it forbids altering
  logical relationships, which normalising into `ontology.relationship`
  arguably does. Unchanged since 2026-09-18. Upstream (pnrobinson,
  obophenotype#7252) defended the custom licence as deliberate. The terms page
  moved and now 404s at `hpo.jax.org/license`.
- **#136 DepMap**: the terms are still unreadable by script.
- **#24 AnnotationHub/ExperimentHub**: no licence is stated for the catalog
  metadata. It is Bioconductor's own data, so one question to the AnnotationHub
  maintainers settles it. The issue is also blocked on #30.

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
| #48 gnomAD constraint | license:blocked → **license:cleared** | priority:medium | status:needs-decision stays until SPEC's "Ruled out" entry is amended |
| #127 HGNC withdrawn | license:cleared (no change) | priority:medium | — (fold into HGNC migration) |
| #49 ClinGen | license:cleared (no change) | priority:medium | — |
| #50 CIViC | license:cleared (no change) | priority:medium | — |
| #18 NCBI Taxonomy | license:cleared (no change) | priority:medium | — |
| #42 Assembly reports | license:cleared (no change) | priority:medium | — (still on #94) |
| #38 GO closure | license:cleared (no change) | priority:medium | — (still needs-decision) |
| #159 ENCODE Annotations | license:cleared (no change) | priority:medium | — (fold into ENCODE migration) |
| #157 sidecars | add license:cleared | priority:low | add data-source |
| #47 Open Targets | license:cleared (no change) | priority:low | — |
| #43 GENCODE | license:unknown → **license:cleared** | priority:low | — (still needs-decision) |
| #44 RefSeq GFF3 | license:cleared (no change) | priority:low | — |
| #24 AnnotationHub/ExperimentHub | license:cleared → **license:unknown** | priority:low | — |
| #140 GEO/SRA/BioSample | add license:cleared | priority:low | add data-source; status:blocked → status:ready (#114 closed) |
| #85 CELLxGENE Census cells | add license:cleared | priority:low | add data-source |
| #138 4DN | license:cleared (no change) | priority:low | — |
| #52 HPO | license:unknown (no change) | priority:low | — |
| #136 DepMap | license:unknown (no change) | priority:low | — |
