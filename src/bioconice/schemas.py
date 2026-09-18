"""Declared Iceberg schemas — the single source of table structure and meaning.

Tables are never created from an inferred Arrow schema. Two things required by
SPEC.md cannot be expressed that way: identifier fields, which are the merge
key, and per-column `doc`, which is what makes the catalog self-describing.

A column exists here only once something populates it. Columns whose source has
not landed yet (gene descriptions, assembly checksums) are added by schema
evolution when it does, rather than shipped as permanent NULLs that read as
"we have this" when we do not.
"""

import time
from dataclasses import dataclass, field

from pyiceberg.exceptions import RESTError
from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import (
    BooleanType, DoubleType, IntegerType, ListType, LongType, NestedField, StringType,
)

MULTI_VALUED = (
    "Ontology term ids of every value the listing gives, sorted, as a list — a convenience "
    "for list_contains() filters. The joinable form is resource.resource_relationship, one "
    "row per (dataset version, relationship, term)."
)

VALID_FROM = (
    "The biocOnIce release from which this version of the record is valid. "
    "A row is one *version*: any change to any attribute closes the previous "
    "row and opens a new one, so the value here is not necessarily when the "
    "record first existed. It is when biocOnIce first carried this version, not "
    "when the source created it — the 51,794 taxa added in 2026.09 existed at "
    "NCBI for years."
)
VALID_TO = (
    "The biocOnIce release at which this version stopped being current, "
    "exclusive. NULL means this is the current version — it does not mean "
    "unknown. Queries wanting current data must filter on valid_to IS NULL; "
    "queries wanting release R want "
    "valid_from <= R AND (valid_to IS NULL OR valid_to > R). Like valid_from, "
    "this is a biocOnIce release, not the date the source changed the record."
)
TAXON = "NCBI taxonomy id of the organism, e.g. 9606 for human. Part of the merge key."
SOURCE = (
    "The asserting annotation provider, from the controlled writer vocabulary: "
    "'ENSEMBL' now; 'REFSEQ', 'GENCODE' when they land. Part of the business key "
    "and of every writer's merge scope, so providers stack in one table and none "
    "can retire another's rows (ADR-0004)."
)
COORD = "1-based-inclusive"
ALPHA_DIVERSITY = (
    "Direction this alpha-diversity metric moved in group 1: 'increased', 'decreased' "
    "or 'unchanged'. NOT a diversity value — there is no number here to plot. NULL "
    "where the study did not report the metric, which is the majority."
)
# CC BY 4.0 asks for credit, a licence link and an indication of changes; each
# Cellosaurus table comment ends with this plus what was changed. The release
# number is per load, so the comment points at the manifest that records it.
_CELLOSAURUS_CREDIT = (
    "Cellosaurus (release: provenance.release.source_version where source = 'cellosaurus') "
    "© CALIPHO group, SIB Swiss Institute of Bioinformatics, CC BY 4.0 "
    "(https://creativecommons.org/licenses/by/4.0/)"
)


@dataclass(frozen=True)
class TableDef:
    """A declared table.

    `business_key` is what identifies a *record* — what a merge joins on to
    decide whether a row is new, changed or retired. The Iceberg identifier
    fields are the *row* key, which is the business key plus `valid_from`,
    because every change opens a new version row. Declaring only the business
    key to Iceberg would
    assert a uniqueness this model does not have; deriving one from the other
    keeps them from drifting.
    """

    schema: Schema
    comment: str
    business_key: tuple = ()
    # Identity-partition columns, for pruning only: merge-scope containment, not
    # partitioning, is the correctness mechanism (ADR-0004).
    partition_by: tuple = ()
    properties: dict = field(default_factory=dict)

    def iceberg_schema(self):
        if not self.business_key:
            return self.schema
        # A versioned table's row key is the business key plus valid_from, since
        # each change opens a new version. A table with no validity columns —
        # the manifest — is keyed by its business key alone.
        names = list(self.business_key)
        if any(f.name == "valid_from" for f in self.schema.fields):
            names.append("valid_from")
        ids = [self.schema.find_field(n).field_id for n in names]
        return Schema(*self.schema.fields, identifier_field_ids=ids)


NAMESPACES = {
    "provenance": "What each biocOnIce release was built from, and how we know.",
    "raw": "Source files landed verbatim, before any interpretation. Read these to "
           "re-derive or audit; query the annotation and reference namespaces instead.",
    "reference": "Genome assemblies and the sequences that make them up.",
    "annotation": "Gene, transcript and exon structure, and identifier cross-references.",
    "resource": "Universal catalog entries for large or external data (matrices, images, "
                "assemblies-as-files) that biocOnIce references by URI rather than ingests.",
    "ontology": "Terms and relationships from external OBO ontologies (CL, UBERON, MONDO, EFO, "
                "HsapDv, MmusDv, GO): cell types, anatomy, disease, experimental factors, "
                "developmental stage.",
    "clinical": "Curated links between human genetic variation and traits or disease.",
}

# (short name, licence) for every OBO ontology this catalog lands, kept here rather than
# imported from obo.py (which needs schemas.TableDef) to avoid a circular import. obo.REGISTRY
# is the same list plus each ontology's release URL; test_obo.py asserts the two stay in sync.
_OBO_ONTOLOGIES = (
    ("cl", "CC-BY-4.0"), ("uberon", "CC-BY-3.0"), ("mondo", "CC-BY-4.0"),
    ("efo", "Apache-2.0"), ("hsapdv", "CC-BY-4.0"), ("mmusdv", "CC-BY-4.0"), ("go", "CC-BY-4.0"),
    ("doid", "CC0-1.0"),
)


def _obo_raw(name, licence):
    """raw.obo__<name>: one row per node, one row per edge, of that ontology's OBO Graphs JSON."""
    return TableDef(
        schema=Schema(
            NestedField(1, "kind", StringType(), required=True,
                        doc="'node' or 'edge': which half of the OBO Graphs JSON this row came from."),
            NestedField(2, "id", StringType(), doc="Node IRI. NULL on an edge row."),
            NestedField(3, "lbl", StringType(),
                        doc="Node label (rdfs:label). NULL on an edge row, and on the node rows "
                            "the file itself leaves unlabeled (imported classes mostly)."),
            NestedField(4, "sub", StringType(), doc="Edge subject IRI. NULL on a node row."),
            NestedField(5, "pred", StringType(),
                        doc="Edge predicate: 'is_a' verbatim (the only relation OBO Graphs JSON "
                            "ever states unqualified), otherwise the relation's own IRI. NULL on "
                            "a node row."),
            NestedField(6, "obj", StringType(), doc="Edge object IRI. NULL on a node row."),
            NestedField(7, "meta", StringType(),
                        doc="The node's or edge's 'meta' object — definition, synonyms, xrefs, "
                            "basicPropertyValues, deprecated flag — serialized back to JSON text "
                            "verbatim. Kept whole and unparsed, like the GTF attribute column: "
                            "interpreting it is a transform concern. NULL where the file carries "
                            "no meta for that node or edge."),
            NestedField(8, "release_version", StringType(), required=True,
                        doc="This ontology's own version, read from graphs[0].meta.version in the "
                            "file (a version IRI or a date, whichever the ontology publishes). Raw "
                            "is replaced wholesale per value of this column, so more than one "
                            "release can coexist, the same as raw.ensembl__gtf per ensembl_release."),
            NestedField(9, "landed_in", StringType(), required=True,
                        doc="The biocOnIce release whose ingest landed these rows."),
        ),
        comment=f"{name.upper()} landed verbatim from its OBO Graphs JSON release: one row per "
                f"node and one row per edge, the WHOLE file — every imported class or property "
                f"from another ontology included, not filtered to the {name.upper()} namespace. "
                f"Filtering at land time would make raw a function of what ontology.term happens "
                f"to derive today (ADR-0002); ontology.term's node count matches this table's "
                f"node-row count exactly for that reason. Licence {licence}.",
        properties={"bioc.license": licence},
    )


def _ncbi_gene_pairs(comment, relationship):
    """raw.ncbi__gene_orthologs and raw.ncbi__gene_group: one five-column format, per NCBI's README."""
    return TableDef(
        schema=Schema(
            NestedField(1, "taxon_id", IntegerType(), required=True,
                        doc="NCBI taxonomy id of gene_id's organism. Upstream '#tax_id'."),
            NestedField(2, "gene_id", StringType(), required=True,
                        doc="NCBI Entrez GeneID of the first gene. Upstream 'GeneID'."),
            NestedField(3, "relationship", StringType(), required=True,
                        doc="Upstream 'relationship', read as 'gene_id has this relationship to "
                            f"other_gene_id'. {relationship}"),
            NestedField(4, "other_taxon_id", IntegerType(), required=True,
                        doc="NCBI taxonomy id of other_gene_id's organism. Upstream 'Other_tax_id'."),
            NestedField(5, "other_gene_id", StringType(), required=True,
                        doc="NCBI Entrez GeneID of the second gene. Upstream 'Other_GeneID'."),
            NestedField(6, "landed_in", StringType(), required=True,
                        doc="The biocOnIce release whose ingest landed these rows."),
        ),
        comment=comment,
        properties={"bioc.column.taxon_id.prefix": "ncbitaxon",
                    "bioc.column.other_taxon_id.prefix": "ncbitaxon",
                    "bioc.column.gene_id.prefix": "ncbigene",
                    "bioc.column.other_gene_id.prefix": "ncbigene"},
    )


GWAS_LICENCE = ("NHGRI-EBI GWAS Catalog, release <gwas_catalog_release>, under EMBL-EBI Terms of "
                "Use (https://www.ebi.ac.uk/about/terms-of-use/): no additional restrictions on "
                "use or redistribution, attribution expected. Not a formal open licence.")
_GWAS_PROPERTIES = {"bioc.column.pubmed_id.prefix": "pubmed",
                    "bioc.license": "LicenseRef-EMBL-EBI-Terms-of-Use"}
_NA_NR = "'NA' and 'NR' are the curators' \"not applicable\" and \"not reported\", kept as published."

# Column docs for the two GWAS Catalog downloads, in each file's column order — the
# raw tables are declared from these. Both files repeat the study-level columns.
_GWAS_ASSOCIATION = {
    "date_added_to_catalog": "Date the study was published in the Catalog, YYYY-MM-DD.",
    "pubmed_id": "PubMed id of the publication ('PUBMEDID' / 'PUBMED ID' upstream).",
    "first_author": "First author of the publication, e.g. 'Schoeler T'.",
    "date": "Publication date, YYYY-MM-DD (online date where there is one).",
    "journal": "Journal, abbreviated, e.g. 'Am J Hum Genet'.",
    "link": "PubMed URL of the publication, without a scheme: 'www.ncbi.nlm.nih.gov/pubmed/<id>'.",
    "study": "Title of the publication. One publication is usually many studies (one per "
             "trait analysed), so this repeats across study accessions.",
    "disease_trait": "The disease or trait examined, as the curator worded it from the paper. "
                     "Free text; the ontology form is mapped_trait_uri.",
    "initial_sample_size": "Sample size and ancestry of the discovery stage, as prose, e.g. "
                           "'43,509 European ancestry individuals'.",
    "replication_sample_size": "Sample size and ancestry of the replication stage, as prose. " + _NA_NR,
    "region": "Cytogenetic region of the variant, e.g. 17q21.31. NULL where the Catalog could "
              "not map the variant.",
    "chr_id": "Chromosome of the variant, GRCh38. One value per SNP on a multi-SNP row, joined "
              "the way snps is (';' or ' x '). NULL where unmapped.",
    "chr_pos": "1-based GRCh38 position of the variant. Text: a multi-SNP row carries one "
               "position per SNP, e.g. '31157072 x 31272944'. NULL where unmapped.",
    "reported_genes": "Gene(s) the authors reported for the association, ', '-separated. 'NR' "
                      "is not reported; 'intergenic' (either case) is the authors' word. NULL "
                      "on 926,711 of 1,192,604 rows at 2026-09-15.",
    "mapped_gene": "Gene symbol(s) the Catalog's Ensembl mapping gives the variant: the genes "
                   "it overlaps, ', '-separated, or 'UPSTREAM - DOWNSTREAM' for an intergenic "
                   "one. On a multi-SNP row each SNP's entry is joined by ';' or ' x ', so this "
                   "is not safely splittable on any one separator (symbols contain '-').",
    "upstream_gene_id": "Ensembl gene id of the nearest upstream gene, for an intergenic variant.",
    "downstream_gene_id": "Ensembl gene id of the nearest downstream gene, for an intergenic variant.",
    "snp_gene_ids": "Ensembl gene id(s) of the genes the variant lies within, ', '-separated.",
    "upstream_gene_distance": "Distance to the nearest upstream gene, in base pairs.",
    "downstream_gene_distance": "Distance to the nearest downstream gene, in base pairs.",
    "strongest_snp_risk_allele": "The variant and its risk or effect allele, 'rs2328895-C'; '?' "
                                 "where the allele was not reported. Multi-SNP rows list every "
                                 "SNP, joined as in snps.",
    "snps": "The variant: an rsID on most rows, otherwise whatever the paper gave "
            "('chr19:5831829'; 201,351 rows at 2026-09-15). NOT one SNP per row: a haplotype "
            "lists its SNPs joined by '; ' (1,956 rows) or ', ' (127), and a SNP x SNP "
            "interaction joins two with ' x ' (3,288).",
    "merged": "'1' if dbSNP has merged this rsID into another, else '0'.",
    "snp_id_current": "The current rsID, digits only, no 'rs'. Differs from snps where merged "
                      "is 1. Text: a few carry a stray trailing character upstream. NULL on "
                      "multi-SNP and unmapped rows.",
    "context": "Most severe Ensembl VEP consequence of the variant, e.g. intron_variant.",
    "intergenic": "'1' if the variant lies between genes, '0' if within one.",
    "risk_allele_frequency": "Reported frequency of the risk allele in controls. Text because "
                             "it is not always a number: 'NR' on half the rows, ranges "
                             "('0.46-0.52'), annotations ('0.77 (EA)').",
    "p_value": "Reported p-value, as printed, e.g. '1E-8'. Text on purpose, in the derived "
               "table too: 6,275 rows at 2026-09-15 are below the smallest double ('1E-396') "
               "and would read as 0. Use pvalue_mlog for arithmetic.",
    "pvalue_mlog": "-log10 of the p-value.",
    "p_value_text": "What the p-value is conditional on, in parentheses as published: "
                    "'(women)', '(dominant)', '(conditioned on rs123)'. This is what "
                    "separates most repeats of one SNP within one study.",
    "or_beta": "Reported odds ratio or beta coefficient ('OR or BETA' upstream) — which one "
               "is not flagged; a unit in ci_95_text means a beta. Before 2021 the Catalog "
               "inverted every OR < 1, with its allele, so older ORs are all > 1.",
    "ci_95_text": "Reported 95% confidence interval ('95% CI (TEXT)' upstream), with the unit "
                  "and direction for a beta: '[0.015-0.034] unit decrease'. " + _NA_NR,
    "platform": "Genotyping platform manufacturer and the number of SNPs passing QC "
                "('PLATFORM [SNPS PASSING QC]' upstream), e.g. 'Affymetrix [509492]'.",
    "cnv": "Whether the study is of copy number variation. 'N' on every row at 2026-09-15.",
    "mapped_trait": "Label(s) of the ontology term(s) the Catalog mapped the trait to, "
                    "', '-separated — and labels contain commas, so split mapped_trait_uri, "
                    "not this.",
    "mapped_trait_uri": "IRI(s) of the mapped ontology term(s), ', '-separated, verbatim: EFO's "
                        "own (http://www.ebi.ac.uk/efo/EFO_0007789) and the terms EFO imports "
                        "from MONDO, OBA, HP, GO, Orphanet and others. On an association row "
                        "this is the association's mapping, which can differ from its study's.",
    "study_accession": "GWAS Catalog study accession, e.g. GCST012020. One per (publication, "
                       "trait analysed); the join between the two files.",
    "genotyping_technology": "e.g. 'Genome-wide genotyping array', 'Genome-wide sequencing'; "
                             "several are ', '-separated.",
}
_S = _GWAS_ASSOCIATION
_GWAS_STUDY = {
    **{c: _S[c] for c in ("date_added_to_catalog", "pubmed_id", "first_author", "date", "journal",
                          "link", "study", "disease_trait", "initial_sample_size",
                          "replication_sample_size", "platform")},
    "association_count": "Number of rows this study has in the associations file. 0 for two "
                         "thirds of studies: most are summary-statistics depositions with no "
                         "curated top associations.",
    **{c: _S[c] for c in ("mapped_trait", "mapped_trait_uri", "study_accession",
                          "genotyping_technology")},
    "submission_date": "Declared by the file; empty on every row at 2026-09-15.",
    "statistical_model": "Declared by the file; empty on every row at 2026-09-15.",
    "background_trait": "Declared by the file; empty on every row at 2026-09-15.",
    "mapped_background_trait": "Label(s) of the ontology term(s) for a trait shared by every "
                               "participant (e.g. a GWAS of nephropathy within diabetics), "
                               "', '-separated. NULL for most studies.",
    "mapped_background_trait_uri": "IRI(s) of the background trait term(s), ', '-separated, verbatim.",
    "cohort": "Named cohort(s) the samples came from, '|'-separated, e.g. 'UKB|CHARGE'. " + _NA_NR,
    "full_summary_statistics": "'yes' if the Catalog hosts full summary statistics for the study.",
    "summary_stats_location": "URL of the study's summary statistics directory on the EBI "
                              "FTP site; 'NA' where there are none. Those files are not landed.",
    "gxe": "'yes' if the study analyses a gene-by-environment interaction.",
}
del _S


def _gwas_raw(docs, what):
    """raw.gwas_catalog__*: every upstream column as text, in file order, plus the two we add."""
    return TableDef(
        schema=Schema(
            *[NestedField(i, name, StringType(), required=name == "study_accession", doc=doc)
              for i, (name, doc) in enumerate(docs.items(), 1)],
            NestedField(len(docs) + 1, "gwas_catalog_release", StringType(), required=True,
                        doc="The Catalog release these rows came from, YYYY-MM-DD: the dated "
                            "directory under releases/ (the retrieval date if landed from "
                            "anywhere else). Raw is replaced wholesale per value of this "
                            "column, so more than one release can coexist."),
            NestedField(len(docs) + 2, "landed_in", StringType(), required=True,
                        doc="The biocOnIce release whose ingest landed these rows."),
        ),
        comment=f"{what} Verbatim and whole: every column as text under a snake_cased name "
                f"(the column docs give upstream spellings that differ), no row dropped — "
                f"whole-row duplicates included — and nothing split. The file has no quoting, "
                f"so a '\"' is data. An empty cell is read as NULL. {GWAS_LICENCE}",
        properties=_GWAS_PROPERTIES,
    )


TABLES = {
    "provenance.release": TableDef(
        schema=Schema(
            NestedField(1, "release", StringType(), required=True,
                        doc="biocOnIce release, YYYY.MM with zero-padded corrections "
                            "YYYY.MM.NN. Zero-padded because '2026.10.10' sorts before "
                            "'2026.10.2' and release ordering would otherwise invert."),
            NestedField(2, "source", StringType(), required=True,
                        doc="Source key, e.g. ensembl, ncbi_gene, go."),
            NestedField(3, "source_version", StringType(),
                        doc="The upstream version in the SOURCE'S OWN vocabulary, never "
                            "normalised: '116' for Ensembl, '2026-08-06' for a source that "
                            "publishes no version. NULL only where nothing at all is knowable."),
            NestedField(4, "version_method", StringType(), required=True,
                        doc="How the version was determined: release_number, "
                            "http_last_modified, etag, ftp_index_probe, retrieval_date, "
                            "unavailable. 'unavailable' is a legitimate value — a source that "
                            "publishes no version is recorded as such, never given a "
                            "fabricated one."),
            NestedField(5, "retrieved_at", StringType(), required=True,
                        doc="UTC timestamp at which the source was fetched."),
            NestedField(6, "url", StringType(), doc="Canonical URL fetched."),
            NestedField(7, "checksum", StringType(), doc="SHA-256 of the retrieved bytes, where computed."),
            NestedField(8, "row_count", LongType(),
                        doc="Rows landed from this source, as a cheap integrity check."),
        ),
        business_key=("release", "source"),
        comment="One row per (biocOnIce release, source): what this release was built from. "
                "This is what makes a release reproducible — resolve it here to each "
                "source's own version, then query each table at that release. Durable "
                "by design: unlike Iceberg snapshot summaries it does not expire.",
        properties={},
    ),
    "raw.ncbi__gene2ensembl": TableDef(
        schema=Schema(
            NestedField(1, "taxon_id", IntegerType(), required=True, doc=TAXON),
            NestedField(2, "gene_id", StringType(), doc="NCBI Entrez GeneID."),
            NestedField(3, "ensembl_gene_id", StringType(),
                        doc="Ensembl stable gene id NCBI maps this Entrez gene to."),
            NestedField(4, "rna_accession", StringType(), doc="RefSeq RNA accession.version."),
            NestedField(5, "ensembl_rna_id", StringType(), doc="Ensembl transcript id."),
            NestedField(6, "protein_accession", StringType(), doc="RefSeq protein accession.version."),
            NestedField(7, "ensembl_protein_id", StringType(), doc="Ensembl protein id."),
            NestedField(8, "landed_in", StringType(), required=True,
                        doc="The biocOnIce release whose ingest landed these rows."),
        ),
        comment="NCBI gene2ensembl landed verbatim and whole: every organism NCBI knows, not "
                "only the ones we derive annotation for. NCBI's '-' placeholder is read as NULL. "
                "Regenerated nightly upstream, so it has no release: the retrieval date is the "
                "version, per NLM's own citation form.",
        properties={"bioc.column.taxon_id.prefix": "ncbitaxon",
                    "bioc.column.gene_id.prefix": "ncbigene",
                    "bioc.column.ensembl_gene_id.prefix": "ensembl"},
    ),
    "raw.ncbi__gene_info": TableDef(
        schema=Schema(
            NestedField(1, "taxon_id", IntegerType(), required=True, doc=TAXON),
            NestedField(2, "gene_id", StringType(), required=True, doc="NCBI Entrez GeneID."),
            NestedField(3, "symbol", StringType(),
                        doc="Default symbol. The nomenclature authority's symbol where there is "
                            "one, otherwise NCBI's own; symbol_authority says which."),
            NestedField(4, "locus_tag", StringType(),
                        doc="Submitter-assigned locus tag, e.g. b0001. Mostly prokaryotic."),
            NestedField(5, "synonyms", StringType(),
                        doc="Alternate symbols, pipe-separated in one string as NCBI publishes "
                            "them. Split at '|' to get ALIAS rows."),
            NestedField(6, "dbxrefs", StringType(),
                        doc="Cross-references, pipe-separated 'Authority:id' pairs, e.g. "
                            "'MIM:191170|HGNC:HGNC:11998|Ensembl:ENSG00000141510'. Split at the "
                            "FIRST colon only: HGNC's own ids embed one."),
            NestedField(7, "chromosome", StringType(),
                        doc="Chromosome as NCBI names it, e.g. 17. May be a pipe-separated list "
                            "for genes placed on more than one, or 'Un' for unplaced."),
            NestedField(8, "map_location", StringType(),
                        doc="Cytogenetic band, e.g. 17p13.1. Not a coordinate; for coordinates "
                            "use annotation.exon."),
            NestedField(9, "description", StringType(),
                        doc="Descriptive gene name, e.g. 'tumor protein p53'. This is the "
                            "column OrgDb serves as GENENAME."),
            NestedField(10, "type_of_gene", StringType(),
                        doc="NCBI gene type, e.g. protein-coding, ncRNA, pseudo. NCBI's "
                            "vocabulary, hyphenated — not Ensembl's biotype vocabulary."),
            NestedField(11, "symbol_authority", StringType(),
                        doc="Symbol as assigned by the nomenclature authority (HGNC, MGI), NULL "
                            "where none has named the gene."),
            NestedField(12, "full_name_authority", StringType(),
                        doc="Full name from the nomenclature authority, NULL where none."),
            NestedField(13, "nomenclature_status", StringType(),
                        doc="'O' official, 'I' interim, NULL where the gene is unnamed."),
            NestedField(14, "other_designations", StringType(),
                        doc="Further names, pipe-separated. Free text, not symbols: not treated "
                            "as ALIAS."),
            NestedField(15, "modification_date", StringType(),
                        doc="Date this Gene record last changed, YYYYMMDD. Per-record, so it is "
                            "not a version for the file as a whole."),
            NestedField(16, "feature_type", StringType(),
                        doc="Feature type for records that are not genes, e.g. 'biological "
                            "region'. NULL for ordinary genes."),
            NestedField(17, "landed_in", StringType(), required=True,
                        doc="The biocOnIce release whose ingest landed these rows."),
        ),
        comment="NCBI gene_info landed verbatim and whole — 71.5M records across 53,800 taxa, not "
                "only the ones we derive annotation for, since a third species should not cost a "
                "re-fetch. NCBI's "
                "'-' placeholder is read as NULL. Pipe-separated fields are kept as published, "
                "unsplit: splitting is interpretation and belongs in transform. Regenerated "
                "nightly upstream, so the retrieval date is the version.",
        properties={"bioc.column.taxon_id.prefix": "ncbitaxon",
                    "bioc.column.gene_id.prefix": "ncbigene"},
    ),
    "raw.ncbi__gene_history": TableDef(
        schema=Schema(
            NestedField(1, "taxon_id", IntegerType(), required=True, doc=TAXON),
            NestedField(2, "gene_id", StringType(),
                        doc="Entrez GeneID that discontinued_gene_id was merged INTO. NULL "
                            "(NCBI's '-') means the id was retired outright with no successor, "
                            "which is the majority of this table."),
            NestedField(3, "discontinued_gene_id", StringType(), required=True,
                        doc="The Entrez GeneID that no longer exists. This is what a stale "
                            "identifier in an old analysis looks up as."),
            NestedField(4, "discontinued_symbol", StringType(),
                        doc="Symbol the discontinued id carried when it was withdrawn."),
            NestedField(5, "discontinue_date", StringType(),
                        doc="Date of withdrawal, YYYYMMDD. NCBI's own clock, not a biocOnIce release."),
            NestedField(6, "landed_in", StringType(), required=True,
                        doc="The biocOnIce release whose ingest landed these rows."),
        ),
        comment="NCBI gene_history landed verbatim and whole: the tombstone list for every "
                "Entrez GeneID ever withdrawn. Landed but NOT YET INTERPRETED — nothing derives "
                "from it, because whether supersession ('merged into') is modelled as a table, a "
                "typed retirement reason, or not at all is still open (issue #15, item 4). It is "
                "here so that question can be settled against real data rather than guessed at.",
        properties={"bioc.column.taxon_id.prefix": "ncbitaxon",
                    "bioc.column.gene_id.prefix": "ncbigene",
                    "bioc.column.discontinued_gene_id.prefix": "ncbigene"},
    ),
    "raw.bugsigdb__full_dump": TableDef(
        schema=Schema(
            NestedField(1, "bsdb_id", StringType(), required=True,
                        doc="BugSigDB signature id, e.g. 'bsdb:83/1/1'. Compound: "
                            "study/experiment/signature. Unique per row (7,425 of 7,425 "
                            "distinct at v1.3.1), so it is the natural key even though raw "
                            "declares none."),
            NestedField(2, "study", StringType(), doc="Wiki page name of the study, e.g. 'Study 83'."),
            NestedField(3, "study_design", StringType(),
                        doc="e.g. case-control, cross-sectional observational, laboratory "
                            "experiment, prospective cohort."),
            NestedField(4, "pmid", StringType(), doc="PubMed id of the source publication."),
            NestedField(5, "doi", StringType(), doc="DOI of the source publication."),
            NestedField(6, "url", StringType(), doc="Publication URL as curated."),
            NestedField(7, "authors_list", StringType(), doc="Author list of the publication, free text."),
            NestedField(8, "title", StringType(), doc="Title of the publication."),
            NestedField(9, "journal", StringType(), doc="Journal name."),
            NestedField(10, "year", StringType(), doc="Publication year. String, not an integer, "
                                                      "because raw is landed unparsed."),
            NestedField(11, "keywords", StringType(), doc="Curator keywords; frequently absent."),
            NestedField(12, "experiment", StringType(),
                        doc="Wiki page name of the experiment within the study, e.g. 'Experiment 1'. "
                            "One study has many experiments; one experiment has many signatures."),
            NestedField(13, "location_of_subjects", StringType(), doc="Geographic origin of subjects."),
            NestedField(14, "host_species", StringType(), doc="Host organism, e.g. Homo sapiens. Not "
                                                              "the microbes — those are the signature."),
            NestedField(15, "body_site", StringType(), doc="Sampled body site as curated text."),
            NestedField(16, "uberon_id", StringType(), doc="UBERON CURIE for body_site, e.g. UBERON:0001988."),
            NestedField(17, "condition", StringType(), doc="Studied condition as curated text."),
            NestedField(18, "efo_id", StringType(), doc="EFO CURIE for condition, e.g. EFO:0001073."),
            NestedField(19, "group_0_name", StringType(), doc="Name of the control/reference group."),
            NestedField(20, "group_1_name", StringType(), doc="Name of the group the signature describes."),
            NestedField(21, "group_1_definition", StringType(), doc="Free-text inclusion criteria for group 1."),
            NestedField(22, "group_0_sample_size", StringType(), doc="Subjects in group 0. String: not always numeric."),
            NestedField(23, "group_1_sample_size", StringType(), doc="Subjects in group 1. String: not always numeric."),
            NestedField(24, "antibiotics_exclusion", StringType(),
                        doc="Antibiotic washout required for inclusion, e.g. '3 months'."),
            NestedField(25, "sequencing_type", StringType(),
                        doc="Assay: 16S, WMS (whole metagenome shotgun), 'ITS / ITS2', PCR."),
            NestedField(26, "variable_region_16s", StringType(),
                        doc="16S hypervariable region, upstream column '16S variable region'. Digits "
                            "are CONCATENATED region numbers, not a number: '34' means V3-V4, '4' "
                            "means V4, '12' means V1-V2. Do not cast this to an integer."),
            NestedField(27, "sequencing_platform", StringType(), doc="Instrument/platform reported."),
            NestedField(28, "data_transformation", StringType(),
                        doc="e.g. relative abundances, raw counts, centered log-ratio."),
            NestedField(29, "statistical_test", StringType(), doc="Test used for differential abundance."),
            NestedField(30, "significance_threshold", StringType(), doc="Alpha, e.g. '0.05'. String, unparsed."),
            NestedField(31, "mht_correction", StringType(),
                        doc="Whether multiple-hypothesis correction was applied: 'TRUE'/'FALSE' as "
                            "strings, since raw is unparsed."),
            NestedField(32, "lda_score_above", StringType(), doc="LEfSe LDA score cutoff where one was used."),
            NestedField(33, "matched_on", StringType(),
                        doc="Comma-separated variables the groups were matched on, e.g. 'age,sex'."),
            NestedField(34, "confounders_controlled_for", StringType(), doc="Comma-separated confounders adjusted for."),
            NestedField(35, "pielou", StringType(), doc=ALPHA_DIVERSITY),
            NestedField(36, "shannon", StringType(), doc=ALPHA_DIVERSITY),
            NestedField(37, "chao1", StringType(), doc=ALPHA_DIVERSITY),
            NestedField(38, "simpson", StringType(), doc=ALPHA_DIVERSITY),
            NestedField(39, "inverse_simpson", StringType(), doc=ALPHA_DIVERSITY),
            NestedField(40, "richness", StringType(), doc=ALPHA_DIVERSITY),
            NestedField(41, "signature_page_name", StringType(),
                        doc="Wiki page name of the signature within its experiment, e.g. 'Signature 1'."),
            NestedField(42, "source_in_paper", StringType(),
                        doc="Where in the publication the signature was read from, e.g. 'Table 2', "
                            "'Figure 3'. Upstream column name is 'Source'; renamed here because "
                            "`source` elsewhere in this catalog means the asserting authority."),
            NestedField(43, "curated_date", StringType(),
                        doc="Date of curation as published, e.g. '10 January 2021'. Human-readable, "
                            "not ISO 8601 — parsing it is a transform concern."),
            NestedField(44, "curator", StringType(), doc="Wiki username of the curator."),
            NestedField(45, "revision_editor", StringType(), doc="Wiki username of the last editor."),
            NestedField(46, "description", StringType(), doc="Curator's description of the signature."),
            NestedField(47, "abundance_in_group_1", StringType(),
                        doc="Direction of the signature: 'increased' or 'decreased' in group 1 "
                            "relative to group 0. This is what makes a signature signed — the same "
                            "taxa increased and decreased are different signatures."),
            NestedField(48, "metaphlan_taxon_names", StringType(),
                        doc="Signature members as MetaPhlAn lineages. TWO levels of nesting, kept "
                            "verbatim: ';' separates members, '|' separates ranks within one "
                            "member's lineage, with k__/p__/c__/o__/f__/g__/s__ rank prefixes. "
                            "Splitting on '|' alone silently turns one taxon into seven."),
            NestedField(49, "ncbi_taxonomy_ids", StringType(),
                        doc="Signature members as NCBI taxon ids, same two-level nesting as "
                            "metaphlan_taxon_names: ';' between members, '|' up each member's "
                            "lineage from kingdom to the curated rank. The LAST element of each "
                            "'|' group is the taxon actually reported; the rest are its lineage."),
            NestedField(50, "state", StringType(),
                        doc="Curation state. Only 'Complete' appears: the export filters incomplete "
                            "records upstream, so this is not a usable filter here."),
            NestedField(51, "reviewer", StringType(), doc="Wiki username of the reviewer."),
            NestedField(52, "export_timestamp", StringType(),
                        doc="The export's own self-declared timestamp, taken from the banner line "
                            "of the CSV, e.g. '2026-04-24_00:41_UTC'. In-band provenance: it is "
                            "the only version marker when landing from the hourly devel export, "
                            "which carries no tag."),
            NestedField(53, "bugsigdb_version", StringType(), required=True,
                        doc="The BugSigDBExports release tag this file came from, e.g. 'v1.3.1'. "
                            "Raw is replaced wholesale per value of this column."),
            NestedField(54, "landed_in", StringType(), required=True,
                        doc="The biocOnIce release whose ingest landed these rows."),
        ),
        comment="BugSigDB's full_dump.csv landed verbatim: one row per curated microbial signature, "
                "flattened across study, experiment and signature. Every column is a string and "
                "nothing is split, per the raw contract — in particular the two member-list columns "
                "keep their ';' and '|' nesting. BugSigDB's 'NA' placeholder is read as NULL, the "
                "same treatment NCBI's '-' gets. Landed from a release TAG rather than the hourly "
                "devel export, so it is immutable and citable (each release has a Zenodo DOI). "
                "Licence CC BY 4.0, declared both in .zenodo.json and in the file's own banner line.",
        properties={"bioc.column.pmid.prefix": "pubmed",
                    "bioc.column.doi.prefix": "doi",
                    "bioc.column.efo_id.prefix": "efo",
                    "bioc.column.uberon_id.prefix": "uberon",
                    "bioc.column.ncbi_taxonomy_ids.prefix": "ncbitaxon",
                    "bioc.license": "CC-BY-4.0"},
    ),
    "raw.ensembl__gtf": TableDef(
        schema=Schema(
            NestedField(1, "seqname", StringType(), doc="GTF column 1, the sequence name."),
            NestedField(2, "source", StringType(), doc="GTF column 2, the annotation source."),
            NestedField(3, "feature", StringType(),
                        doc="GTF column 3: gene, transcript, exon, CDS, five_prime_utr, "
                            "three_prime_utr, start_codon, stop_codon, Selenocysteine."),
            NestedField(4, "start", LongType(), doc="GTF column 4, 1-based inclusive."),
            NestedField(5, "end", LongType(), doc="GTF column 5, 1-based inclusive."),
            NestedField(6, "score", StringType(), doc="GTF column 6, '.' throughout Ensembl GTFs."),
            NestedField(7, "strand", StringType(), doc="GTF column 7, '+' or '-'."),
            NestedField(8, "frame", StringType(),
                        doc="GTF column 8. On a CDS row this is the reading-frame phase; '.' elsewhere."),
            NestedField(9, "attribute", StringType(),
                        doc="GTF column 9 verbatim, unparsed: the semicolon-separated key \"value\" "
                            "list carrying gene_id, transcript_id, exon_number, tag and the rest. "
                            "Kept whole so attributes nobody has needed yet can be extracted later "
                            "without re-fetching the source."),
            NestedField(10, "taxon_id", IntegerType(), required=True, doc=TAXON),
            NestedField(11, "ensembl_release", StringType(), required=True,
                        doc="Ensembl release this file came from, e.g. 116."),
            NestedField(12, "landed_in", StringType(), required=True,
                        doc="The biocOnIce release whose ingest landed these rows. Not a validity interval: raw is replaced per source version, not versioned."),
        ),
        comment="Ensembl GTF landed verbatim, one row per feature line. Deliberately has no "
                "merge key and no validity interval: a GTF line has no natural identity, "
                "and an "
                "Ensembl release is immutable, so this table is replaced wholesale per "
                "(taxon_id, ensembl_release) and is idempotent under re-ingest. History here "
                "is the accumulation of releases, not a validity interval.",
        properties={"bioc.column.taxon_id.prefix": "ncbitaxon",
                    "bioc.column.start.coordinate_system": COORD,
                    "bioc.column.end.coordinate_system": COORD},
    ),
    "reference.genome": TableDef(
        schema=Schema(
            NestedField(1, "genome_id", StringType(), required=True,
                        doc="Assembly accession, e.g. GCA_000001405.29. Stable across releases."),
            NestedField(2, "taxon_id", IntegerType(), required=True, doc=TAXON),
            NestedField(3, "source", StringType(), required=True, doc=SOURCE),
            NestedField(4, "assembly_name", StringType(),
                        doc="Provider's assembly name, e.g. GRCh38.p14."),
            NestedField(5, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(6, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("genome_id", "taxon_id", "source"),
        partition_by=("source", "taxon_id"),
        comment="Genome assemblies. One row per assembly per organism per source: two "
                "providers may describe the same assembly, and neither may retire the "
                "other's row.",
        properties={"bioc.column.genome_id.prefix": "insdc.gca",
                    "bioc.column.taxon_id.prefix": "ncbitaxon"},
    ),
    "annotation.gene": TableDef(
        schema=Schema(
            NestedField(1, "gene_id", StringType(), required=True,
                        doc="The provider's stable gene id without version, e.g. "
                            "ENSG00000141510 under source ENSEMBL. "
                            "Part of the merge key; an upstream version bump updates this row "
                            "rather than creating a new one."),
            NestedField(2, "taxon_id", IntegerType(), required=True, doc=TAXON),
            NestedField(9, "source", StringType(), required=True, doc=SOURCE),
            NestedField(3, "version", StringType(),
                        doc="Upstream record version at this release, e.g. 21. Changes over time."),
            NestedField(4, "symbol", StringType(),
                        doc="Official gene symbol, e.g. TP53. NULL for genes with no assigned name."),
            NestedField(5, "gene_type", StringType(),
                        doc="Gene biotype in the provider's vocabulary, e.g. protein_coding, "
                            "lncRNA for Ensembl."),
            NestedField(6, "curation_source", StringType(),
                        doc="The provider's own annotation-source tag, e.g. ensembl_havana from "
                            "GTF column 2. Curation provenance within the provider — the "
                            "asserting provider itself is `source`."),
            NestedField(7, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(8, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("gene_id", "taxon_id", "source"),
        partition_by=("source", "taxon_id"),
        comment="Genes, stacked across annotation providers: one row per gene per organism "
                "per source, in the provider's own id space. Join to annotation.transcript "
                "on (gene_id, taxon_id, source). Descriptions and cytogenetic bands are not "
                "here: they come from NCBI, keyed by Entrez id, in annotation.ncbi__gene.",
        properties={"bioc.column.gene_id.prefix": "ensembl",
                    "bioc.column.taxon_id.prefix": "ncbitaxon"},
    ),
    "annotation.ncbi__gene": TableDef(
        schema=Schema(
            NestedField(1, "gene_id", StringType(), required=True,
                        doc="NCBI Entrez GeneID, e.g. 7157. The central key of OrgDb, which is "
                            "why this table exists keyed on it rather than on an Ensembl id."),
            NestedField(2, "taxon_id", IntegerType(), required=True, doc=TAXON),
            NestedField(3, "symbol", StringType(),
                        doc="NCBI's default symbol, e.g. TP53. May disagree with the symbol "
                            "Ensembl carries in annotation.gene; neither is corrected to match "
                            "the other."),
            NestedField(4, "description", StringType(),
                        doc="Descriptive gene name, e.g. 'tumor protein p53'. This is OrgDb's "
                            "GENENAME."),
            NestedField(5, "gene_type", StringType(),
                        doc="NCBI gene type, e.g. protein-coding, ncRNA, pseudo. OrgDb's "
                            "GENETYPE. NCBI's hyphenated vocabulary, deliberately not mapped "
                            "onto Ensembl's biotype names in annotation.gene.gene_type."),
            NestedField(6, "chromosome", StringType(),
                        doc="Chromosome as NCBI names it. Pipe-separated where NCBI places the "
                            "gene on more than one, kept as published."),
            NestedField(7, "map_location", StringType(),
                        doc="Cytogenetic band, e.g. 17p13.1. OrgDb's MAP. Not a coordinate."),
            NestedField(8, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(9, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("gene_id", "taxon_id"),
        comment="Genes as NCBI Gene defines them, keyed by Entrez GeneID. Separate from "
                "annotation.gene rather than extra columns on it, because gene_info is keyed by "
                "Entrez id and the Entrez-to-Ensembl mapping is many-to-many in both directions: "
                "writing a description onto an Ensembl-keyed row would mean silently picking one "
                "of several Entrez records for the genes where they disagree. Reach it from an "
                "Ensembl gene through annotation.identifier_mapping (ENSEMBL <-> ENTREZ), which "
                "keeps the fan-out visible instead of resolving it at write time. Synonyms and "
                "dbXrefs from the same source land in annotation.identifier_mapping. Note that "
                "'gene' here is NCBI's sense of the word: most rows are gene_type "
                "'biological-region' — regulatory features, 128,261 of human's 193,809 records "
                "against 20,595 protein-coding. They are kept rather than filtered, because "
                "gene_type distinguishes them and OrgDb's ENTREZID key space includes them; "
                "filter on gene_type if you want genes in the narrower sense.",
        properties={"bioc.column.gene_id.prefix": "ncbigene",
                    "bioc.column.taxon_id.prefix": "ncbitaxon"},
    ),
    "annotation.transcript": TableDef(
        schema=Schema(
            NestedField(1, "transcript_id", StringType(), required=True,
                        doc="The provider's stable transcript id without version, e.g. "
                            "ENST00000269305 under source ENSEMBL."),
            NestedField(2, "taxon_id", IntegerType(), required=True, doc=TAXON),
            NestedField(9, "source", StringType(), required=True, doc=SOURCE),
            NestedField(3, "gene_id", StringType(),
                        doc="Stable gene id of the parent gene, in the same provider's id "
                            "space. Joins to annotation.gene on (gene_id, taxon_id, source)."),
            NestedField(4, "version", StringType(), doc="Upstream record version at this release."),
            NestedField(5, "biotype", StringType(),
                        doc="Transcript biotype, e.g. protein_coding, retained_intron."),
            NestedField(6, "canonical", BooleanType(),
                        doc="True if Ensembl tags this as the canonical transcript of its gene."),
            NestedField(7, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(8, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("transcript_id", "taxon_id", "source"),
        partition_by=("source", "taxon_id"),
        comment="Transcripts, stacked across annotation providers. One row per transcript "
                "per source; a gene has many.",
        properties={"bioc.column.transcript_id.prefix": "ensembl",
                    "bioc.column.gene_id.prefix": "ensembl",
                    "bioc.column.taxon_id.prefix": "ncbitaxon"},
    ),
    "annotation.exon": TableDef(
        schema=Schema(
            NestedField(1, "exon_id", StringType(), required=True,
                        doc="The provider's stable exon id without version, e.g. "
                            "ENSE00002064269 under source ENSEMBL. NOT unique on its own: one "
                            "exon is shared by every transcript containing it, so the key is "
                            "(exon_id, transcript_id, taxon_id, source)."),
            NestedField(2, "transcript_id", StringType(), required=True,
                        doc="Transcript this row places the exon in. Joins to "
                            "annotation.transcript on (transcript_id, taxon_id, source)."),
            NestedField(3, "taxon_id", IntegerType(), required=True, doc=TAXON),
            NestedField(14, "source", StringType(), required=True, doc=SOURCE),
            NestedField(4, "sequence_name", StringType(),
                        doc="Sequence the exon lies on, as named by Ensembl, e.g. 17 — not chr17."),
            NestedField(5, "start", LongType(),
                        doc="Start coordinate, 1-based and inclusive (Ensembl/GTF convention, "
                            "NOT the 0-based half-open convention of BED and UCSC)."),
            NestedField(6, "end", LongType(), doc="End coordinate, 1-based and inclusive."),
            NestedField(7, "strand", StringType(), doc="'+' or '-'."),
            NestedField(8, "rank", IntegerType(),
                        doc="Ordinal of this exon within the transcript, 5' to 3', starting at 1. "
                            "Order exons by rank, not by coordinate: on the minus strand the two "
                            "disagree and coordinate order is reversed."),
            NestedField(9, "cds_start", LongType(),
                        doc="Start of the coding segment within this exon, 1-based inclusive. "
                            "NULL where the exon is not translated."),
            NestedField(10, "cds_end", LongType(), doc="End of the coding segment, 1-based inclusive."),
            NestedField(11, "cds_phase", IntegerType(),
                        doc="Reading frame of the coding segment: bases to remove from its start "
                            "to reach the first complete codon. Not recoverable from coordinates."),
            NestedField(12, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(13, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("exon_id", "transcript_id", "taxon_id", "source"),
        partition_by=("source", "taxon_id"),
        comment="Exons in transcript context, carrying coding bounds, stacked across "
                "annotation providers. Stands in for TxDb's "
                "exon, cds and splicing tables: a row is already keyed by exon and transcript, "
                "so it is the splicing junction, and a coding segment always falls within an "
                "exon of the same transcript. UTRs are derived from cds bounds, not stored.",
        properties={"bioc.column.exon_id.prefix": "ensembl",
                    "bioc.column.transcript_id.prefix": "ensembl",
                    "bioc.column.taxon_id.prefix": "ncbitaxon",
                    "bioc.column.start.coordinate_system": COORD,
                    "bioc.column.end.coordinate_system": COORD,
                    "bioc.column.cds_start.coordinate_system": COORD,
                    "bioc.column.cds_end.coordinate_system": COORD},
    ),
    "annotation.identifier_mapping": TableDef(
        schema=Schema(
            NestedField(1, "source_namespace", StringType(), required=True,
                        doc="Authority of the source identifier, e.g. ENSEMBL."),
            NestedField(2, "source_id", StringType(), required=True, doc="Identifier in source_namespace."),
            NestedField(3, "target_namespace", StringType(), required=True,
                        doc="Authority of the target identifier, e.g. SYMBOL."),
            NestedField(4, "target_id", StringType(), required=True, doc="Identifier in target_namespace."),
            NestedField(5, "taxon_id", IntegerType(), required=True, doc=TAXON),
            NestedField(6, "source", StringType(), required=True,
                        doc="Who asserts this mapping, e.g. Ensembl. Part of the business key, so that one source cannot retire another's cross-references."),
            NestedField(7, "confidence", DoubleType(),
                        doc="Asserter's confidence where one is published; NULL where none is."),
            NestedField(8, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(9, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("source_namespace", "source_id", "target_namespace",
                      "target_id", "taxon_id", "source"),
        comment="Cross-references between identifier authorities. Mappings are many-to-many in "
                "both directions. The whole tuple is the key: a mapping has no attributes that "
                "can change, so it is only ever asserted or withdrawn, never updated.",
        properties={"bioc.column.taxon_id.prefix": "ncbitaxon"},
    ),
    "raw.ncbi__gene2pubmed": TableDef(
        schema=Schema(
            NestedField(1, "taxon_id", IntegerType(), required=True, doc=TAXON),
            NestedField(2, "gene_id", StringType(), required=True, doc="NCBI Entrez GeneID."),
            NestedField(3, "pubmed_id", StringType(), required=True,
                        doc="PubMed id (PMID) of a publication NCBI links to this gene."),
            NestedField(4, "landed_in", StringType(), required=True,
                        doc="The biocOnIce release whose ingest landed these rows."),
        ),
        comment="NCBI gene2pubmed landed verbatim and whole — every organism, ~40M rows, not "
                "only the taxa we derive annotation for, since a third species should not cost "
                "a re-fetch. Regenerated nightly upstream, so it has no release: the retrieval "
                "date is the version.",
        properties={"bioc.column.taxon_id.prefix": "ncbitaxon",
                    "bioc.column.gene_id.prefix": "ncbigene",
                    "bioc.column.pubmed_id.prefix": "pubmed"},
    ),
    "annotation.ncbi__gene_pubmed": TableDef(
        schema=Schema(
            NestedField(1, "gene_id", StringType(), required=True,
                        doc="NCBI Entrez GeneID, e.g. 7157. Part of the merge key."),
            NestedField(2, "taxon_id", IntegerType(), required=True, doc=TAXON),
            NestedField(3, "pubmed_id", StringType(), required=True,
                        doc="PubMed id (PMID) of a publication discussing this gene. This is "
                            "the column OrgDb serves as PMID."),
            NestedField(4, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(5, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("gene_id", "taxon_id", "pubmed_id"),
        comment="Gene-to-publication links as NCBI Gene curates them, keyed by Entrez GeneID. "
                "One row per (gene, publication): a gene has many publications and a "
                "publication many genes. The whole tuple is the key — a link has no "
                "attributes that can change, so it is only ever asserted or withdrawn, "
                "never updated.",
        properties={"bioc.column.gene_id.prefix": "ncbigene",
                    "bioc.column.taxon_id.prefix": "ncbitaxon",
                    "bioc.column.pubmed_id.prefix": "pubmed"},
    ),
    "raw.ncbi__gene2accession": TableDef(
        schema=Schema(
            NestedField(1, "taxon_id", IntegerType(), required=True, doc=TAXON),
            NestedField(2, "gene_id", StringType(), required=True,
                        doc="NCBI Entrez GeneID. Upstream column 'GeneID'."),
            NestedField(3, "status", StringType(),
                        doc="RefSeq status: REVIEWED, VALIDATED, PROVISIONAL, PREDICTED, "
                            "INFERRED, MODEL, or the literal string 'NA'. NULL (NCBI's "
                            "'-') marks a GenBank/INSDC submission, not a RefSeq record."),
            NestedField(4, "rna_nucleotide_accession_version", StringType(),
                        doc="RNA accession.version, e.g. NM_000546.6, version kept as "
                            "published. Upstream column 'RNA_nucleotide_accession.version'; "
                            "upstream's dots become underscores here because a dot is not "
                            "a valid column name."),
            NestedField(5, "rna_nucleotide_gi", StringType(),
                        doc="GI number of the RNA record. Upstream column 'RNA_nucleotide_gi'."),
            NestedField(6, "protein_accession_version", StringType(),
                        doc="Protein accession.version, e.g. NP_000537.3. Upstream column "
                            "'protein_accession.version'."),
            NestedField(7, "protein_gi", StringType(),
                        doc="GI number of the protein record. Upstream column 'protein_gi'."),
            NestedField(8, "genomic_nucleotide_accession_version", StringType(),
                        doc="Genomic accession.version the gene is placed on — RefSeq "
                            "(NC_/NT_/NW_) or GenBank, distinguished by the underscore "
                            "only RefSeq accessions carry. Upstream column "
                            "'genomic_nucleotide_accession.version'."),
            NestedField(9, "genomic_nucleotide_gi", StringType(),
                        doc="GI number of the genomic record. Upstream column "
                            "'genomic_nucleotide_gi'."),
            NestedField(10, "start_position_on_the_genomic_accession", StringType(),
                        doc="Start of the gene on the genomic accession, 0-based as NCBI "
                            "publishes this file. String, unparsed, per the raw contract."),
            NestedField(11, "end_position_on_the_genomic_accession", StringType(),
                        doc="End of the gene on the genomic accession. String, unparsed."),
            NestedField(12, "orientation", StringType(),
                        doc="'+', '-', or '?' where the orientation is not known."),
            NestedField(13, "assembly", StringType(),
                        doc="Assembly the genomic accession belongs to, e.g. 'Reference "
                            "GRCh38.p14 Primary Assembly'."),
            NestedField(14, "mature_peptide_accession_version", StringType(),
                        doc="Mature peptide accession.version, rarely present. Upstream "
                            "column 'mature_peptide_accession.version'."),
            NestedField(15, "mature_peptide_gi", StringType(),
                        doc="GI number of the mature peptide record. Upstream column "
                            "'mature_peptide_gi'."),
            NestedField(16, "symbol", StringType(),
                        doc="Default symbol at the time of the dump. Upstream column "
                            "'Symbol'. gene_info's symbol is authoritative; this one is "
                            "a convenience copy."),
            NestedField(17, "landed_in", StringType(), required=True,
                        doc="The biocOnIce release whose ingest landed these rows."),
        ),
        comment="NCBI gene2accession landed verbatim and whole: every organism and every "
                "accession status, a superset of gene2refseq and by far the largest raw "
                "table here (~1e9 rows upstream), so it lands streamed in batches. NCBI's "
                "'-' placeholder is read as NULL. Positions are strings, unparsed: raw is "
                "landed uninterpreted. Regenerated nightly upstream, so the retrieval "
                "date is the version.",
        properties={"bioc.column.taxon_id.prefix": "ncbitaxon",
                    "bioc.column.gene_id.prefix": "ncbigene"},
    ),
    "raw.ncbi__gene2go": TableDef(
        schema=Schema(
            NestedField(1, "taxon_id", IntegerType(), required=True, doc=TAXON),
            NestedField(2, "gene_id", StringType(), required=True, doc="NCBI Entrez GeneID."),
            NestedField(3, "go_id", StringType(), required=True,
                        doc="GO term id, e.g. GO:0000122. Already CURIE-prefixed by NCBI."),
            NestedField(4, "evidence", StringType(),
                        doc="GO evidence code, e.g. IEA, IDA, TAS. NULL (NCBI's '-') where "
                            "none is recorded."),
            NestedField(5, "qualifier", StringType(),
                        doc="GO relation qualifier, e.g. involved_in, located_in, enables; "
                            "'NOT' prefixes a negated annotation. Pipe-separated where several "
                            "apply, kept as published. NULL (NCBI's '-') where none."),
            NestedField(6, "go_term", StringType(),
                        doc="The GO term's name at retrieval time, e.g. 'nucleus'. A "
                            "convenience denormalised by NCBI — the ontology, not this file, "
                            "is the authority for names."),
            NestedField(7, "pubmed", StringType(),
                        doc="PubMed ids supporting the annotation, pipe-separated in one "
                            "string as NCBI publishes them. NULL (NCBI's '-') where uncited."),
            NestedField(8, "category", StringType(),
                        doc="GO aspect: Function, Process or Component. NCBI's spelling of "
                            "the three GO namespaces."),
            NestedField(9, "landed_in", StringType(), required=True,
                        doc="The biocOnIce release whose ingest landed these rows."),
        ),
        comment="NCBI gene2go landed verbatim and whole: every organism NCBI annotates, not "
                "only the ones we derive annotation for. NCBI's '-' placeholder is read as "
                "NULL; pipe-separated fields are kept as published, unsplit. Regenerated "
                "nightly upstream, so the retrieval date is the version.",
        properties={"bioc.column.taxon_id.prefix": "ncbitaxon",
                    "bioc.column.gene_id.prefix": "ncbigene",
                    "bioc.column.pubmed.prefix": "pubmed"},
    ),
    "annotation.ncbi__gene_go": TableDef(
        schema=Schema(
            NestedField(1, "gene_id", StringType(), required=True,
                        doc="NCBI Entrez GeneID, e.g. 7157. OrgDb's ENTREZID; reach an "
                            "Ensembl gene through annotation.identifier_mapping."),
            NestedField(2, "taxon_id", IntegerType(), required=True, doc=TAXON),
            NestedField(3, "go_id", StringType(), required=True,
                        doc="GO term id, e.g. GO:0000122. DIRECT annotation only — ancestor "
                            "terms (OrgDb's GOALL) need the GO DAG and are not here."),
            NestedField(4, "evidence", StringType(), required=True,
                        doc="GO evidence code, e.g. IEA, IDA, TAS. OrgDb's EVIDENCE. Part of "
                            "the merge key — the same term asserted under two codes is two "
                            "annotations. Empty string where NCBI published '-', never NULL: "
                            "a NULL key never joins to itself and would churn on every merge."),
            NestedField(5, "qualifier", StringType(), required=True,
                        doc="GO relation qualifier, e.g. involved_in, located_in; 'NOT' "
                            "prefixes a negated annotation — dropping it would invert the "
                            "claim, which is why this is part of the merge key. Pipe-separated "
                            "where several apply. Empty string where NCBI published '-', "
                            "never NULL, for the same join reason as evidence."),
            NestedField(6, "go_term", StringType(),
                        doc="The GO term's name as NCBI carried it at retrieval, e.g. "
                            "'nucleus'. Denormalised convenience; the ontology is the "
                            "authority."),
            NestedField(7, "category", StringType(),
                        doc="GO aspect: Function, Process or Component. OrgDb's ONTOLOGY "
                            "column, under NCBI's spelling rather than BP/CC/MF."),
            NestedField(8, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(9, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("gene_id", "taxon_id", "go_id", "evidence", "qualifier"),
        comment="Direct GO annotations per Entrez gene, from NCBI gene2go: OrgDb's GO table. "
                "One row per (gene, term, evidence code, qualifier) per organism. Supporting "
                "PMIDs are not carried — they live unsplit in raw.ncbi__gene2go. The GOALL "
                "closure over ancestor terms is deliberately absent: it depends on a GO DAG "
                "snapshot, which is its own source.",
        properties={"bioc.column.gene_id.prefix": "ncbigene",
                    "bioc.column.taxon_id.prefix": "ncbitaxon",
                    "bioc.column.go_id.prefix": "go"},
    ),
    "raw.icite__metadata": TableDef(
        schema=Schema(
            NestedField(1, "pmid", StringType(), required=True, doc="PubMed id, as text as upstream prints it."),
            NestedField(2, "doi", StringType(), doc="DOI if iCite has one; blank upstream reads as blank here."),
            NestedField(3, "year", StringType(), doc="Publication year."),
            NestedField(4, "title", StringType(), doc="Article title."),
            NestedField(5, "authors", StringType(), doc="Author names, one comma-separated string."),
            NestedField(6, "journal", StringType(), doc="Journal name, ISO abbreviation."),
            NestedField(7, "is_research_article", StringType(), doc="'Yes'/'No': publication types consistent with primary research."),
            NestedField(8, "relative_citation_ratio", StringType(), doc="RCR: field- and time-normalised citation rate, NIH R01 papers = 1.0."),
            NestedField(9, "nih_percentile", StringType(), doc="RCR percentile among NIH-funded papers."),
            NestedField(10, "human", StringType(), doc="Translation module: human fraction, 0-1."),
            NestedField(11, "animal", StringType(), doc="Translation module: animal fraction, 0-1."),
            NestedField(12, "molecular_cellular", StringType(), doc="Translation module: molecular/cellular fraction, 0-1."),
            NestedField(13, "apt", StringType(), doc="Approximate Potential to Translate: predicted probability of citation by a clinical article."),
            NestedField(14, "is_clinical", StringType(), doc="'Yes'/'No': the article itself is clinical."),
            NestedField(15, "citation_count", StringType(), doc="Citations received, per the NIH Open Citation Collection."),
            NestedField(16, "citations_per_year", StringType(), doc="citation_count over years since publication."),
            NestedField(17, "expected_citations_per_year", StringType(), doc="Field-expected citations per year, the RCR denominator."),
            NestedField(18, "field_citation_rate", StringType(), doc="Citation rate of the paper's co-citation network."),
            NestedField(19, "provisional", StringType(), doc="'Yes'/'No': RCR is provisional (paper under two years old)."),
            NestedField(20, "x_coord", StringType(), doc="Translation triangle x coordinate."),
            NestedField(21, "y_coord", StringType(), doc="Translation triangle y coordinate."),
            NestedField(22, "cited_by_clin", StringType(), doc="Space-separated PMIDs of clinical articles citing this one."),
            NestedField(23, "cited_by", StringType(), doc="Space-separated PMIDs citing this one."),
            NestedField(24, "references", StringType(), doc="Space-separated PMIDs this one cites."),
            NestedField(25, "last_modified", StringType(), doc="Upstream last-modified timestamp of the record."),
            NestedField(26, "snapshot", StringType(), required=True, doc="iCite snapshot label, e.g. '2026-08': the monthly Figshare release these values were read from."),
            NestedField(27, "landed_in", StringType(), required=True,
                        doc="The biocOnIce release whose ingest landed these rows."),
        ),
        comment="The NIH iCite database snapshot landed verbatim, every column as text — the "
                "citation lists included. Holds the LATEST snapshot only, a deliberate narrowing "
                "of the raw layer's per-version rule: one snapshot is ~40M rows dominated by "
                "PMID lists, older snapshots stay immutable on Figshare, and every snapshot's "
                "metrics are kept in annotation.icite__metrics. CC BY 4.0; cite iCite.",
        properties={"bioc.column.pmid.prefix": "pubmed"},
    ),
    "annotation.icite__publication": TableDef(
        schema=Schema(
            NestedField(1, "pmid", StringType(), required=True, doc="PubMed id (PMID) of the paper. Part of the merge key."),
            NestedField(2, "doi", StringType(),
                        doc="DOI, normalised: lower-case, no resolver prefix, always '10.<registrant>/<suffix>'. "
                            "NULL when iCite has none or its value is not a DOI (6,414 rows in the 2026-08 "
                            "snapshot); the original text is in raw.icite__metadata.doi. Not unique: 16,521 "
                            "DOIs map to more than one PMID."),
            NestedField(3, "title", StringType(), doc="Article title, whitespace-trimmed."),
            NestedField(4, "authors", StringType(), doc="Author names as one string, as iCite prints them."),
            NestedField(5, "journal", StringType(), doc="Journal name, ISO abbreviation."),
            NestedField(6, "year", IntegerType(), doc="Publication year."),
            NestedField(7, "is_research_article", BooleanType(),
                        doc="Publication types consistent with primary research, per iCite."),
            NestedField(8, "is_clinical", BooleanType(), doc="The article itself is a clinical article, per iCite."),
            NestedField(9, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(10, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("pmid",),
        comment="One row per PubMed record as iCite describes it: what the paper is, not how it "
                "is cited — those numbers move monthly and live in annotation.icite__metrics. "
                "Joins to annotation.ncbi__gene_pubmed on pmid. Source: NIH iCite, CC BY 4.0.",
        properties={"bioc.column.pmid.prefix": "pubmed"},
    ),
    "annotation.icite__metrics": TableDef(
        schema=Schema(
            NestedField(1, "pmid", StringType(), required=True, doc="PubMed id (PMID) of the paper. Part of the merge key."),
            NestedField(2, "snapshot", StringType(), required=True, doc="iCite snapshot label, e.g. '2026-08': the monthly Figshare release these values were read from. Part of the merge key."),
            NestedField(3, "relative_citation_ratio", DoubleType(),
                        doc="RCR: field- and time-normalised citation rate; NIH R01-funded papers average 1.0."),
            NestedField(4, "nih_percentile", DoubleType(), doc="RCR percentile among NIH-funded papers."),
            NestedField(5, "citation_count", LongType(), doc="Citations received per the NIH Open Citation Collection."),
            NestedField(6, "citations_per_year", DoubleType(), doc="citation_count over years since publication."),
            NestedField(7, "expected_citations_per_year", DoubleType(), doc="Field-expected citations per year, the RCR denominator."),
            NestedField(8, "field_citation_rate", DoubleType(), doc="Citation rate of the paper's co-citation network."),
            NestedField(9, "human", DoubleType(), doc="Translation module: human fraction, 0-1."),
            NestedField(10, "animal", DoubleType(), doc="Translation module: animal fraction, 0-1."),
            NestedField(11, "molecular_cellular", DoubleType(), doc="Translation module: molecular/cellular fraction, 0-1."),
            NestedField(12, "apt", DoubleType(),
                        doc="Approximate Potential to Translate: predicted probability of citation by a clinical article."),
            NestedField(13, "provisional", BooleanType(), doc="RCR is provisional (paper under two years old)."),
            NestedField(14, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(15, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("pmid", "snapshot"),
        partition_by=("snapshot",),
        comment="iCite's citation metrics for one paper as of one monthly snapshot. Keyed by "
                "(pmid, snapshot) so a snapshot's numbers are facts that never change — the "
                "alternative, metrics as Type 2 attributes, would open ~40M version rows a month. "
                "For the current view take the latest snapshot; to see a paper's trajectory, "
                "order by snapshot. Source: NIH iCite, CC BY 4.0.",
        properties={"bioc.column.pmid.prefix": "pubmed"},
    ),
    "raw.cellxgene__dataset": TableDef(
        schema=Schema(
            NestedField(1, "dataset_id", StringType(), required=True,
                        doc="CELLxGENE dataset id. Stable across revisions; a revision keeps "
                            "this id and gets a new dataset_version_id."),
            NestedField(2, "dataset_version_id", StringType(), required=True,
                        doc="Id of this specific version of the dataset. What resource.cellxgene__dataset is keyed on."),
            NestedField(3, "collection_id", StringType(), required=True, doc="Id of the collection this dataset belongs to."),
            NestedField(4, "collection_version_id", StringType(), doc="Id of this version of the collection."),
            NestedField(5, "collection_name", StringType(), doc="Collection title, e.g. a study or atlas name."),
            NestedField(6, "collection_doi", StringType(), doc="DOI of the collection's publication, where one exists."),
            NestedField(7, "collection_doi_label", StringType(), doc="Human-readable citation for collection_doi, as CZI formats it."),
            NestedField(8, "title", StringType(), doc="Dataset title."),
            NestedField(9, "citation", StringType(), doc="CZI's own suggested citation string for this dataset version."),
            NestedField(10, "schema_version", StringType(), doc="CELLxGENE schema version the dataset's H5AD conforms to, e.g. '7.1.0'."),
            NestedField(11, "cell_count", LongType(), doc="Total cells in the dataset."),
            NestedField(12, "primary_cell_count", LongType(),
                        doc="Cells flagged is_primary_data = true, CZI's own de-duplication rule (a cell profiled in "
                            "two datasets is primary in exactly one). <= cell_count."),
            NestedField(13, "mean_genes_per_cell", DoubleType(), doc="Mean genes detected per cell."),
            NestedField(14, "published_at", StringType(), doc="ISO 8601 timestamp the dataset was first published, as CZI publishes it. Unparsed."),
            NestedField(15, "revised_at", StringType(), doc="ISO 8601 timestamp of the latest revision; NULL if never revised. Unparsed."),
            NestedField(16, "explorer_url", StringType(), doc="CELLxGENE Explorer URL for interactive browsing."),
            NestedField(17, "processing_status", StringType(), doc="CZI's own pipeline status, e.g. 'SUCCESS'."),
            NestedField(18, "tombstone", BooleanType(),
                        doc="CZI's tombstone flag. Always false in this table in practice: a tombstoned dataset is "
                            "excluded from the PUBLIC listing outright rather than kept with this set, so retirement "
                            "here is via ordinary set-difference against the next crawl, not this column."),
            NestedField(19, "visibility", StringType(), doc="CZI's visibility tag. Always 'PUBLIC': the listing is fetched pre-filtered to it."),
            NestedField(20, "is_pre_analysis", BooleanType(), doc="CZI flag: dataset predates a schema change that added fields later versions carry."),
            NestedField(21, "revision_of_collection", StringType(), doc="Collection id this collection supersedes, where this is a revision. NULL otherwise."),
            NestedField(22, "revision_of_dataset", StringType(), doc="Dataset id this dataset version supersedes, where this is a revision. NULL otherwise."),
            NestedField(23, "x_approximate_distribution", StringType(), doc="Distribution family CZI assumes for X, e.g. 'COUNT', for tools that need it."),
            NestedField(24, "organism", StringType(),
                        doc="[{label, ontology_term_id}, ...] as JSON text, unexploded -- exploding is interpretation, "
                            "done in transform. NCBITaxon terms. Every dataset observed 2026-09-18 carries exactly one."),
            NestedField(25, "assay", StringType(), doc="[{label, ontology_term_id}, ...] as JSON text. EFO terms."),
            NestedField(26, "tissue", StringType(), doc="[{label, ontology_term_id, tissue_type}, ...] as JSON text. UBERON (or CL for cell culture) terms."),
            NestedField(27, "disease", StringType(), doc="[{label, ontology_term_id}, ...] as JSON text. MONDO terms, or PATO:0000461 for 'normal'."),
            NestedField(28, "cell_type", StringType(), doc="[{label, ontology_term_id}, ...] as JSON text. CL terms, or 'unknown'."),
            NestedField(29, "development_stage", StringType(), doc="[{label, ontology_term_id}, ...] as JSON text. HsapDv/MmusDv/UBERON terms depending on organism."),
            NestedField(30, "self_reported_ethnicity", StringType(), doc="[{label, ontology_term_id}, ...] as JSON text. HANCESTRO terms, or 'unknown'/'na'."),
            NestedField(31, "sex", StringType(), doc="[{label, ontology_term_id}, ...] as JSON text. PATO terms, or 'unknown'."),
            NestedField(32, "donor_id", StringType(), doc="[donor id, ...] as JSON text: plain curator-assigned strings, no ontology term."),
            NestedField(33, "suspension_type", StringType(), doc="[suspension type, ...] as JSON text, e.g. 'cell', 'nucleus', 'na'. Plain strings, no ontology term."),
            NestedField(34, "is_primary_data", StringType(), doc="[bool, ...] as JSON text: which is_primary_data values occur among this dataset's cells."),
            NestedField(35, "assets", StringType(),
                        doc="[{filesize, filetype, url}, ...] as JSON text. filetype is 'H5AD' for every dataset observed "
                            "2026-09-18, plus 'ATAC_FRAGMENT'/'ATAC_INDEX' for 21 ATAC datasets. No SpatialData/OME-Zarr "
                            "filetype has been observed; resource.cellxgene__dataset.spatialdata_uri stays NULL until one is."),
            NestedField(36, "spatial", StringType(),
                        doc="{has_fullres, is_single} as JSON text where the dataset is spatial; NULL (JSON null) otherwise. "
                            "Presence, not content, is what resource.cellxgene__dataset.is_spatial reads."),
            NestedField(37, "batch_condition", StringType(), doc="[column name, ...] as JSON text: obs columns CZI suggests batching on, where curated. Often NULL."),
            NestedField(38, "perturbation_types", StringType(), doc="[perturbation type, ...] as JSON text, e.g. 'chemical'. NULL where not a perturbation dataset."),
            NestedField(39, "genetic_perturbation_strategy", StringType(), doc="[strategy, ...] as JSON text. NULL in every dataset observed 2026-09-18."),
            NestedField(40, "retrieval_date", StringType(), required=True,
                        doc="UTC date this crawl of the listing was taken, YYYY-MM-DD. The source's own version: "
                            "the Discover API publishes a live current-state listing, not archived per-release dumps."),
            NestedField(41, "landed_in", StringType(), required=True, doc="The biocOnIce release whose ingest landed these rows."),
        ),
        comment="The CELLxGENE Discover PUBLIC dataset listing landed verbatim: one row per dataset "
                "version. Scalar fields are typed directly; multi-valued and nested fields are kept as "
                "their JSON text, unexploded, per the raw contract. Holds the LATEST crawl only, a "
                "deliberate narrowing of the raw layer's per-version rule (same reason as "
                "raw.icite__metadata): the listing has no archived versions of its own to accumulate. "
                "Licence CC BY 4.0.",
        properties={"bioc.license": "CC-BY-4.0"},
    ),
    "resource.cellxgene__dataset": TableDef(
        schema=Schema(
            NestedField(1, "dataset_id", StringType(), required=True,
                        doc="CELLxGENE dataset id. Stable across revisions; several rows here can share one, each a "
                            "different dataset_version_id."),
            NestedField(2, "dataset_version_id", StringType(), required=True,
                        doc="Id of this specific dataset version. The business key: a revision or a tombstoned "
                            "dataset closes this row (valid_to set) rather than updating it in place."),
            NestedField(3, "collection_id", StringType(), required=True, doc="Id of the collection this dataset belongs to."),
            NestedField(4, "collection_name", StringType(), doc="Collection title, e.g. a study or atlas name."),
            NestedField(5, "collection_doi", StringType(), doc="DOI of the collection's publication, where one exists."),
            NestedField(6, "title", StringType(), doc="Dataset title."),
            NestedField(7, "taxon_id", IntegerType(), required=True,
                        doc="NCBI taxon id, parsed from the listing's organism ontology_term_id "
                            "(always 'NCBITaxon:<id>' in data observed 2026-09-18). A dataset whose organism does not "
                            "parse this way, or that carries more than one organism, fails the ingest loudly rather "
                            "than dropping or guessing (issue #84 acceptance criterion 3)."),
            NestedField(8, "organism_label", StringType(), doc="Organism common/scientific name as CZI labels it, e.g. 'Homo sapiens'."),
            NestedField(9, "assay_term_ids", ListType(element_id=109, element_type=StringType(), element_required=False), doc=MULTI_VALUED + " EFO terms."),
            NestedField(10, "tissue_term_ids", ListType(element_id=110, element_type=StringType(), element_required=False), doc=MULTI_VALUED + " UBERON (or CL) terms."),
            NestedField(11, "disease_term_ids", ListType(element_id=111, element_type=StringType(), element_required=False), doc=MULTI_VALUED + " MONDO terms, or PATO:0000461 for 'normal'."),
            NestedField(12, "cell_type_term_ids", ListType(element_id=112, element_type=StringType(), element_required=False), doc=MULTI_VALUED + " CL terms, or 'unknown'."),
            NestedField(17, "cell_count", LongType(), required=True, doc="Total cells in the dataset."),
            NestedField(18, "primary_cell_count", LongType(), doc="Cells flagged is_primary_data = true. <= cell_count."),
            NestedField(19, "mean_genes_per_cell", DoubleType(), doc="Mean genes detected per cell."),
            NestedField(20, "schema_version", StringType(), doc="CELLxGENE schema version the dataset's H5AD conforms to, e.g. '7.1.0'."),
            NestedField(21, "license", StringType(), required=True, doc="Always 'CC BY 4.0': CELLxGENE Discover's uniform licence for public data."),
            NestedField(22, "h5ad_uri", StringType(), required=True,
                        doc="Public HTTPS URL of the dataset's H5AD, from the listing's assets. Referenced, never "
                            "landed (SPEC.md Large Data Integration). Required: every row has one, or the ingest "
                            "fails (issue #84 acceptance criterion 2)."),
            NestedField(23, "census_release", StringType(), required=True,
                        doc="Census build (e.g. '2025-11-08') this dataset's cells are reachable through, via "
                            "annotation.cellxgene__cell (issue #85) keyed the same way. Defaults to the Census "
                            "release manifest's 'stable' LTS alias at ingest time."),
            NestedField(24, "published_at", StringType(), doc="ISO 8601 timestamp the dataset was first published. Unparsed."),
            NestedField(25, "revised_at", StringType(), doc="ISO 8601 timestamp of the latest revision; NULL if never revised. Unparsed."),
            NestedField(26, "tombstone", BooleanType(), required=True,
                        doc="CZI's tombstone flag, carried through from raw. Always false in practice here: a "
                            "tombstoned dataset is excluded from the PUBLIC listing outright, so its row is closed "
                            "by ordinary retirement (valid_to set) rather than by this flag flipping true."),
            NestedField(27, "is_spatial", BooleanType(), required=True,
                        doc="True for a spatial dataset (the listing's spatial object is non-null: Visium, "
                            "Slide-seq and similar). Issue #84 acceptance criterion 9."),
            NestedField(28, "spatial_platform", StringType(),
                        doc="Spatial platform name, derived from the assay ontology term via a controlled mapping "
                            "(SPATIAL_PLATFORMS in cellxgene.py) -- never free text. NULL for a non-spatial dataset; "
                            "for a spatial one, an assay term absent from the mapping fails the ingest loudly rather "
                            "than becoming NULL (issue #84 acceptance criterion 11)."),
            NestedField(29, "spatialdata_uri", StringType(),
                        doc="URI of the scverse SpatialData (OME-Zarr) store, where the source publishes one. "
                            "Images and transcript-level point clouds live there, referenced, never landed. NULL "
                            "where absent -- as of 2026-09-18 that is every dataset (issue #84 acceptance criterion 10)."),
            NestedField(30, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(31, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("dataset_version_id",),
        comment="One row per CELLxGENE dataset version: what the dataset is, where its H5AD and Census "
                "cells live, never the expression matrix itself. A dataset that disappears from the next "
                "crawl (tombstoned, or superseded by a revision under a new dataset_version_id) is closed "
                "by the ordinary merge set-difference rule; the revision's new dataset_version_id opens a "
                "new row (issue #84 acceptance criterion 5). annotation.cellxgene__gene (Census var, keyed "
                "by feature_id and census_release) and annotation.cellxgene__cell (Census obs, issue #85) "
                "are companions, not here: var ships only inside the TileDB-SOMA store and tiledbsoma is "
                "not an allowed dependency, so that table is deferred until Census publishes var in a "
                "DuckDB-readable form.",
        properties={"bioc.column.taxon_id.prefix": "ncbitaxon",
                    "bioc.license": "CC-BY-4.0"},
    ),
    **{f"raw.obo__{name}": _obo_raw(name, licence) for name, licence in _OBO_ONTOLOGIES},
    "ontology.term": TableDef(
        schema=Schema(
            NestedField(1, "ontology", StringType(), required=True,
                        doc="Short ontology name: cl, uberon, mondo, efo, hsapdv, mmusdv, go, doid. Part "
                            "of the merge key, so one ontology's re-ingest never retires another's "
                            "terms."),
            NestedField(2, "term_id", StringType(), required=True,
                        doc="CURIE, e.g. CL:0000624. Converted from the node's IRI where it follows "
                            "the OBO PURL convention (.../<PREFIX>_<NUMBER>); kept as the full IRI "
                            "verbatim otherwise, which happens for terms imported from a namespace "
                            "that does not follow that convention."),
            NestedField(3, "name", StringType(),
                        doc="rdfs:label. NULL for the handful of imported nodes the file itself "
                            "leaves unlabeled."),
            NestedField(4, "definition", StringType(),
                        doc="Textual definition (IAO:0000115), where the ontology gives one."),
            NestedField(5, "namespace", StringType(),
                        doc="OBO namespace/aspect (oboInOwl:hasOBONamespace) — this is GO's "
                            "biological_process / molecular_function / cellular_component. NULL "
                            "where the file does not tag it, which most CL/UBERON/MONDO terms do "
                            "not."),
            NestedField(6, "synonyms", StringType(),
                        doc="Every oboInOwl synonym (exact, narrow, broad and related alike — the "
                            "distinction between them is not kept), '|'-joined into one string. "
                            "NULL where the term has none."),
            NestedField(7, "obsolete", BooleanType(), required=True,
                        doc="True if the ontology marks this term deprecated. Obsolete terms are "
                            "kept, never dropped — see replaced_by."),
            NestedField(8, "replaced_by", StringType(),
                        doc="CURIE of the successor term (IAO:0100001 'term replaced by'), where "
                            "the ontology names exactly one. NULL for a current term, or for an "
                            "obsolete term the ontology leaves without a single successor — some "
                            "only list 'consider' candidates, which are not a replacement and are "
                            "not carried here."),
            NestedField(9, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(10, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("ontology", "term_id"),
        partition_by=("ontology",),
        comment="Ontology terms, stacked across ontologies: one current row per (ontology, "
                "term_id), Type 2 on any attribute change. Includes every node the release file "
                "carries, imported classes included — see raw.obo__<ontology>.",
    ),
    "ontology.relationship": TableDef(
        schema=Schema(
            NestedField(1, "ontology", StringType(), required=True,
                        doc="Short ontology name, same vocabulary as ontology.term.ontology. Part "
                            "of the merge key."),
            NestedField(2, "subject_id", StringType(), required=True,
                        doc="CURIE of the subject term, same normalisation as ontology.term.term_id."),
            NestedField(3, "predicate", StringType(), required=True,
                        doc="'is_a' verbatim; a short name for the handful of BFO/RO relations "
                            "common to every OBO ontology (part_of, has_part, develops_from, "
                            "regulates, negatively_regulates, positively_regulates); the relation's "
                            "own CURIE for anything else. Nothing is invented: an unmapped relation "
                            "is exactly the CURIE the file states, never guessed at."),
            NestedField(4, "object_id", StringType(), required=True,
                        doc="CURIE of the object term, same normalisation as ontology.term.term_id."),
            NestedField(5, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(6, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("ontology", "subject_id", "predicate", "object_id"),
        partition_by=("ontology",),
        comment="Edges between ontology terms: one row per (ontology, subject_id, predicate, "
                "object_id). The is_a closure (e.g. CL:0000624 -> CL:0000084 -> CL:0000000) is a "
                "recursive CTE over predicate = 'is_a' here, not precomputed. An edge's endpoints "
                "are not required to resolve to a row in ontology.term — no foreign key is "
                "enforced, matching every other table in this catalog.",
    ),
    "raw.bedbase__bed": TableDef(
        schema=Schema(
            NestedField(1, "id", StringType(), required=True,
                        doc="BEDbase's own id for this BED file record, e.g. "
                            "'0000120fe8c5334bb0ce759dfcf06c3b'. Stable; carried downstream "
                            "as resource_id."),
            NestedField(2, "name", StringType(), doc="Record name/title as BEDbase prints it."),
            NestedField(3, "description", StringType(),
                        doc="Record description as BEDbase prints it. Frequently blank."),
            NestedField(4, "genome_alias", StringType(),
                        doc="Free-text genome label as submitted, e.g. 'hg38'. NOT a reliable "
                            "key: BEDbase's 118 genome labels include mismatched and compound "
                            "strings — an Arabidopsis (taxon 3702) record was found tagged "
                            "'hg18' (verified live 2026-09-17). genome_digest is the key; this "
                            "is a display label only."),
            NestedField(5, "genome_digest", StringType(),
                        doc="Sequence-collection digest identifying the exact assembly. NULL "
                            "for a meaningful share of records (~5% in a 500-record sample "
                            "verified live 2026-09-17) where BEDbase could not resolve one. "
                            "The reliable genome key when present; genome_alias never is."),
            NestedField(6, "bed_compliance", StringType(),
                        doc="BED-standard compliance class BEDbase assigned, e.g. 'bed6+4'."),
            NestedField(7, "data_format", StringType(),
                        doc="Upstream data format BEDbase detected, e.g. 'encode_narrowpeak_rs'."),
            NestedField(8, "compliant_columns", IntegerType(),
                        doc="Columns conforming to the declared bed_compliance's core spec."),
            NestedField(9, "non_compliant_columns", IntegerType(),
                        doc="Columns beyond bed_compliance's core spec."),
            NestedField(10, "is_universe", BooleanType(),
                        doc="True for BEDbase's small curated 'universe' region sets, false "
                            "for an ordinary submitted BED file."),
            NestedField(11, "license_id", StringType(),
                        doc="Licence as a DUO (Data Use Ontology) code, e.g. 'DUO:0000042' "
                            "(general research use). Carried on every row: we reference these "
                            "files, we do not redistribute them, so landing the licence is what "
                            "makes that honest."),
            NestedField(12, "processed", BooleanType(),
                        doc="Whether BEDbase's bedstat/bedboss pipeline finished processing "
                            "this record; false records may lack stats even at the full-detail "
                            "endpoint."),
            NestedField(13, "submission_date", StringType(),
                        doc="ISO 8601 timestamp this record was submitted, kept as upstream "
                            "prints it, unparsed."),
            NestedField(14, "last_update_date", StringType(),
                        doc="ISO 8601 timestamp this record last changed, kept as upstream "
                            "prints it, unparsed."),
            NestedField(15, "annotation_organism", StringType(),
                        doc="annotation.organism as BEDbase prints it, e.g. 'Homo sapiens'. "
                            "Free text; annotation_species_id is the reliable taxon join."),
            NestedField(16, "annotation_species_id", StringType(),
                        doc="annotation.species_id as BEDbase prints it — usually a single "
                            "NCBI taxon id as text, e.g. '9606', but not always: a co-infection "
                            "study was found carrying '9606, 11676' (verified live "
                            "2026-09-17). resource.bedbase__bedfile.taxon_id is a TRY_CAST of "
                            "this column, NULL where it does not parse as one integer."),
            NestedField(17, "annotation_genotype", StringType(), doc="annotation.genotype as published."),
            NestedField(18, "annotation_phenotype", StringType(), doc="annotation.phenotype as published."),
            NestedField(19, "annotation_description", StringType(),
                        doc="annotation.description — distinct from the record's own top-level "
                            "`description` above. Frequently blank."),
            NestedField(20, "annotation_cell_type", StringType(), doc="annotation.cell_type as published."),
            NestedField(21, "annotation_cell_line", StringType(), doc="annotation.cell_line as published."),
            NestedField(22, "annotation_tissue", StringType(), doc="annotation.tissue as published."),
            NestedField(23, "annotation_library_source", StringType(),
                        doc="annotation.library_source as published, e.g. 'genomic'."),
            NestedField(24, "annotation_assay", StringType(),
                        doc="Assay type, e.g. 'ATAC-seq', 'DNase-seq', 'PRO-cap'."),
            NestedField(25, "annotation_antibody", StringType(), doc="annotation.antibody as published."),
            NestedField(26, "annotation_target", StringType(), doc="annotation.target as published."),
            NestedField(27, "annotation_treatment", StringType(), doc="annotation.treatment as published."),
            NestedField(28, "annotation_global_sample_id", StringType(),
                        doc="GEO/ENCODE sample ids, pipe-separated in one string as BEDbase's "
                            "list is joined, e.g. 'geo:gsm4837486'. Pipe-joined per the "
                            "unsplit-list convention elsewhere in this catalog "
                            "(ncbi__gene_info.synonyms); a join key into Milestone 3's "
                            "experimental metadata."),
            NestedField(29, "annotation_global_experiment_id", StringType(),
                        doc="GEO/ENCODE experiment ids, pipe-separated, e.g. 'geo:gse159673'. "
                            "Same convention as annotation_global_sample_id."),
            NestedField(30, "annotation_original_file_name", StringType(),
                        doc="Original filename as submitted upstream, e.g. "
                            "'GSM4837486_Plasma_B2_T1.ATACseq.narrowPeak.gz'."),
            NestedField(31, "landed_in", StringType(), required=True,
                        doc="The biocOnIce release whose ingest landed these rows."),
        ),
        comment="BEDbase's /v1/bed/list landed verbatim and whole, one row per BED file "
                "record — every field the listing endpoint returns, its nested `annotation` "
                "object flattened with an `annotation_` prefix. Per-file DETAIL (URIs, "
                "checksums, stats) is deliberately NOT landed here: /v1/bed/{id}/metadata"
                "?full=true is one HTTP request per record, 663,721 of them, left to a "
                "follow-up (issue #79) rather than paid for on every crawl. BEDbase has no "
                "release cadence, so this table is replaced wholesale each ingest and the "
                "manifest records retrieval_date as the version.",
        properties={"bioc.column.annotation_species_id.prefix": "ncbitaxon",
                    "bioc.column.license_id.prefix": "duo"},
    ),
    "raw.bedbase__bedset": TableDef(
        schema=Schema(
            NestedField(1, "id", StringType(), required=True,
                        doc="BEDbase's own id for this bedset, e.g. 'gse33600' — often a GEO "
                            "series accession, but not guaranteed to be one."),
            NestedField(2, "name", StringType(), doc="Bedset name as BEDbase prints it."),
            NestedField(3, "md5sum", StringType(),
                        doc="BEDbase's own MD5 digest of the bedset's metadata — not a digest "
                            "of any file."),
            NestedField(4, "submission_date", StringType(),
                        doc="ISO 8601 timestamp, kept as upstream prints it, unparsed."),
            NestedField(5, "last_update_date", StringType(),
                        doc="ISO 8601 timestamp, kept as upstream prints it, unparsed."),
            NestedField(6, "description", StringType(),
                        doc="Bedset description, Markdown text as BEDbase prints it."),
            NestedField(7, "bedfile_count", IntegerType(),
                        doc="Member BED files, per BEDbase's own count. Membership itself "
                            "(bed_ids) is NOT landed here: it is null on this listing endpoint "
                            "and needs a per-bedset detail request, 22,189 of them, deferred "
                            "alongside the per-file detail issue #79 leaves for a follow-up."),
            NestedField(8, "author", StringType(), doc="Curator/submitter name as published."),
            NestedField(9, "bedset_source", StringType(),
                        doc="BEDbase's own 'source' field on a bedset, e.g. 'gse33600' "
                            "(frequently identical to `id`). Renamed from upstream's `source` "
                            "here and on resource.bedbase__bedset because `source` elsewhere in "
                            "this catalog names the asserting annotation provider — the same "
                            "rename BugSigDB's `source_in_paper` makes, and for the same reason."),
            NestedField(10, "landed_in", StringType(), required=True,
                        doc="The biocOnIce release whose ingest landed these rows."),
        ),
        comment="BEDbase's /v1/bedset/list landed verbatim and whole, one row per bedset. "
                "Statistics, plots and membership (bed_ids) are null on this listing endpoint "
                "and require a per-bedset detail request (22,189 of them) — deferred alongside "
                "the per-file detail issue #79 leaves for a follow-up. Replaced wholesale each "
                "ingest; retrieval_date is the version, per BEDbase's lack of a release cadence.",
        properties={},
    ),
    "resource.bedbase__bedfile": TableDef(
        schema=Schema(
            NestedField(1, "resource_id", StringType(), required=True,
                        doc="BEDbase's bed id (raw.bedbase__bed.id). Business key — one live "
                            "row per file, Type 2 on any attribute change."),
            NestedField(2, "title", StringType(), doc="Record name/title, from raw.bedbase__bed.name."),
            NestedField(3, "description", StringType(), doc="Record description, from raw.bedbase__bed.description."),
            NestedField(4, "genome_digest", StringType(),
                        doc="Sequence-collection digest of the assembly this file's regions "
                            "are called against — the reliable genome key; NULL where BEDbase "
                            "could not resolve one. Joins to reference.genome once we compute "
                            "sequence-collection digests for our own assemblies (currently "
                            "keyed by INSDC accession only)."),
            NestedField(5, "genome_alias", StringType(),
                        doc="Free-text genome label, display only — never a join key. See "
                            "raw.bedbase__bed.genome_alias for how messy this gets."),
            NestedField(6, "taxon_id", IntegerType(),
                        doc="NCBI taxon id, a TRY_CAST of raw.bedbase__bed.annotation_species_id; "
                            "NULL where that text does not parse as a single integer (a "
                            "co-infection study carrying more than one id as free text is the "
                            "known case — see that column's doc)."),
            NestedField(7, "organism", StringType(),
                        doc="Free-text organism name as BEDbase prints it; taxon_id is the "
                            "reliable join."),
            NestedField(8, "assay", StringType(), doc="Assay type, e.g. 'ATAC-seq', 'DNase-seq'."),
            NestedField(9, "target", StringType(), doc="ChIP/CUT&RUN target, where applicable."),
            NestedField(10, "antibody", StringType(), doc="Antibody used, where applicable."),
            NestedField(11, "cell_type", StringType(), doc="Cell type sampled."),
            NestedField(12, "cell_line", StringType(), doc="Cell line sampled, where applicable."),
            NestedField(13, "tissue", StringType(), doc="Tissue sampled."),
            NestedField(14, "treatment", StringType(), doc="Treatment applied to the sample, free text."),
            NestedField(15, "sample_id", ListType(element_id=115, element_type=StringType(), element_required=False),
                        doc="GEO/ENCODE sample ids, sorted list, e.g. ['geo:gsm4837486']. The joinable form is "
                            "resource.resource_relationship (derived_from_sample); a join key into Milestone 3's "
                            "experimental metadata."),
            NestedField(16, "experiment_id", ListType(element_id=116, element_type=StringType(), element_required=False),
                        doc="GEO/ENCODE experiment ids, sorted list, e.g. ['geo:gse159673']. Joinable form: "
                            "resource.resource_relationship (derived_from_experiment)."),
            NestedField(17, "compliance", StringType(), doc="BED-standard compliance class, e.g. 'bed6+4'."),
            NestedField(18, "format", StringType(), doc="Upstream data format BEDbase detected."),
            NestedField(19, "license_id", StringType(),
                        doc="Licence as a DUO code, carried on every row — we reference this "
                            "file, we do not redistribute it, and this is what makes that "
                            "honest. See raw.bedbase__bed.license_id."),
            NestedField(20, "provider", StringType(), required=True,
                        doc="Constant 'BEDbase': the catalog this resource entry was read "
                            "from, per SPEC's Resource Metadata Layer."),
            NestedField(21, "submitted", StringType(), doc="Submission timestamp, ISO 8601 text as published."),
            NestedField(22, "updated", StringType(), doc="Last-update timestamp, ISO 8601 text as published."),
            NestedField(23, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(24, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("resource_id",),
        comment="One row per BEDbase BED file, Type 2 by resource_id: title, genome, "
                "organism/assay-level annotation and licence, from /v1/bed/list. Objects are "
                "REFERENCED here, never ingested: size, checksum and the http/s3/bigbed URIs "
                "live at /v1/bed/{id}/metadata?full=true, one request per record (663,721 of "
                "them), deferred to a follow-up (issue #79) and to be added by schema "
                "evolution when they land — a column that is always NULL advertises a "
                "capability this table does not yet have. Key genomes on genome_digest, never "
                "genome_alias — see that column's doc. license_id (a DUO code) is carried on "
                "every row because we reference these files rather than redistribute them.",
        properties={"bioc.column.taxon_id.prefix": "ncbitaxon",
                    "bioc.column.license_id.prefix": "duo"},
    ),
    "resource.bedbase__bedset": TableDef(
        schema=Schema(
            NestedField(1, "resource_id", StringType(), required=True,
                        doc="BEDbase's bedset id (raw.bedbase__bedset.id). Business key — one "
                            "live row per bedset, Type 2 on any attribute change."),
            NestedField(2, "title", StringType(), doc="Bedset name, from raw.bedbase__bedset.name."),
            NestedField(3, "description", StringType(), doc="Bedset description, Markdown text as published."),
            NestedField(4, "bedfile_count", IntegerType(), doc="Member BED files, per BEDbase's own count."),
            NestedField(5, "author", StringType(), doc="Curator/submitter name as published."),
            NestedField(6, "bedset_source", StringType(),
                        doc="BEDbase's own 'source' field on this bedset — see "
                            "raw.bedbase__bedset.bedset_source for the rename."),
            NestedField(7, "provider", StringType(), required=True,
                        doc="Constant 'BEDbase': the catalog this resource entry was read "
                            "from, per SPEC's Resource Metadata Layer."),
            NestedField(8, "submitted", StringType(), doc="Submission timestamp, ISO 8601 text as published."),
            NestedField(9, "updated", StringType(), doc="Last-update timestamp, ISO 8601 text as published."),
            NestedField(10, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(11, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("resource_id",),
        comment="One row per BEDbase bedset, Type 2 by resource_id, from /v1/bedset/list. "
                "Membership (which BED files belong to a bedset) is deliberately NOT here: "
                "bed_ids is null on the listing endpoint and needs a per-bedset detail "
                "request (22,189 of them) — deferred alongside the per-file detail issue #79 "
                "leaves for a follow-up, to land as a resource_relationship-style membership "
                "table when it does.",
        properties={},
    ),
    "annotation.icite__citation": TableDef(
        schema=Schema(
            NestedField(1, "citing_pmid", StringType(), required=True,
                        doc="PubMed id of the paper that cites. Part of the merge key."),
            NestedField(2, "cited_pmid", StringType(), required=True,
                        doc="PubMed id of the paper cited. Part of the merge key."),
            NestedField(3, "shard", IntegerType(), required=True,
                        doc="cited_pmid modulo 16: the unit this table is merged and partitioned in, "
                            "so ~930M edges never sit in memory at once. Implementation detail, "
                            "safe to ignore; it exists so the table's write path is honest."),
            NestedField(4, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(5, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("citing_pmid", "cited_pmid"),
        partition_by=("shard",),
        comment="The PubMed citation graph as iCite publishes it: one row per (citing, cited) "
                "pair: the NIH Open Citation Collection exactly, via the references lists in "
                "raw.icite__metadata (928,458,585 edges in the 2026-08 snapshot, equal to the OCC file). An edge has no attributes, so it is only ever "
                "asserted or withdrawn: valid_from is the release it appeared in, valid_to the "
                "release it vanished. 'Who cites X' is WHERE cited_pmid = X AND valid_to IS NULL. "
                "Source: NIH iCite / NIH-OCC, CC BY 4.0.",
        properties={"bioc.column.citing_pmid.prefix": "pubmed",
                    "bioc.column.cited_pmid.prefix": "pubmed"},
    ),
    **{f"raw.pubtator3__{kind}": TableDef(
        schema=Schema(
            NestedField(1, "pmid", StringType(), required=True,
                        doc="PubMed id, as text as upstream prints it."),
            NestedField(2, "type", StringType(), required=True,
                        doc=f"Entity type. '{kind.capitalize()}' on every row of this file."),
            NestedField(3, "concept_id", StringType(),
                        doc="Normalised identifier, verbatim: an NCBI Gene id, 'MESH:D…', an NCBI "
                            "Taxonomy id, an rs id or a structured 'tmVar:…;HGVS:…' string. "
                            "';'-joined where one mention resolves to several ids. A literal '-' "
                            "is upstream's own value for 'recognised but not normalised' (10.7M "
                            "chemical rows) and is kept as text, not read as NULL."),
            NestedField(4, "mentions", StringType(),
                        doc="The surface strings found in the paper for this concept, '|'-joined, "
                            "as the tagger saw them (case variants, stray punctuation and quotes "
                            "included). NULL where upstream leaves it empty: rows asserted by a "
                            "curated resource with no text hit."),
            NestedField(5, "resource", StringType(),
                        doc="Who asserts the row, '|'-joined in no stable order: 'PubTator3' for a "
                            "text-mined hit, otherwise the curated source it was taken from (MESH, "
                            "gene2pubmed, generifs_basic, CTD, BioGRID, ClinVar, dbSNP, RGD, …)."),
            NestedField(6, "landed_in", StringType(), required=True,
                        doc="The biocOnIce release whose ingest landed these rows."),
        ),
        comment=f"PubTator3's {kind}2pubtator3.gz landed verbatim and whole: one row per (PMID, "
                f"concept) the dump carries, five headerless tab-separated columns. Holds the "
                f"LATEST dump only — upstream regenerates the file in place about monthly and "
                f"archives none. Query annotation.pubtator3__mention instead; read this for the "
                f"mention strings, which are deliberately not derived. NCBI public domain "
                f"(US Government Work, per the directory README); cite Wei et al. 2024, PubTator 3.0.",
        properties={"bioc.column.pmid.prefix": "pubmed", "bioc.license": "public-domain"},
    ) for kind in ("gene", "disease", "chemical", "species", "mutation")},
    "annotation.pubtator3__mention": TableDef(
        schema=Schema(
            NestedField(1, "pmid", StringType(), required=True,
                        doc="PubMed id of the paper; joins to annotation.icite__publication.pmid "
                            "and both ends of annotation.icite__citation. Part of the merge key."),
            NestedField(2, "concept_type", StringType(), required=True,
                        doc="'Gene', 'Disease', 'Chemical', 'Species' or 'Mutation', as upstream "
                            "spells it. In the key because id spaces overlap: '9606' is a taxon "
                            "under Species and a gene under Gene. Part of the merge key."),
            NestedField(3, "concept_id", StringType(), required=True,
                        doc="One identifier per row. Gene: NCBI Gene id (joins to "
                            "annotation.ncbi__gene.gene_id). Species: NCBI Taxonomy id. Disease and "
                            "Chemical: 'MESH:D…'/'MESH:C…' (a few OMIM). Mutation: an rs id, or "
                            "tmVar's structured string kept whole — its 'VariantGroup:n' part "
                            "numbers variants within one paper and is not an identifier. Upstream's "
                            "';'-joined multi-id values are split, except under Mutation where ';' "
                            "is part of the id. Part of the merge key."),
            NestedField(4, "resource", StringType(), required=True,
                        doc="Who asserts the link, one per row: 'PubTator3' for a text-mined hit, "
                            "otherwise a curated source (MESH, gene2pubmed, generifs_basic, CTD, "
                            "BioGRID, ClinVar, dbSNP, …). Filter resource <> 'PubTator3' for curated "
                            "links only. Part of the merge key."),
            NestedField(5, "shard", IntegerType(), required=True,
                        doc="pmid modulo 16: the unit this table is merged and partitioned in, so "
                            "~460M rows never sit in one merge. Implementation detail, safe to ignore."),
            NestedField(6, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(7, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("pmid", "concept_type", "concept_id", "resource"),
        partition_by=("shard",),
        comment="Which genes, diseases, chemicals, species and variants each PubMed paper mentions, "
                "per PubTator3 (NCBI's text mining over all of PubMed and PMC open access) and the "
                "curated sources it folds in: one row per (paper, concept, asserting resource). "
                "Machine annotation — expect false positives, far broader than ncbi__gene_pubmed. "
                "No attributes, so a row is only ever asserted or withdrawn. Not derived: rows "
                "PubTator3 could not normalise (concept id '-'), and the mention strings, which "
                "churn between dumps; both stay in raw.pubtator3__*. 'Papers about TP53' is WHERE "
                "concept_type = 'Gene' AND concept_id = '7157' AND valid_to IS NULL. NCBI public domain.",
        properties={"bioc.column.pmid.prefix": "pubmed", "bioc.license": "public-domain"},
    ),
    "resource.resource_relationship": TableDef(
        schema=Schema(
            NestedField(1, "resource_id", StringType(), required=True,
                        doc="The resource this row is about: resource.cellxgene__dataset.dataset_version_id, "
                            "resource.bedbase__bedfile.resource_id, ... Part of the merge key."),
            NestedField(2, "relationship", StringType(), required=True,
                        doc="What the target is to the resource: has_assay, has_tissue, has_disease, "
                            "has_cell_type (CELLxGENE; eQTL Catalogue the last two; targets are ontology term ids); derived_from_sample, "
                            "derived_from_experiment (BEDbase, targets are 'geo:gsm…'-style accessions). "
                            "has_cell_type (CELLxGENE, targets are ontology term ids); derived_from_sample, "
                            "derived_from_experiment (BEDbase, targets are 'geo:gsm…'-style accessions); "
                            "part_of_dataset, has_biosample, has_target_gene (ENCODE, targets are 'encode:ENCSR…', "
                            "an ontology term id, 'ncbigene:<Entrez id>'). "
                            "Part of the merge key."),
            NestedField(3, "target_id", StringType(), required=True,
                        doc="The related thing, as a CURIE or accession: joins to ontology.term.term_id when it "
                            "is an ontology term, and through ontology.relationship for rollups. Part of the merge key."),
            NestedField(4, "source", StringType(), required=True,
                        doc="The writer that asserted this row ('cellxgene', 'bedbase', 'eqtlcatalogue', 'encode'): "
                            "its merge scope, so one "
                            "catalog's re-ingest never retires another's rows (ADR-0004). Part of the merge key."),
            NestedField(5, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(6, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("resource_id", "relationship", "target_id", "source"),
        partition_by=("source",),
        comment="SPEC.md's resource_relationship: one row per (resource, relationship, target). The "
                "joinable form of every multi-valued field on a resource catalog row — the "
                "'which datasets have cell type X in tissue Y' question is a join here, and an ontology "
                "rollup is a join through ontology.relationship. A relationship has no attributes, so a "
                "row is only ever asserted or withdrawn.",
    ),
    # The two raw ENCODE tables are report.tsv columns, all text: (name, doc) pairs in
    # request order, names being encode.EXPERIMENT_FIELDS / FILE_FIELDS with '.' -> '_'.
    "raw.encode__experiment": TableDef(
        schema=Schema(*(NestedField(i, n, StringType(), doc=d,
                                    required=n in {"accession", "retrieval_date", "landed_in"})
                        for i, (n, d) in enumerate((
            ("accession", "ENCODE experiment accession, e.g. 'ENCSR000AKS'. Experiments share the ENCSR "
                          "prefix with every other dataset type (annotations, references, series)."),
            ("uuid", "The portal's internal UUID for the object."),
            ("status", "'released', 'archived' or 'revoked' — everything an anonymous request can see. "
                       "Landed whole: not filtered to released."),
            ("date_created", "ISO 8601 timestamp the object was created on the portal."),
            ("date_submitted", "YYYY-MM-DD the lab submitted the experiment. NULL where never recorded."),
            ("date_released", "YYYY-MM-DD of public release."),
            ("description", "Submitter's free-text description. Whitespace runs are collapsed to one space "
                            "by the portal's TSV writer; quote characters are literal."),
            ("assay_term_id", "Assay ontology id, e.g. 'OBI:0000716' (ChIP-seq); 'NTR:…' is an ENCODE "
                              "new-term-request placeholder, not a published term."),
            ("assay_term_name", "Assay ontology label, e.g. 'ChIP-seq'."),
            ("assay_title", "ENCODE's display refinement of the assay, e.g. 'TF ChIP-seq', 'Histone ChIP-seq'."),
            ("assay_slims", "Assay category slims, ','-joined, e.g. 'DNA binding'."),
            ("biosample_ontology_term_id", "Biosample type as an ontology CURIE: UBERON (tissue), CL (cell type), "
                                           "EFO or CLO (cell line), or an 'NTR:' placeholder."),
            ("biosample_ontology_term_name", "Label of biosample_ontology_term_id, e.g. 'K562', 'liver'."),
            ("biosample_ontology_classification", "'tissue', 'cell line', 'primary cell', 'in vitro differentiated "
                                                  "cells', 'organoid', 'whole organisms', ..."),
            ("biosample_ontology_organ_slims", "Organ slims of the biosample term, ','-joined."),
            ("biosample_ontology_cell_slims", "Cell slims of the biosample term, ','-joined."),
            ("biosample_summary", "ENCODE's generated one-line biosample description, including organism, "
                                  "treatments and modifications."),
            ("replicates_library_biosample_organism_scientific_name",
             "Organism of the replicates' biosamples, e.g. 'Homo sapiens'; ','-joined if replicates differ. "
             "NULL for the experiments with no replicate."),
            ("replicates_library_biosample_organism_taxon_id",
             "NCBI taxonomy id of the same, as text; ','-joined if replicates differ."),
            ("target_name", "Target as ENCODE names it, '<label>-<organism>', e.g. 'CTCF-human'. NULL for "
                            "assays without a target."),
            ("target_label", "Target label, e.g. 'CTCF', 'H3K27ac'."),
            ("target_investigated_as", "Target categories, ','-joined, e.g. 'histone,narrow histone mark'."),
            ("target_genes_geneid", "Entrez GeneIDs of the target's genes, ','-joined — three for a histone "
                                    "mark. NULL where the target has no gene (a synthetic tag, say)."),
            ("target_genes_symbol", "Symbols of the same genes, ','-joined, in the same order."),
            ("control_type", "Set on control experiments, e.g. 'input library'. NULL otherwise."),
            ("perturbed", "'True' or 'False': whether the biosample was treated or genetically modified."),
            ("lab_title", "Submitting lab, e.g. 'Michael Snyder, Stanford'."),
            ("award_name", "Grant number, e.g. 'U54HG006996'."),
            ("award_project", "Funding project: 'ENCODE', 'Roadmap', 'modENCODE', 'modERN', 'GGR', ..."),
            ("award_rfa", "Project phase, e.g. 'ENCODE4'."),
            ("assembly", "Assembly labels of the experiment's processed files, ','-joined, as the portal "
                         "spells them ('GRCh38', 'hg19', 'mm10-minimal')."),
            ("replication_type", "'isogenic', 'anisogenic', 'unreplicated', ..."),
            ("bio_replicate_count", "Number of biological replicates, as text."),
            ("tech_replicate_count", "Number of technical replicates, as text."),
            ("dbxrefs", "External cross-references, ','-joined CURIE-like strings, e.g. 'GEO:GSE30263'."),
            ("doi", "The experiment's DOI, e.g. '10.17989/ENCSR000AKS'."),
            ("internal_tags", "ENCODE collection tags, ','-joined, e.g. 'ENCYCLOPEDIAv5'."),
            ("alternate_accessions", "Accessions merged into this one, ','-joined."),
            ("supersedes", "Object paths of the experiments this one replaces, ','-joined."),
            ("superseded_by", "Object paths of the experiments that replace this one, ','-joined."),
            ("possible_controls", "Object paths of this experiment's control experiments, ','-joined."),
            ("retrieval_date", "UTC date this report was downloaded, YYYY-MM-DD. The source's own version: "
                               "the portal is a live inventory with no release number."),
            ("landed_in", "The biocOnIce release whose ingest landed these rows."),
        ), 1))),
        comment="The ENCODE portal's Experiment inventory landed verbatim from report.tsv: one row per "
                "experiment, every status, every assay and organism. All text; list-valued fields are "
                "','-joined as the portal writes them. The columns are the explicit field list "
                "encode.EXPERIMENT_FIELDS, a declared subset of the Experiment object. Holds the LATEST "
                "crawl only, as raw.cellxgene__dataset does. ENCODE data are free of use restrictions; the "
                "ENCODE Consortium asks to be cited: https://www.encodeproject.org/help/citing-encode/",
    ),
    "raw.encode__file": TableDef(
        schema=Schema(*(NestedField(i, n, StringType(), doc=d,
                                    required=n in {"title", "retrieval_date", "landed_in"})
                        for i, (n, d) in enumerate((
            ("accession", "ENCODE file accession, e.g. 'ENCFF002FAS'. NULL for the 1,283 files (2026-09-18) "
                          "that have only an external_accession."),
            ("external_accession", "Another archive's accession or a name, for a file ENCODE gave no "
                                   "accession: SRA runs ('SRR1270455'), named reference files "
                                   "('GRCh38_EBV.chrom.sizes'). NULL otherwise."),
            ("title", "accession, or external_accession where there is none: what the portal keys the file "
                      "by ('/files/<title>/'). Never NULL."),
            ("uuid", "The portal's internal UUID for the object."),
            ("status", "'released', 'archived' or 'revoked'. Landed whole: not filtered to released."),
            ("dataset", "Object path of the dataset the file belongs to: '/experiments/ENCSR…/', "
                        "'/annotations/ENCSR…/', '/references/ENCSR…/', ... Only the first kind is in "
                        "raw.encode__experiment."),
            ("file_format", "'bed', 'bigWig', 'fastq', 'bam', 'bigBed', 'tsv', 'tar', ..."),
            ("file_format_type", "Sub-format, mostly for bed/bigBed: 'narrowPeak', 'bed3+', 'idr_thresholded_peak', ..."),
            ("file_type", "file_format and file_format_type together, e.g. 'bed narrowPeak'."),
            ("output_type", "What the file holds, e.g. 'reads', 'alignments', 'IDR thresholded peaks'."),
            ("output_category", "Coarse class of output_type: 'raw data', 'alignment', 'signal', 'annotation', ..."),
            ("assembly", "Genome assembly label as the portal spells it: 'GRCh38', 'hg19', 'mm10', "
                         "'mm10-minimal', 'dm6', 'ce11', ... NULL for unaligned data. A label, not an "
                         "assembly key."),
            ("genome_annotation", "Gene annotation version used, e.g. 'V29' (GENCODE), 'M21'. NULL where none."),
            ("file_size", "Size in bytes, as text."),
            ("md5sum", "MD5 of the file as stored (compressed, where it is)."),
            ("content_md5sum", "MD5 of the uncompressed content, where the portal computed one."),
            ("href", "Download path relative to https://www.encodeproject.org, "
                     "'/files/<acc>/@@download/<acc>.<ext>'; it redirects to cloud_metadata_url."),
            ("cloud_metadata_url", "Direct HTTPS URL of the object in the public encode-public S3 bucket."),
            ("s3_uri", "The same object as an s3:// URI."),
            ("no_file_available", "'True' where ENCODE holds metadata for a file it does not host."),
            ("restricted", "'True' where the file is access-restricted (no public URL). NULL is unrestricted."),
            ("derived_from", "Object paths of the files this one was computed from, ','-joined. Mostly "
                             "'/files/ENCFF…/', occasionally a named reference file."),
            ("biological_replicates", "Biological replicate numbers the file covers, ','-joined."),
            ("technical_replicates", "Technical replicates as '<bio>_<tech>', ','-joined."),
            ("preferred_default", "'True' where ENCODE marks the file as the default one to use for its dataset."),
            ("processed", "'True' for pipeline output, 'False' for submitted raw data."),
            ("run_type", "Sequencing run type for reads: 'single-ended' or 'paired-ended'."),
            ("read_length", "Read length for reads, as text."),
            ("paired_end", "'1' or '2' for one end of a paired-end fastq."),
            ("paired_with", "Object path of the mate fastq."),
            ("date_created", "ISO 8601 timestamp the file object was created."),
            ("lab_title", "Lab that produced the file; 'ENCODE Processing Pipeline' for uniform processing."),
            ("award_project", "Funding project, as raw.encode__experiment.award_project."),
            ("award_rfa", "Project phase, e.g. 'ENCODE4'."),
            ("alternate_accessions", "Accessions merged into this one, ','-joined."),
            ("superseded_by", "Object paths of the files that replace this one, ','-joined."),
            ("retrieval_date", "UTC date this report was downloaded, YYYY-MM-DD. The source's own version."),
            ("landed_in", "The biocOnIce release whose ingest landed these rows."),
        ), 1))),
        comment="The ENCODE portal's File inventory landed verbatim from report.tsv: one row per file, "
                "every status, format and dataset type. Metadata only — the files themselves stay in "
                "ENCODE's public bucket. All text; list-valued fields are ','-joined as the portal writes "
                "them. The columns are the explicit field list encode.FILE_FIELDS. Holds the LATEST crawl "
                "only. ENCODE data are free of use restrictions; the ENCODE Consortium asks to be cited: "
                "https://www.encodeproject.org/help/citing-encode/",
    ),
    "resource.encode__experiment": TableDef(
        schema=Schema(
            NestedField(1, "resource_id", StringType(), required=True,
                        doc="'encode:' + accession, e.g. 'encode:ENCSR000AKS' — the spelling BEDbase's "
                            "derived_from_experiment rows use as target_id in resource.resource_relationship, "
                            "so the two join with '='. Business key."),
            NestedField(2, "accession", StringType(), required=True, doc="Bare ENCODE accession, e.g. 'ENCSR000AKS'."),
            NestedField(3, "status", StringType(), doc="'released', 'archived' or 'revoked'. Filter on it: archived and "
                                                       "revoked experiments are rows here too."),
            NestedField(4, "description", StringType(), doc="Submitter's free-text description."),
            NestedField(5, "assay_term_id", StringType(), doc="Assay ontology id, mostly OBI; 'NTR:…' is an ENCODE placeholder."),
            NestedField(6, "assay_term_name", StringType(), doc="Assay label, e.g. 'ChIP-seq'."),
            NestedField(7, "assay_title", StringType(), doc="ENCODE's finer assay name, e.g. 'TF ChIP-seq'."),
            NestedField(8, "biosample_term_id", StringType(),
                        doc="Biosample type as a CURIE in ontology.term.term_id's form: UBERON, CL, EFO or CLO; "
                            "'NTR:…' placeholders resolve nowhere. Joinable form: resource.resource_relationship "
                            "(has_biosample)."),
            NestedField(9, "biosample_term_name", StringType(), doc="Label of biosample_term_id, e.g. 'K562'."),
            NestedField(10, "biosample_classification", StringType(), doc="'tissue', 'cell line', 'primary cell', ..."),
            NestedField(11, "biosample_summary", StringType(), doc="ENCODE's one-line biosample description."),
            NestedField(12, "taxon_id", IntegerType(),
                        doc="NCBI taxonomy id. NULL where the experiment has no replicate, or replicates of more "
                            "than one organism."),
            NestedField(13, "organism", StringType(), doc="Scientific name as published; ','-joined if more than one."),
            NestedField(14, "target_label", StringType(), doc="Assay target, e.g. 'CTCF', 'H3K27ac'. NULL for untargeted assays."),
            NestedField(15, "target_investigated_as", StringType(), doc="Target categories as published, ','-joined."),
            NestedField(16, "target_gene_ids", ListType(element_id=116, element_type=StringType(), element_required=False),
                        doc="Entrez GeneIDs of the target's genes, sorted list. Joinable form: "
                            "resource.resource_relationship (has_target_gene, 'ncbigene:<id>')."),
            NestedField(17, "control_type", StringType(), doc="Set on control experiments, e.g. 'input library'."),
            NestedField(18, "lab", StringType(), doc="Submitting lab."),
            NestedField(19, "award", StringType(), doc="Grant number."),
            NestedField(20, "project", StringType(), doc="'ENCODE', 'Roadmap', 'modENCODE', 'modERN', 'GGR', ..."),
            NestedField(21, "rfa", StringType(), doc="Project phase, e.g. 'ENCODE4'."),
            NestedField(22, "assemblies", ListType(element_id=122, element_type=StringType(), element_required=False),
                        doc="Assembly labels of the experiment's processed files, sorted list, as the portal spells them."),
            NestedField(23, "dbxrefs", ListType(element_id=123, element_type=StringType(), element_required=False),
                        doc="External cross-references, sorted list, e.g. ['GEO:GSE30263'] — upstream's spelling, "
                            "not the 'geo:gse…' one BEDbase writes."),
            NestedField(24, "doi", StringType(), doc="The experiment's DOI."),
            NestedField(25, "date_submitted", StringType(), doc="YYYY-MM-DD, as published."),
            NestedField(26, "date_released", StringType(), doc="YYYY-MM-DD, as published."),
            NestedField(27, "portal_uri", StringType(), required=True, doc="The experiment's page on encodeproject.org."),
            NestedField(28, "provider", StringType(), required=True, doc="Constant 'ENCODE'."),
            NestedField(29, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(30, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("resource_id",),
        comment="One row per ENCODE experiment, Type 2 by resource_id: assay, biosample, target, lab and award. "
                "Every status is here — filter status = 'released' for current data. Its files are "
                "resource.encode__file rows with this resource_id as dataset_id. ENCODE data are free of use "
                "restrictions; the ENCODE Consortium asks to be cited: "
                "https://www.encodeproject.org/help/citing-encode/",
        properties={"bioc.column.taxon_id.prefix": "ncbitaxon",
                    "bioc.column.target_gene_ids.prefix": "ncbigene",
                    "bioc.column.doi.prefix": "doi"},
    ),
    "resource.encode__file": TableDef(
        schema=Schema(
            NestedField(1, "resource_id", StringType(), required=True,
                        doc="'encode:' + accession, e.g. 'encode:ENCFF002FAS' — the spelling BEDbase's "
                            "derived_from_sample rows use as target_id. Business key."),
            NestedField(2, "accession", StringType(), required=True,
                        doc="Bare ENCODE file accession; for the ~1,300 files that have none, the external "
                            "accession or name the portal keys them by (raw.encode__file.title)."),
            NestedField(3, "dataset_id", StringType(),
                        doc="'encode:' + the accession of the dataset the file belongs to. Joins "
                            "resource.encode__experiment.resource_id when dataset_type = 'experiments'."),
            NestedField(4, "dataset_type", StringType(),
                        doc="Kind of dataset, as the portal's path segment: 'experiments', 'annotations', "
                            "'references', ... Only experiments are landed (encode.py)."),
            NestedField(5, "status", StringType(), doc="'released', 'archived' or 'revoked'."),
            NestedField(6, "file_format", StringType(), doc="'bed', 'bigWig', 'fastq', 'bam', ..."),
            NestedField(7, "file_format_type", StringType(), doc="Sub-format, e.g. 'narrowPeak'. NULL for most formats."),
            NestedField(8, "file_type", StringType(), doc="Format and sub-format together, e.g. 'bed narrowPeak'."),
            NestedField(9, "output_type", StringType(), doc="What the file holds, e.g. 'IDR thresholded peaks'."),
            NestedField(10, "output_category", StringType(), doc="'raw data', 'alignment', 'signal', 'annotation', ..."),
            NestedField(11, "assembly", StringType(),
                        doc="Assembly LABEL as published ('GRCh38', 'hg19', 'mm10-minimal'), not an assembly key: "
                            "free of any join until issue #94 defines one. NULL for unaligned data."),
            NestedField(12, "genome_annotation", StringType(), doc="Gene annotation version, e.g. 'V29'."),
            NestedField(13, "size", LongType(), doc="Bytes."),
            NestedField(14, "md5sum", StringType(), doc="MD5 of the file as stored."),
            NestedField(15, "content_md5sum", StringType(), doc="MD5 of the uncompressed content, where published."),
            NestedField(16, "https_uri", StringType(),
                        doc="Portal download URL; redirects to cloud_uri. NULL where no file is available."),
            NestedField(17, "cloud_uri", StringType(), doc="Direct HTTPS URL in the public encode-public bucket."),
            NestedField(18, "s3_uri", StringType(), doc="s3://encode-public/… URI of the same object; anonymous read."),
            NestedField(19, "no_file_available", BooleanType(), doc="True where ENCODE holds metadata only."),
            NestedField(20, "restricted", BooleanType(), doc="True where access is restricted. NULL means unrestricted."),
            NestedField(21, "preferred_default", BooleanType(), doc="True where ENCODE marks this the file to use by default."),
            NestedField(22, "derived_from", ListType(element_id=122, element_type=StringType(), element_required=False),
                        doc="'encode:'-prefixed ids of the files this was computed from, sorted list; join to "
                            "resource_id. A few name a reference file rather than an accession."),
            NestedField(23, "lab", StringType(), doc="Producing lab; 'ENCODE Processing Pipeline' for uniform processing."),
            NestedField(24, "date_created", StringType(), doc="ISO 8601 timestamp, as published."),
            NestedField(25, "provider", StringType(), required=True, doc="Constant 'ENCODE'."),
            NestedField(26, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(27, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("resource_id",),
        comment="One row per ENCODE file, Type 2 by resource_id. Objects are REFERENCED, never ingested: "
                "https_uri / cloud_uri / s3_uri point at ENCODE's public bucket, with size and md5sum. "
                "Every status and every dataset type is here. ENCODE data are free of use restrictions; the "
                "ENCODE Consortium asks to be cited: https://www.encodeproject.org/help/citing-encode/",
    ),
    "raw.hgnc__complete_set": TableDef(
        schema=Schema(
            NestedField(1, "hgnc_id", StringType(), required=True,
                        doc="HGNC id with its prefix, e.g. 'HGNC:11998'. Stable across symbol "
                            "changes. Unique per row within one hgnc_version, so it is the natural "
                            "key even though raw declares none."),
            NestedField(2, "symbol", StringType(), doc="Approved gene symbol, e.g. TP53."),
            NestedField(3, "name", StringType(), doc="Approved gene name, e.g. 'tumor protein p53'."),
            NestedField(4, "locus_group", StringType(),
                        doc="Broad locus class: 'protein-coding gene', 'non-coding RNA', "
                            "'pseudogene' or 'other'."),
            NestedField(5, "locus_type", StringType(),
                        doc="Specific locus class within locus_group, e.g. 'gene with protein "
                            "product', 'RNA, long non-coding', 'immunoglobulin pseudogene'."),
            NestedField(6, "status", StringType(),
                        doc="Nomenclature status. 'Approved' on every row of the complete set: "
                            "withdrawn and merged records are published in a separate file, "
                            "withdrawn.txt, which is not landed."),
            NestedField(7, "location", StringType(),
                        doc="Cytogenetic location, e.g. 17p13.1. Not a coordinate."),
            NestedField(8, "alias_symbol", StringType(),
                        doc="Other symbols used for this gene, never approved by HGNC, "
                            "'|'-separated as published."),
            NestedField(9, "alias_name", StringType(),
                        doc="Other names used for this gene, '|'-separated as published."),
            NestedField(10, "prev_symbol", StringType(),
                        doc="Symbols HGNC previously approved for this gene, '|'-separated as "
                            "published. No dates are attached to the individual symbols."),
            NestedField(11, "prev_name", StringType(),
                        doc="Names HGNC previously approved for this gene, '|'-separated."),
            NestedField(12, "gene_group", StringType(),
                        doc="Names of the HGNC gene groups (families) the gene belongs to, "
                            "'|'-separated, parallel to gene_group_id."),
            NestedField(13, "gene_group_id", StringType(),
                        doc="HGNC gene group ids, '|'-separated, parallel to gene_group."),
            NestedField(14, "date_approved_reserved", StringType(),
                        doc="Date the symbol was first approved or reserved, YYYY-MM-DD. String, "
                            "because raw is landed unparsed."),
            NestedField(15, "date_symbol_changed", StringType(),
                        doc="Date of the most recent approved-symbol change, YYYY-MM-DD. NULL if "
                            "the symbol never changed."),
            NestedField(16, "date_name_changed", StringType(),
                        doc="Date of the most recent approved-name change, YYYY-MM-DD."),
            NestedField(17, "date_modified", StringType(),
                        doc="Date HGNC last edited any field of the record, YYYY-MM-DD."),
            NestedField(18, "entrez_id", StringType(), doc="NCBI Entrez GeneID, curated by HGNC."),
            NestedField(19, "ensembl_gene_id", StringType(),
                        doc="Ensembl stable gene id, curated by HGNC. Unversioned."),
            NestedField(20, "vega_id", StringType(), doc="Vega (Havana) gene id, OTTHUMG...; Vega is archived."),
            NestedField(21, "ucsc_id", StringType(), doc="UCSC Genome Browser gene id, e.g. uc060aur.1."),
            NestedField(22, "ena", StringType(),
                        doc="INSDC (ENA/GenBank/DDBJ) nucleotide accessions, '|'-separated."),
            NestedField(23, "refseq_accession", StringType(),
                        doc="RefSeq nucleotide accessions without version, '|'-separated."),
            NestedField(24, "ccds_id", StringType(), doc="Consensus CDS ids, '|'-separated."),
            NestedField(25, "uniprot_ids", StringType(), doc="UniProtKB accessions, '|'-separated."),
            NestedField(26, "pubmed_id", StringType(),
                        doc="PubMed ids of publications HGNC cites for the gene, '|'-separated."),
            NestedField(27, "mgd_id", StringType(),
                        doc="Mouse Genome Informatics ids of mouse orthologs, e.g. 'MGI:98834', "
                            "'|'-separated."),
            NestedField(28, "rgd_id", StringType(),
                        doc="Rat Genome Database ids of rat orthologs, e.g. 'RGD:3889', '|'-separated."),
            NestedField(29, "lsdb", StringType(),
                        doc="Locus-specific mutation databases as one flat '|'-separated list that "
                            "alternates name and URL: 'name|url|name|url'."),
            NestedField(30, "cosmic", StringType(), doc="Symbol used by COSMIC for the gene."),
            NestedField(31, "omim_id", StringType(),
                        doc="OMIM ids. Usually one; '|'-separated on the few genes with several."),
            NestedField(32, "mirbase", StringType(), doc="miRBase accession, e.g. MI0000651."),
            NestedField(33, "homeodb", StringType(), doc="Homeobox Database id."),
            NestedField(34, "snornabase", StringType(), doc="snoRNABase id, e.g. SR0000002."),
            NestedField(35, "bioparadigms_slc", StringType(),
                        doc="Symbol used by the Bioparadigms solute carrier (SLC) tables."),
            NestedField(36, "orphanet", StringType(), doc="Orphanet gene id."),
            NestedField(37, "pseudogene_org", StringType(),
                        doc="Pseudogene.org id. Upstream column name 'pseudogene.org'."),
            NestedField(38, "horde_id", StringType(),
                        doc="Symbol used by HORDE, the human olfactory receptor database."),
            NestedField(39, "merops", StringType(), doc="MEROPS peptidase database id, e.g. I43.950."),
            NestedField(40, "imgt", StringType(),
                        doc="Symbol used by IMGT, the immunogenetics information system."),
            NestedField(41, "iuphar", StringType(),
                        doc="IUPHAR/BPS Guide to Pharmacology link. On 2026-09-18 the cell repeats "
                            "the row's own HGNC id rather than an IUPHAR object id; landed as "
                            "published."),
            NestedField(42, "kznf_gene_catalog", StringType(),
                        doc="Human KZNF Gene Catalog id. Empty on every row on 2026-09-18."),
            NestedField(43, "mamit_trnadb", StringType(),
                        doc="Mamit-tRNAdb id. Upstream column name 'mamit-trnadb'."),
            NestedField(44, "cd", StringType(),
                        doc="Cluster-of-differentiation symbol from the HCDM database, e.g. CD243."),
            NestedField(45, "lncrnadb", StringType(), doc="lncRNAdb id."),
            NestedField(46, "enzyme_id", StringType(), doc="Enzyme Commission numbers, '|'-separated."),
            NestedField(47, "intermediate_filament_db", StringType(),
                        doc="Human Intermediate Filament Database id. Empty on every row on 2026-09-18."),
            NestedField(48, "rna_central_id", StringType(), doc="RNAcentral id, e.g. URS00007E4F6E."),
            NestedField(49, "lncipedia", StringType(), doc="Symbol used by LNCipedia."),
            NestedField(50, "gtrnadb", StringType(), doc="GtRNAdb gene name, e.g. tRNA-Ala-AGC-1-1."),
            NestedField(51, "agr", StringType(),
                        doc="Alliance of Genome Resources id, which for human is the HGNC id itself."),
            NestedField(52, "mane_select", StringType(),
                        doc="The MANE Select transcript as 'Ensembl transcript|RefSeq transcript', "
                            "both versioned, e.g. 'ENST00000269305.9|NM_000546.6'."),
            NestedField(53, "gencc", StringType(),
                        doc="Gene Curation Coalition link: the row's HGNC id where GenCC has a "
                            "record for the gene, else NULL."),
            NestedField(54, "hgnc_version", StringType(), required=True,
                        doc="The date that versions this file: the date in a dated archive's file "
                            "name, or the retrieval date for the rolling file, which carries no "
                            "version of its own. provenance.release says which it was. Raw is "
                            "replaced wholesale per value of this column."),
            NestedField(55, "landed_in", StringType(), required=True,
                        doc="The biocOnIce release whose ingest landed these rows."),
        ),
        comment="HGNC's hgnc_complete_set.txt landed verbatim and whole: one row per approved "
                "human gene nomenclature record, every column. Every column is a string and "
                "nothing is split — multi-valued cells keep their '|'. An empty cell is read as "
                "NULL. Approved records only: HGNC publishes withdrawn and merged ids in a "
                "separate file that is not landed. Licence CC0.",
        properties={"bioc.column.hgnc_id.prefix": "hgnc",
                    "bioc.column.entrez_id.prefix": "ncbigene",
                    "bioc.column.ensembl_gene_id.prefix": "ensembl",
                    "bioc.license": "CC0-1.0"},
    ),
    "annotation.hgnc__gene": TableDef(
        schema=Schema(
            NestedField(1, "hgnc_id", StringType(), required=True,
                        doc="HGNC id with its prefix, e.g. 'HGNC:11998'. Stable across symbol "
                            "changes, which is why it and not the symbol is the key."),
            NestedField(2, "taxon_id", IntegerType(), required=True,
                        doc="NCBI taxonomy id. Always 9606: HGNC names human genes only."),
            NestedField(3, "symbol", StringType(),
                        doc="The approved symbol, e.g. TP53. The authoritative one where "
                            "annotation.gene (Ensembl) and annotation.ncbi__gene disagree."),
            NestedField(4, "name", StringType(), doc="The approved name, e.g. 'tumor protein p53'."),
            NestedField(5, "locus_group", StringType(),
                        doc="Broad locus class: 'protein-coding gene', 'non-coding RNA', "
                            "'pseudogene' or 'other'."),
            NestedField(6, "locus_type", StringType(),
                        doc="Specific locus class, e.g. 'gene with protein product'. HGNC's own "
                            "vocabulary, not mapped onto Ensembl biotypes or NCBI gene types."),
            NestedField(7, "status", StringType(),
                        doc="Nomenclature status. Currently always 'Approved': the complete set "
                            "excludes withdrawn records, so a withdrawn gene shows up as a row "
                            "closed by valid_to rather than as a status value."),
            NestedField(8, "location", StringType(), doc="Cytogenetic location, e.g. 17p13.1. Not a coordinate."),
            NestedField(9, "alias_symbol", StringType(),
                        doc="Symbols in use but never approved, '|'-separated as published."),
            NestedField(10, "alias_name", StringType(),
                        doc="Names in use but never approved, '|'-separated as published."),
            NestedField(11, "prev_symbol", StringType(),
                        doc="Previously approved symbols, '|'-separated as published. With "
                            "date_symbol_changed this is HGNC's own symbol history; this table's "
                            "valid_from/valid_to record changes seen since biocOnIce began loading it."),
            NestedField(12, "prev_name", StringType(),
                        doc="Previously approved names, '|'-separated as published."),
            NestedField(13, "date_approved_reserved", StringType(),
                        doc="Date the symbol was first approved or reserved, YYYY-MM-DD."),
            NestedField(14, "date_symbol_changed", StringType(),
                        doc="Date of the most recent symbol change, YYYY-MM-DD. NULL if it never changed."),
            NestedField(15, "date_name_changed", StringType(),
                        doc="Date of the most recent name change, YYYY-MM-DD."),
            NestedField(16, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(17, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("hgnc_id",),
        comment="Human gene nomenclature as HGNC approves it, keyed by HGNC id: symbol, name, "
                "locus type, and the symbol and name history HGNC publishes. HGNC is the naming "
                "authority, so this is the tie-breaker when Ensembl and NCBI carry different "
                "symbols for one gene. Reach Entrez, Ensembl, UCSC and OMIM ids through "
                "annotation.identifier_mapping (source = 'HGNC'). Gene groups and the remaining "
                "cross-references stay in raw.hgnc__complete_set. Licence CC0.",
        properties={"bioc.column.hgnc_id.prefix": "hgnc",
                    "bioc.column.taxon_id.prefix": "ncbitaxon",
                    "bioc.license": "CC0-1.0"},
    ),
    "raw.ncbi__gene_orthologs": _ncbi_gene_pairs(
        "NCBI gene_orthologs landed verbatim and whole: every ortholog pair NCBI publishes, "
        "~19M rows. NOT symmetric: each ortholog set has one primary gene (the first two "
        "columns; human for the vertebrates) and its other N-1 members are listed against "
        "it once, so two non-primary members are never paired directly. Query "
        "annotation.ortholog, which carries each pair in both directions. Regenerated "
        "nightly upstream, so the retrieval date is the version.",
        "Always 'Ortholog' in this file."),
    "raw.ncbi__gene_group": _ncbi_gene_pairs(
        "NCBI gene_group landed verbatim and whole: gene-gene relationships other than "
        "orthology, ~51k rows, reported symmetrically where that makes sense (a 'Readthrough "
        "parent' row has its 'Readthrough child' mirror). NCBI calls the file a non-"
        "comprehensive subset. Nothing is derived from it yet. Orthologs are in "
        "raw.ncbi__gene_orthologs. Regenerated nightly upstream, so the retrieval date is "
        "the version.",
        "How gene_id relates to other_gene_id: 'Related functional gene', 'Related "
        "pseudogene', 'Readthrough parent', 'Readthrough child', 'Readthrough sibling', "
        "'Potential readthrough sibling', 'Region parent' or 'Region member'."),
    "annotation.ortholog": TableDef(
        schema=Schema(
            NestedField(1, "gene_id", StringType(), required=True,
                        doc="The gene, in the provider's own id space: an Entrez GeneID such as "
                            "7157 under source NCBI. Part of the merge key."),
            NestedField(2, "taxon_id", IntegerType(), required=True,
                        doc="NCBI taxonomy id of gene_id's organism, e.g. 9606. The row belongs "
                            "to this taxon: it is the merge scope, so filter on it to get one "
                            "organism's orthologs. Part of the merge key."),
            NestedField(3, "ortholog_gene_id", StringType(), required=True,
                        doc="The orthologous gene in another organism, same id space as gene_id, "
                            "e.g. 22059 (mouse Trp53) for 7157. Part of the merge key."),
            NestedField(4, "ortholog_taxon_id", IntegerType(), required=True,
                        doc="NCBI taxonomy id of ortholog_gene_id's organism, e.g. 10090. Part of "
                            "the merge key."),
            NestedField(5, "source", StringType(), required=True,
                        doc="The provider asserting the orthology: 'NCBI' now (gene_orthologs, "
                            "from the Eukaryotic Genome Annotation Pipeline's protein similarity "
                            "plus local synteny, and curator review); Ensembl Compara when it "
                            "lands. Part of the business key and of every writer's merge scope, "
                            "so providers stack and none can retire another's rows (ADR-0004)."),
            NestedField(6, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(7, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("gene_id", "taxon_id", "ortholog_gene_id", "ortholog_taxon_id", "source"),
        comment="Ortholog pairs, stacked across providers: Bioconductor's Orthology.eg.db. One "
                "row per ordered pair per source, and every pair is stored in both directions, so "
                "`WHERE gene_id = '22059' AND source = 'NCBI'` finds human TP53 as readily as the "
                "reverse. Under source NCBI only pairs NCBI itself lists are here: each ortholog "
                "set hangs off one primary gene (human, for vertebrates), so mouse-to-rat is two "
                "hops through the human gene — join the table to itself on ortholog_gene_id — "
                "and is not materialised. The whole tuple is the key: a pair has no attributes, "
                "so it is only ever asserted or withdrawn.",
        properties={"bioc.column.taxon_id.prefix": "ncbitaxon",
                    "bioc.column.ortholog_taxon_id.prefix": "ncbitaxon"},
    ),
    "raw.ncbi__mane_summary": TableDef(
        schema=Schema(
            NestedField(1, "ncbi_geneid", StringType(), required=True,
                        doc="Upstream '#NCBI_GeneID': the Entrez GeneID with MANE's own prefix, "
                            "e.g. 'GeneID:7157', verbatim."),
            NestedField(2, "ensembl_gene", StringType(), required=True,
                        doc="Upstream 'Ensembl_Gene': versioned Ensembl gene id, e.g. ENSG00000141510.21."),
            NestedField(3, "hgnc_id", StringType(),
                        doc="Upstream 'HGNC_ID', e.g. 'HGNC:11998'. NULL (an empty cell) for the "
                            "few genes HGNC has not named."),
            NestedField(4, "symbol", StringType(),
                        doc="Gene symbol: HGNC's where there is one, else NCBI's."),
            NestedField(5, "name", StringType(), doc="Gene name, e.g. 'tumor protein p53'."),
            NestedField(6, "refseq_nuc", StringType(), required=True,
                        doc="Upstream 'RefSeq_nuc': versioned RefSeq transcript, e.g. NM_000546.6. "
                            "NR_ for the non-coding transcripts MANE includes."),
            NestedField(7, "refseq_prot", StringType(),
                        doc="Upstream 'RefSeq_prot': versioned RefSeq protein, e.g. NP_000537.3. "
                            "NULL on a non-coding (NR_) row."),
            NestedField(8, "ensembl_nuc", StringType(), required=True,
                        doc="Upstream 'Ensembl_nuc': versioned Ensembl transcript, e.g. ENST00000269305.9."),
            NestedField(9, "ensembl_prot", StringType(),
                        doc="Upstream 'Ensembl_prot': versioned Ensembl protein, e.g. "
                            "ENSP00000269305.4. NULL on a non-coding row."),
            NestedField(10, "mane_status", StringType(), required=True,
                        doc="Upstream 'MANE_status': 'MANE Select' or 'MANE Plus Clinical'."),
            NestedField(11, "grch38_chr", StringType(),
                        doc="Upstream 'GRCh38_chr': RefSeq accession of the GRCh38 sequence, e.g. "
                            "NC_000017.11. Not a chromosome name."),
            NestedField(12, "chr_start", StringType(),
                        doc="Transcript start on grch38_chr, 1-based, as published (unparsed string)."),
            NestedField(13, "chr_end", StringType(),
                        doc="Transcript end on grch38_chr, inclusive, as published (unparsed string)."),
            NestedField(14, "chr_strand", StringType(),
                        doc="'+' or '-'. The '-' is the minus strand, never a missing marker: this "
                            "file is read with the empty cell as NULL, unlike the NCBI Gene dumps."),
            NestedField(15, "mane_version", StringType(), required=True,
                        doc="MANE release, from the file name, e.g. '1.5'. Raw is replaced per value "
                            "of this column, so several releases can coexist."),
            NestedField(16, "landed_in", StringType(), required=True,
                        doc="The biocOnIce release whose ingest landed these rows."),
        ),
        comment="The MANE summary file (Matched Annotation from NCBI and EMBL-EBI) landed verbatim "
                "and whole, one row per matched RefSeq/Ensembl transcript pair, human GRCh38 only. "
                "Every column is the unparsed string; an empty cell is NULL. Query "
                "annotation.mane__transcript instead. A joint NCBI / EMBL-EBI product, published "
                "on NCBI's FTP site without a licence file; NCBI places no restrictions on use or "
                "distribution of its molecular data.",
        properties={"bioc.column.hgnc_id.prefix": "hgnc"},
    ),
    "annotation.mane__transcript": TableDef(
        schema=Schema(
            NestedField(1, "ensembl_transcript_id", StringType(), required=True,
                        doc="Ensembl stable transcript id without version, e.g. ENST00000269305. "
                            "The key, and the join to annotation.transcript.transcript_id "
                            "(source = 'ENSEMBL')."),
            NestedField(2, "taxon_id", IntegerType(), required=True,
                        doc="NCBI taxonomy id. Always 9606: MANE is a human product."),
            NestedField(3, "ensembl_transcript_version", StringType(),
                        doc="Version of the Ensembl transcript MANE matched, e.g. 9. The match is "
                            "exact to the version: if annotation.transcript.version differs, the "
                            "two are from different Ensembl releases."),
            NestedField(4, "refseq_rna", StringType(), required=True,
                        doc="The matched RefSeq transcript WITH its version, e.g. NM_000546.6 — the "
                            "form the REFSEQ_RNA namespace of annotation.identifier_mapping uses. "
                            "NR_ for a non-coding transcript."),
            NestedField(5, "mane_status", StringType(), required=True,
                        doc="'MANE Select': the one representative transcript of its gene. 'MANE "
                            "Plus Clinical': an extra transcript needed to report known pathogenic "
                            "variants that Select misses; a gene may have more than one. Filter on "
                            "this before assuming one row per gene."),
            NestedField(6, "gene_id", StringType(), required=True,
                        doc="NCBI Entrez GeneID, e.g. 7157, without MANE's 'GeneID:' prefix. Joins "
                            "annotation.ncbi__gene."),
            NestedField(7, "ensembl_gene_id", StringType(), required=True,
                        doc="Ensembl stable gene id without version, e.g. ENSG00000141510. Joins "
                            "annotation.gene (source = 'ENSEMBL')."),
            NestedField(8, "hgnc_id", StringType(),
                        doc="HGNC id with its prefix, e.g. 'HGNC:11998'. NULL where HGNC has not "
                            "named the gene."),
            NestedField(9, "symbol", StringType(), doc="Gene symbol as MANE carried it, e.g. TP53."),
            NestedField(10, "refseq_protein", StringType(),
                        doc="The matched RefSeq protein with its version, e.g. NP_000537.3. NULL "
                            "for a non-coding transcript."),
            NestedField(11, "ensembl_protein_id", StringType(),
                        doc="Ensembl stable protein id without version, e.g. ENSP00000269305. NULL "
                            "for a non-coding transcript."),
            NestedField(12, "ensembl_protein_version", StringType(),
                        doc="Version of the matched Ensembl protein, e.g. 4."),
            NestedField(13, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(14, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("ensembl_transcript_id",),
        comment="MANE transcripts: for each human gene, the transcript RefSeq and Ensembl annotate "
                "identically (MANE Select), plus the MANE Plus Clinical extras. One row per matched "
                "pair, carrying both providers' transcript, protein and gene ids — the bridge "
                "between RefSeq- and Ensembl-based annotation. RefSeq accessions are versioned and "
                "Ensembl ids are split into id and version, each the way the rest of the catalog "
                "writes them. The same pairs are in annotation.identifier_mapping as REFSEQ_RNA -> "
                "ENSEMBL_TRANSCRIPT under source = 'MANE'. Gene name and GRCh38 coordinates stay "
                "in raw.ncbi__mane_summary.",
        properties={"bioc.column.ensembl_transcript_id.prefix": "ensembl",
                    "bioc.column.ensembl_gene_id.prefix": "ensembl",
                    "bioc.column.ensembl_protein_id.prefix": "ensembl",
                    "bioc.column.gene_id.prefix": "ncbigene",
                    "bioc.column.hgnc_id.prefix": "hgnc",
                    "bioc.column.refseq_rna.prefix": "refseq",
                    "bioc.column.refseq_protein.prefix": "refseq",
                    "bioc.column.taxon_id.prefix": "ncbitaxon"},
    ),
    "raw.cellosaurus__release": TableDef(
        schema=Schema(
            NestedField(1, "line_number", LongType(), required=True,
                        doc="1-based ordinal of this line in cellosaurus.txt. Ordering by it gives "
                            "the file back."),
            NestedField(2, "accession", StringType(),
                        doc="CVCL_ accession of the entry this line belongs to, taken from the "
                            "entry's AC line and repeated on every line of the entry (the ID line "
                            "precedes AC, the '//' terminator follows it). NULL on header lines."),
            NestedField(3, "code", StringType(),
                        doc="The two-letter line code: ID, AC, AS, SY, DR, RX, WW, CC, ST, DI, OX, "
                            "HI, OI, SX, AG, CA, DT, or '//' for the entry terminator. The file's "
                            "own header documents each. NULL on header lines."),
            NestedField(4, "value", StringType(),
                        doc="The line after its code and the three spaces that follow, verbatim "
                            "and unparsed. On a header line (code IS NULL) the whole line, blank "
                            "ones included as ''. NULL on a '//' line. The file's line is "
                            "therefore code || '   ' || value, '//' or value."),
            NestedField(5, "cellosaurus_version", StringType(), required=True,
                        doc="Cellosaurus' own release, from the ' Version:' line of the file "
                            "header, e.g. '56.0'. Raw is replaced wholesale per value of this "
                            "column, so more than one release can coexist."),
            NestedField(6, "landed_in", StringType(), required=True,
                        doc="The biocOnIce release whose ingest landed these rows."),
        ),
        comment="Cellosaurus' flat file (cellosaurus.txt) landed whole, one row per line in file "
                "order, the header included: nothing is filtered and no value is parsed, so "
                "comments (CC), STR profiles (ST), references (RX) and web links (WW) are here "
                "although nothing derives them yet. Query annotation.cellosaurus__* instead. "
                + _CELLOSAURUS_CREDIT + "; reformatted as rows, content unmodified.",
        properties={"bioc.license": "CC-BY-4.0"},
    ),
    "annotation.cellosaurus__cell_line": TableDef(
        schema=Schema(
            NestedField(1, "accession", StringType(), required=True,
                        doc="Cellosaurus accession, e.g. CVCL_0030 (HeLa). Stable, and the form "
                            "RRIDs cite (RRID:CVCL_0030)."),
            NestedField(2, "name", StringType(), required=True,
                        doc="The recommended cell line name (ID line), e.g. 'HeLa'. Not unique: "
                            "distinct lines do share names, which is why the accession is the key."),
            NestedField(3, "synonyms", StringType(),
                        doc="Other names and spellings in use (SY line), sorted and '|'-joined. "
                            "NULL where there are none."),
            NestedField(4, "secondary_accessions", StringType(),
                        doc="Former accessions merged into this one (AS line), sorted and "
                            "'|'-joined. Resolve a CVCL_ id that matches no accession here."),
            NestedField(5, "taxon_ids", ListType(element_id=105, element_type=IntegerType(), element_required=False),
                        doc="NCBI taxonomy ids of the species of origin (OX lines), sorted. A list "
                            "because hybrid lines and hybridomas have two or three, e.g. "
                            "[9606, 10116] for a human x rat hybrid; filter with list_contains()."),
            NestedField(6, "sex", StringType(),
                        doc="Sex of the cell (SX line), in Cellosaurus' words: 'Female', 'Male', "
                            "'Mixed sex', 'Sex ambiguous' or 'Sex unspecified'. NULL where the "
                            "entry has no SX line, which is not the same as 'Sex unspecified'."),
            NestedField(7, "age", StringType(),
                        doc="Age of the donor at sampling (AG line), as published and unparsed: "
                            "'30Y6M', 'Adult', 'Fetus', 'Blastocyst stage', 'Age unspecified'."),
            NestedField(8, "category", StringType(),
                        doc="Cellosaurus' cell line category (CA line), e.g. 'Cancer cell line', "
                            "'Transformed cell line', 'Induced pluripotent stem cell', 'Hybridoma', "
                            "'Hybrid cell line', 'Finite cell line'."),
            NestedField(9, "parent_accessions", StringType(),
                        doc="Accessions of the lines this one was derived from (HI lines), sorted "
                            "and '|'-joined: usually one, two for a hybrid of two established "
                            "lines. NULL for a line established directly from a donor."),
            NestedField(10, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(11, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("accession",),
        comment="Cell lines as Cellosaurus registers them, one current row per CVCL_ accession: "
                "name, synonyms, species, sex, donor age, category and parent line. Reach other "
                "databases' ids (DepMap, ENCODE, CLO, EFO, BTO, Wikidata, GEO) through "
                "annotation.cellosaurus__xref and diseases through annotation.cellosaurus__disease. "
                "Comments, STR profiles, references and same-individual links stay in "
                "raw.cellosaurus__release. " + _CELLOSAURUS_CREDIT + "; modified (normalised).",
        properties={"bioc.column.accession.prefix": "cellosaurus",
                    "bioc.column.taxon_ids.prefix": "ncbitaxon",
                    "bioc.license": "CC-BY-4.0"},
    ),
    "annotation.cellosaurus__xref": TableDef(
        schema=Schema(
            NestedField(1, "accession", StringType(), required=True,
                        doc="Cellosaurus accession, e.g. CVCL_0030."),
            NestedField(2, "database", StringType(), required=True,
                        doc="The cross-referenced resource, in Cellosaurus' abbreviation: 'DepMap', "
                            "'ENCODE', 'CLO', 'EFO', 'BTO', 'Wikidata', 'GEO', 'BioSample', "
                            "'Cell_Model_Passport', 'ATCC', ... (117 in release 56.0)."),
            NestedField(3, "identifier", StringType(), required=True,
                        doc="The line's id in that resource, as Cellosaurus prints it: 'ACH-001086', "
                            "'SIDM00846', 'Q847482', 'GSM501788'. Ontology ids come with an "
                            "underscore ('EFO_0001185', 'CLO_0003684'); replace(identifier, '_', ':') "
                            "joins them to ontology.term.term_id."),
            NestedField(4, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(5, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("accession", "database", "identifier"),
        comment="Cellosaurus' cross-references (DR lines): which record in another resource is "
                "this cell line. Many-to-many in both directions. The whole tuple is the key, so a "
                "cross-reference is only ever asserted or withdrawn. Not in "
                "annotation.identifier_mapping, which maps gene identifiers within one taxon: a "
                "cell line is not a gene and a hybrid line has two taxa. "
                + _CELLOSAURUS_CREDIT + "; modified (normalised).",
        properties={"bioc.column.accession.prefix": "cellosaurus",
                    "bioc.license": "CC-BY-4.0"},
    ),
    "annotation.cellosaurus__disease": TableDef(
        schema=Schema(
            NestedField(1, "accession", StringType(), required=True,
                        doc="Cellosaurus accession, e.g. CVCL_0030."),
            NestedField(2, "database", StringType(), required=True,
                        doc="The disease vocabulary: 'NCIt' (NCI Thesaurus) or 'ORDO' (Orphanet "
                            "rare disease ontology). Most lines with a disease carry one of each."),
            NestedField(3, "disease_id", StringType(), required=True,
                        doc="The term's id as Cellosaurus prints it: 'C27677' for NCIt (NCIT:C27677 "
                            "as a CURIE), 'Orphanet_521' for ORDO (Orphanet:521)."),
            NestedField(4, "disease_name", StringType(),
                        doc="The term's label as Cellosaurus prints it, e.g. 'Human "
                            "papillomavirus-related endocervical adenocarcinoma'."),
            NestedField(5, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(6, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("accession", "database", "disease_id"),
        comment="Diseases of the donor a cell line was established from (DI lines), one row per "
                "(cell line, disease term). Kept apart from annotation.cellosaurus__xref because "
                "it says something about the line rather than naming it elsewhere. Not mapped to "
                "MONDO: MONDO's own NCIT and Orphanet xrefs are the bridge. "
                + _CELLOSAURUS_CREDIT + "; modified (normalised).",
        properties={"bioc.column.accession.prefix": "cellosaurus",
                    "bioc.license": "CC-BY-4.0"},
    ),
    "raw.wikipathways__gmt": TableDef(
        schema=Schema(
            NestedField(1, "file_name", StringType(), required=True,
                        doc="The species GMT file this line came from, e.g. "
                            "'wikipathways-20260910-gmt-Homo_sapiens.gmt'. Every species file of "
                            "the release is landed."),
            NestedField(2, "line_number", IntegerType(), required=True,
                        doc="1-based line number within file_name. With file_name it identifies a "
                            "row within one wikipathways_version, though raw declares no key."),
            NestedField(3, "name", StringType(),
                        doc="GMT field 1, the set name, unparsed. WikiPathways packs four "
                            "'%'-separated fields into it: pathway name, 'WikiPathways_<release "
                            "date>', WP id, species — e.g. 'Glutathione metabolism%"
                            "WikiPathways_20260910%WP100%Homo sapiens'."),
            NestedField(4, "description", StringType(),
                        doc="GMT field 2, the set description. WikiPathways puts the pathway's "
                            "URL here, e.g. https://www.wikipathways.org/instance/WP100."),
            NestedField(5, "genes", StringType(),
                        doc="GMT fields 3 onward, verbatim: the rest of the line, still "
                            "tab-separated, in file order and with the file's repeats (a gene "
                            "drawn twice in a pathway is listed twice). Entrez GeneIDs. Split "
                            "with str_split(genes, chr(9)). NULL for a set with no genes."),
            NestedField(6, "species", StringType(),
                        doc="The species token of file_name, as spelled there, e.g. "
                            "'Homo_sapiens'."),
            NestedField(7, "wikipathways_version", StringType(), required=True,
                        doc="WikiPathways release these rows came from: the date in the file "
                            "names, in WikiPathways' own form, e.g. '20260910'. Raw holds every "
                            "landed version; filter on this to get one."),
            NestedField(8, "landed_in", StringType(), required=True,
                        doc="biocOnIce release that fetched these rows."),
        ),
        comment="WikiPathways' monthly GMT gene-set export (data.wikipathways.org/current/gmt/), "
                "every species file, one row per GMT line. GMT is ragged — name, description, "
                "then one field per gene — so the gene fields are kept as one verbatim "
                "tab-joined string and the packed set name is left unparsed; "
                "annotation.wikipathways__* is the interpreted form. Licence CC0 "
                "(wikipathways.org/terms.html).",
        properties={"bioc.license": "CC0-1.0"},
    ),
    "annotation.wikipathways__pathway": TableDef(
        schema=Schema(
            NestedField(1, "pathway_id", StringType(), required=True,
                        doc="WikiPathways id, e.g. WP100. Unique across species: a pathway "
                            "belongs to one organism, and its homologue in another has its own "
                            "id."),
            NestedField(2, "taxon_id", IntegerType(), required=True,
                        doc="NCBI taxonomy id of the pathway's species, e.g. 9606. Species-level, "
                            "as WikiPathways names it — NCBI Gene files yeast genes under strain "
                            "S288C (559292), not 4932."),
            NestedField(3, "name", StringType(), doc="Pathway title, e.g. 'Glutathione metabolism'."),
            NestedField(4, "species", StringType(),
                        doc="Species name as WikiPathways spells it, e.g. 'Homo sapiens', "
                            "'Canis familiaris'. taxon_id is the joinable form."),
            NestedField(5, "url", StringType(),
                        doc="The pathway's page, e.g. https://www.wikipathways.org/instance/WP100 "
                            "— the GMT description field."),
            NestedField(6, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(7, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("pathway_id",),
        comment="One row per WikiPathways pathway, every species, from the monthly GMT export. "
                "Only what the GMT carries: ontology tags, authors and last-modified dates live "
                "in the GPML export, which is not landed. Genes are in "
                "annotation.wikipathways__gene_pathway. Licence CC0.",
        properties={"bioc.column.pathway_id.prefix": "wikipathways",
                    "bioc.column.taxon_id.prefix": "ncbitaxon",
                    "bioc.license": "CC0-1.0"},
    ),
    "annotation.wikipathways__gene_pathway": TableDef(
        schema=Schema(
            NestedField(1, "pathway_id", StringType(), required=True,
                        doc="WikiPathways id, e.g. WP100; joins to "
                            "annotation.wikipathways__pathway. Part of the merge key."),
            NestedField(2, "taxon_id", IntegerType(), required=True,
                        doc="NCBI taxonomy id of the pathway's species (species-level; see "
                            "annotation.wikipathways__pathway.taxon_id). Determined by "
                            "pathway_id, carried so a species filter needs no join."),
            NestedField(3, "gene_id", StringType(), required=True,
                        doc="NCBI Entrez GeneID of a member gene, e.g. 2687; joins to "
                            "annotation.ncbi__gene.gene_id — on gene_id alone, which is unique "
                            "across taxa. Part of the merge key."),
            NestedField(4, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(5, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("pathway_id", "gene_id"),
        comment="Gene-set membership: one row per (pathway, Entrez gene), every species, from "
                "WikiPathways' monthly GMT export. A set, so a gene the GMT line repeats appears "
                "once. Membership has no attributes beyond the species, so a row is only ever "
                "asserted or withdrawn. (pathway_id, taxon_id, gene_id) is the shape any other "
                "gene-set source should take so that they union. Licence CC0.",
        properties={"bioc.column.pathway_id.prefix": "wikipathways",
                    "bioc.column.taxon_id.prefix": "ncbitaxon",
                    "bioc.column.gene_id.prefix": "ncbigene",
                    "bioc.license": "CC0-1.0"},
    ),
    "raw.eqtlcatalogue__dataset": TableDef(
        schema=Schema(
            NestedField(1, "study_id", StringType(), doc="eQTL Catalogue study accession, e.g. QTS000001."),
            NestedField(2, "dataset_id", StringType(), required=True,
                        doc="eQTL Catalogue dataset accession, e.g. QTD000001: one study x sample group x "
                            "quantification method. Unique per row within one eqtlcatalogue_release, so it "
                            "is the natural key even though raw declares none."),
            NestedField(3, "study_label", StringType(), doc="Study name, e.g. Alasoo_2018, GTEx."),
            NestedField(4, "sample_group", StringType(),
                        doc="The study's group of samples the QTLs were mapped in, e.g. macrophage_naive."),
            NestedField(5, "tissue_id", StringType(),
                        doc="Ontology id of the tissue or cell type, as published with an underscore: "
                            "CL_0000235, UBERON_0002107, EFO_0005292 (LCL). Not a CURIE here."),
            NestedField(6, "tissue_label", StringType(), doc="Upstream's label for tissue_id, e.g. macrophage."),
            NestedField(7, "condition_label", StringType(), doc="Stimulation or condition, e.g. naive, IFNg."),
            NestedField(8, "sample_size", StringType(), doc="Number of samples (donors) in the dataset, as text."),
            NestedField(9, "quant_method", StringType(),
                        doc="Molecular trait quantified: ge (gene expression), exon, tx (transcript usage), "
                            "txrev (txrevise events), leafcutter (splice junctions), microarray, aptamer."),
            NestedField(10, "pmid", StringType(), doc="PubMed id of the study's publication."),
            NestedField(11, "study_type", StringType(), doc="'bulk' or 'single-cell' (pseudobulk eQTLs)."),
            NestedField(12, "eqtlcatalogue_release", StringType(), required=True,
                        doc="eQTL Catalogue release these rows describe: the N of dataset_metadata_rN.tsv, "
                            "e.g. '7'. Raw holds every landed release; filter on this to get one."),
            NestedField(13, "landed_in", StringType(), required=True,
                        doc="biocOnIce release that fetched these rows."),
        ),
        comment="eQTL Catalogue's per-release dataset metadata (data_tables/dataset_metadata_r<N>.tsv in "
                "github.com/eQTL-Catalogue/eQTL-Catalogue-resources), landed verbatim and whole as text. "
                "'NA' and the empty cell read as NULL. resource.eqtlcatalogue__dataset is the interpreted "
                "form. eQTL Catalogue release 7, CC BY 4.0; Kerimov et al. Nat Genet 2021.",
        properties={"bioc.license": "CC-BY-4.0"},
    ),
    "raw.eqtlcatalogue__tabix_ftp_paths": TableDef(
        schema=Schema(
            NestedField(1, "study_id", StringType(), doc="eQTL Catalogue study accession, e.g. QTS000001."),
            NestedField(2, "dataset_id", StringType(), required=True,
                        doc="eQTL Catalogue dataset accession, e.g. QTD000001; joins to "
                            "raw.eqtlcatalogue__dataset within one eqtlcatalogue_release."),
            NestedField(3, "study_label", StringType(), doc="Study name, e.g. Alasoo_2018, GTEx."),
            NestedField(4, "sample_group", StringType(),
                        doc="The study's group of samples the QTLs were mapped in, e.g. macrophage_naive."),
            NestedField(5, "tissue_id", StringType(),
                        doc="Ontology id of the tissue or cell type, with an underscore: CL_0000235."),
            NestedField(6, "tissue_label", StringType(), doc="Upstream's label for tissue_id, e.g. macrophage."),
            NestedField(7, "condition_label", StringType(), doc="Stimulation or condition, e.g. naive, IFNg."),
            NestedField(8, "sample_size", StringType(),
                        doc="Number of samples, as text. STALE against raw.eqtlcatalogue__dataset, which "
                            "upstream keeps correcting (120 of 758 differ on 2026-09-18); prefer that one."),
            NestedField(9, "quant_method", StringType(),
                        doc="Molecular trait quantified: ge, exon, tx, txrev, leafcutter, microarray, aptamer."),
            NestedField(10, "ftp_path", StringType(),
                        doc="FTP URI of the tabix-indexed summary statistics, as published."),
            NestedField(11, "ftp_cs_path", StringType(),
                        doc="FTP URI of the SuSiE credible sets file, as published."),
            NestedField(12, "ftp_lbf_path", StringType(),
                        doc="FTP URI of the SuSiE log Bayes factors file, as published."),
            NestedField(13, "eqtlcatalogue_release", StringType(), required=True,
                        doc="eQTL Catalogue release this table was landed with, e.g. '7'. The file itself "
                            "carries no release; the transform checks it names exactly that release's datasets."),
            NestedField(14, "landed_in", StringType(), required=True,
                        doc="biocOnIce release that fetched these rows."),
        ),
        comment="eQTL Catalogue's table of FTP paths (tabix/tabix_ftp_paths.tsv in "
                "github.com/eQTL-Catalogue/eQTL-Catalogue-resources), landed verbatim and whole as text: the "
                "only place the per-dataset file URIs are published. Its metadata columns duplicate "
                "raw.eqtlcatalogue__dataset and are the staler copy. eQTL Catalogue release 7, CC BY 4.0; "
                "Kerimov et al. Nat Genet 2021.",
        properties={"bioc.license": "CC-BY-4.0"},
    ),
    "resource.eqtlcatalogue__dataset": TableDef(
        schema=Schema(
            NestedField(1, "dataset_id", StringType(), required=True,
                        doc="eQTL Catalogue dataset accession, e.g. QTD000001: one study x sample group x "
                            "quantification method. The business key, and the resource_id of this dataset's "
                            "rows in resource.resource_relationship."),
            NestedField(2, "study_id", StringType(), required=True,
                        doc="eQTL Catalogue study accession, e.g. QTS000001. One study has many datasets."),
            NestedField(3, "study_label", StringType(), doc="Study name, e.g. Alasoo_2018, GTEx."),
            NestedField(4, "sample_group", StringType(),
                        doc="The study's group of samples the QTLs were mapped in, e.g. macrophage_naive."),
            NestedField(5, "taxon_id", IntegerType(), required=True,
                        doc="NCBI taxonomy id. Always 9606: the eQTL Catalogue is human only."),
            NestedField(6, "tissue_term_id", StringType(),
                        doc="Tissue or cell type as a CURIE, e.g. CL:0000235, UBERON:0002107; joins to "
                            "ontology.term.term_id for CL, UBERON and EFO (BTO is not landed). Upstream's "
                            "tissue_id with its underscore turned into a colon. The joinable form is "
                            "resource.resource_relationship (has_cell_type for CL, has_tissue otherwise)."),
            NestedField(7, "tissue_label", StringType(), doc="Upstream's label for the term, e.g. macrophage."),
            NestedField(8, "condition_label", StringType(), doc="Stimulation or condition, e.g. naive, IFNg."),
            NestedField(9, "sample_size", IntegerType(), doc="Number of samples (donors) the QTLs were mapped in."),
            NestedField(10, "quant_method", StringType(),
                        doc="Molecular trait quantified: ge (gene expression), exon, tx (transcript usage), "
                            "txrev (txrevise events), leafcutter (splice junctions), microarray, aptamer."),
            NestedField(11, "study_type", StringType(), doc="'bulk' or 'single-cell' (pseudobulk eQTLs)."),
            NestedField(12, "pmid", StringType(),
                        doc="PubMed id of the study's publication; joins to annotation.icite__publication.pmid."),
            NestedField(13, "license", StringType(), required=True,
                        doc="Always 'CC BY 4.0': the eQTL Catalogue's uniform licence (ebi.ac.uk/eqtl/License)."),
            NestedField(14, "sumstats_uri", StringType(), required=True,
                        doc="FTP URI of the dataset's tabix-indexed summary statistics, as upstream publishes "
                            "it; the index is this plus '.tbi', and https://ftp.ebi.ac.uk serves the same "
                            "path. Ends '.all.tsv.gz' for ge, microarray and aptamer datasets and '.cc.tsv.gz' "
                            "for exon, tx, txrev and leafcutter ones — upstream's choice of which file to "
                            "publish per method. Referenced, never landed (about 2 GB for one ge dataset). "
                            "EBI's firewall blacklists bursty tabix clients: pace requests."),
            NestedField(15, "credible_sets_uri", StringType(), required=True,
                        doc="FTP URI of the dataset's SuSiE fine-mapped credible sets (.credible_sets.tsv.gz). "
                            "Referenced, not landed."),
            NestedField(16, "lbf_uri", StringType(), required=True,
                        doc="FTP URI of the dataset's SuSiE log Bayes factors per variant "
                            "(.lbf_variable.txt.gz). Referenced, not landed."),
            NestedField(17, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(18, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("dataset_id",),
        comment="One row per eQTL Catalogue dataset: what was measured, in which tissue or cell type, from "
                "which study, and where its uniformly processed QTL summary statistics and fine-mapping "
                "results live on the EBI FTP — never the statistics themselves. A dataset dropped from a "
                "later release is closed by the ordinary merge rule. eQTL Catalogue release 7, CC BY 4.0; "
                "Kerimov et al. Nat Genet 2021.",
        properties={"bioc.column.taxon_id.prefix": "ncbitaxon",
                    "bioc.license": "CC-BY-4.0"},
    ),
    "raw.gwas_catalog__associations": _gwas_raw(
        _GWAS_ASSOCIATION,
        "The GWAS Catalog's ontology-annotated full associations download "
        "(gwas-catalog-associations_ontology-annotated-full.zip): one row per curated "
        "variant-trait association, with its study's columns repeated on each."),
    "raw.gwas_catalog__studies": _gwas_raw(
        _GWAS_STUDY,
        "The GWAS Catalog's studies download (gwas-catalog-download-studies-v1.0.3.1.txt): one "
        "row per study accession, including the two thirds with no curated association."),
    "clinical.gwas_catalog__study": TableDef(
        schema=Schema(
            NestedField(1, "study_accession", StringType(), required=True,
                        doc=_GWAS_STUDY["study_accession"]),
            NestedField(2, "pubmed_id", StringType(), doc="PubMed id of the publication."),
            NestedField(3, "first_author", StringType(), doc=_GWAS_STUDY["first_author"]),
            NestedField(4, "publication_date", StringType(), doc=_GWAS_STUDY["date"]),
            NestedField(5, "journal", StringType(), doc=_GWAS_STUDY["journal"]),
            NestedField(6, "link", StringType(), doc=_GWAS_STUDY["link"]),
            NestedField(7, "study_title", StringType(), doc=_GWAS_STUDY["study"]),
            NestedField(8, "date_added_to_catalog", StringType(),
                        doc=_GWAS_STUDY["date_added_to_catalog"]),
            NestedField(9, "disease_trait", StringType(),
                        doc="The disease or trait examined, as the curator worded it from the "
                            "paper. Free text; the ontology form is mapped_trait_ids."),
            NestedField(10, "initial_sample_size", StringType(),
                        doc=_GWAS_STUDY["initial_sample_size"]),
            NestedField(11, "replication_sample_size", StringType(),
                        doc=_GWAS_STUDY["replication_sample_size"]),
            NestedField(12, "platform", StringType(), doc=_GWAS_STUDY["platform"]),
            NestedField(13, "genotyping_technology", StringType(),
                        doc=_GWAS_STUDY["genotyping_technology"]),
            NestedField(14, "cohort", StringType(), doc=_GWAS_STUDY["cohort"]),
            NestedField(15, "association_count", IntegerType(),
                        doc="Number of curated associations the Catalog holds for this study, by "
                            "its own count. 0 for two thirds of studies: most are summary-"
                            "statistics depositions with no curated top associations. It counts "
                            "the associations file's whole-row duplicates, so for 39 studies at "
                            "2026-09-15 it exceeds their rows in clinical.gwas_catalog__association."),
            NestedField(16, "mapped_trait", StringType(),
                        doc="Label(s) of the mapped ontology term(s), ', '-separated as "
                            "published. For reading; labels contain commas, so join on "
                            "mapped_trait_ids."),
            NestedField(17, "mapped_trait_ids",
                        ListType(element_id=117, element_type=StringType(), element_required=False),
                        doc="CURIEs of the ontology term(s) the Catalog mapped the trait to, "
                            "sorted: EFO:0007789, MONDO:0005148, OBA:…, HP:…. Normalised from the "
                            "IRIs in raw exactly as ontology.term.term_id is, and every one is a "
                            "term EFO defines or imports, so unnest and join ontology.term on "
                            "ontology = 'efo'. An id that is not PREFIX_digits "
                            "(…/OBA_VT0001253, …/NCIT_C95746; 62 distinct at 2026-09-15) stays "
                            "the full IRI, because that is how ontology.term carries it. NULL "
                            "where the Catalog mapped nothing."),
            NestedField(18, "mapped_background_trait", StringType(),
                        doc=_GWAS_STUDY["mapped_background_trait"]),
            NestedField(19, "mapped_background_trait_ids",
                        ListType(element_id=119, element_type=StringType(), element_required=False),
                        doc="CURIEs of the background trait term(s), sorted; same form and join "
                            "as mapped_trait_ids. NULL for most studies."),
            NestedField(20, "full_summary_statistics", BooleanType(),
                        doc="True if the Catalog hosts full summary statistics for the study."),
            NestedField(21, "summary_stats_location", StringType(),
                        doc=_GWAS_STUDY["summary_stats_location"]),
            NestedField(22, "gxe", BooleanType(),
                        doc="True if the study analyses a gene-by-environment interaction."),
            NestedField(23, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(24, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("study_accession",),
        comment="GWAS Catalog studies, keyed by study accession (GCST…): one per publication and "
                "trait analysed, with the publication, the trait as reported and as mapped to "
                "EFO, sample descriptions, and where the summary statistics are. Includes "
                "studies with no curated association. Three columns the file declares but never "
                "fills stay in raw.gwas_catalog__studies; ancestry breakdowns are a separate "
                f"upstream file, not landed. {GWAS_LICENCE}",
        properties=_GWAS_PROPERTIES,
    ),
    "clinical.gwas_catalog__association": TableDef(
        schema=Schema(
            NestedField(1, "association_key", StringType(), required=True,
                        doc="md5 of the nine curated columns — study_accession, snps, "
                            "strongest_snp_risk_allele, p_value, p_value_text, or_beta, "
                            "ci_95_text, risk_allele_frequency, reported_genes — joined by "
                            "U+001F with NULL as empty. The download publishes no association "
                            "id and no smaller set of columns is unique (one study reports one "
                            "SNP under several models or strata), so an association is "
                            "identified by what was curated from the paper. A curation fix to "
                            "any of the nine therefore reads as one association retired and "
                            "another opened; a change to the Catalog's mapping columns is a new "
                            "version under the same key. Not an upstream identifier."),
            NestedField(2, "study_accession", StringType(), required=True,
                        doc="GWAS Catalog study accession; joins clinical.gwas_catalog__study, "
                            "which holds the publication, reported trait and sample columns."),
            NestedField(3, "snps", StringType(), doc=_GWAS_ASSOCIATION["snps"]),
            NestedField(4, "snp_ids",
                        ListType(element_id=104, element_type=StringType(), element_required=False),
                        doc="The individual variants in snps, split on its three separators, "
                            "distinct and sorted: one element on an ordinary row, several for a "
                            "haplotype or interaction. For list_contains(snp_ids, 'rs7903146'); "
                            "which separator joined them is only in snps."),
            NestedField(5, "strongest_snp_risk_allele", StringType(),
                        doc=_GWAS_ASSOCIATION["strongest_snp_risk_allele"]),
            NestedField(6, "p_value", StringType(), doc=_GWAS_ASSOCIATION["p_value"]),
            NestedField(7, "pvalue_mlog", DoubleType(), doc=_GWAS_ASSOCIATION["pvalue_mlog"]),
            NestedField(8, "p_value_text", StringType(), doc=_GWAS_ASSOCIATION["p_value_text"]),
            NestedField(9, "or_beta", DoubleType(), doc=_GWAS_ASSOCIATION["or_beta"]),
            NestedField(10, "ci_95_text", StringType(), doc=_GWAS_ASSOCIATION["ci_95_text"]),
            NestedField(11, "risk_allele_frequency", StringType(),
                        doc=_GWAS_ASSOCIATION["risk_allele_frequency"]),
            NestedField(12, "reported_genes", StringType(),
                        doc=_GWAS_ASSOCIATION["reported_genes"]),
            NestedField(13, "region", StringType(), doc=_GWAS_ASSOCIATION["region"]),
            NestedField(14, "chr_id", StringType(), doc=_GWAS_ASSOCIATION["chr_id"]),
            NestedField(15, "chr_pos", StringType(), doc=_GWAS_ASSOCIATION["chr_pos"]),
            NestedField(16, "context", StringType(), doc=_GWAS_ASSOCIATION["context"]),
            NestedField(17, "intergenic", BooleanType(),
                        doc="True if the variant lies between genes. NULL where unmapped."),
            NestedField(18, "mapped_gene", StringType(), doc=_GWAS_ASSOCIATION["mapped_gene"]),
            NestedField(19, "snp_gene_ids",
                        ListType(element_id=119, element_type=StringType(), element_required=False),
                        doc="Ensembl gene ids of the genes the variant lies within, in published "
                            "order; joins annotation.gene. NULL for an intergenic variant — see "
                            "upstream_gene_id / downstream_gene_id."),
            NestedField(20, "upstream_gene_id", StringType(),
                        doc=_GWAS_ASSOCIATION["upstream_gene_id"]),
            NestedField(21, "upstream_gene_distance", IntegerType(),
                        doc=_GWAS_ASSOCIATION["upstream_gene_distance"]),
            NestedField(22, "downstream_gene_id", StringType(),
                        doc=_GWAS_ASSOCIATION["downstream_gene_id"]),
            NestedField(23, "downstream_gene_distance", IntegerType(),
                        doc=_GWAS_ASSOCIATION["downstream_gene_distance"]),
            NestedField(24, "merged", BooleanType(),
                        doc="True if dbSNP has merged this rsID into another; snp_id_current is "
                            "then the survivor."),
            NestedField(25, "snp_id_current", StringType(),
                        doc=_GWAS_ASSOCIATION["snp_id_current"]),
            NestedField(26, "mapped_trait", StringType(),
                        doc="Label(s) of the mapped ontology term(s), ', '-separated as "
                            "published. For reading; labels contain commas, so join on "
                            "mapped_trait_ids."),
            NestedField(27, "mapped_trait_ids",
                        ListType(element_id=127, element_type=StringType(), element_required=False),
                        doc="CURIEs of the ontology term(s) the Catalog mapped this association's "
                            "trait to, sorted; same form and join as "
                            "clinical.gwas_catalog__study.mapped_trait_ids, and usually but not "
                            "always the same terms as the study's (196 studies differ at "
                            "2026-09-15). NULL on the few rows the Catalog left unmapped."),
            NestedField(28, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(29, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("association_key",),
        comment="Curated variant-trait associations from the GWAS Catalog (p < 1e-5 in the "
                "source paper), one row per association. A row is NOT one SNP: haplotype and "
                "SNP x SNP interaction rows name several in snps / snp_ids, and their position "
                "and gene columns are joined the same way. One study can report one SNP several "
                "times (per sex, model or conditional analysis, see p_value_text), so count "
                "rows, not (study, SNP) pairs. Traits are lists of EFO-form CURIEs: unnest "
                "mapped_trait_ids and join ontology.term on ontology = 'efo'. Study-level "
                "columns are in clinical.gwas_catalog__study, on study_accession. The file's "
                f"whole-row duplicates are collapsed here and kept in raw. Human, GRCh38. {GWAS_LICENCE}",
        properties={"bioc.license": _GWAS_PROPERTIES["bioc.license"]},
    ),
}




def is_rate_limit(err):
    """R2 Data Catalog's catalog-wide write limit, however pyiceberg surfaces it.

    The REST error's *message* is 'TooManyRequestsException: Rate limit
    exceeded…' — the class is a plain RESTError and the text has no '429', so
    matching on either alone misses it (it did, in production, 2026-09-18).
    """
    text = f"{type(err).__name__}: {err}"
    return "429" in text or "TooManyRequests" in text


def rate_limited(call):
    """Run one catalog write, waiting out R2 Data Catalog's catalog-wide 429.

    Creating a namespace or a table is a write request like any other, so with
    two loads running it can be refused for rate alone; the retrying commit
    paths (ncbi._commit, merge.overwrite) never see it because it fails before
    them. Anything but a 429 is raised as it is.
    """
    for _ in range(8):
        try:
            return call()
        except RESTError as err:
            if not is_rate_limit(err):
                raise
            time.sleep(65)
    return call()


def create(cat, identifier):
    """Create the table if absent, with its declared schema, comment and properties."""
    ns = identifier.split(".")[0]
    # Remembered on the catalog object itself, not in a module-level set keyed by
    # id(cat): ids are recycled once a catalog is garbage-collected, so a fresh
    # catalog (every test, or a second one in a process) inherited a dead
    # catalog's "already ensured" and then hit NoSuchNamespaceError.
    ensured = cat.__dict__.setdefault("_bioconice_namespaces", set())
    if ns not in ensured:
        rate_limited(lambda: cat.create_namespace_if_not_exists(
            ns, properties={"comment": NAMESPACES[ns]}))
        ensured.add(ns)
    d = TABLES[identifier]
    # An empty PartitionSpec() is Iceberg's unpartitioned spec, so this is
    # uniform whether or not the table declares partition_by.
    spec = PartitionSpec(*[
        PartitionField(source_id=d.schema.find_field(n).field_id, field_id=1000 + i,
                       transform=IdentityTransform(), name=n)
        for i, n in enumerate(d.partition_by)])
    table = rate_limited(lambda: cat.create_table_if_not_exists(
        identifier, schema=d.iceberg_schema(), partition_spec=spec,
        properties={"comment": d.comment, **d.properties}))
    return _evolve(table, d, identifier)


def _evolve(table, d, identifier):
    """Add columns the declaration has gained since the table was created.

    Without this a new column in a TableDef never reaches a live table, and the
    cast in merge.write fails on it. Only optional columns can be added: existing
    rows read NULL for them. A new *required* column (a row-key change, as in
    issue #94) has no value for the rows already there, so that is a rebuild and
    this refuses it rather than guessing.

    ponytail: additions only. A changed type, a dropped column or a changed doc
    string is left alone — handle those when one actually happens.
    """
    live = {f.name for f in table.schema().fields}
    missing = [f for f in d.schema.fields if f.name not in live]
    if not missing:
        return table
    required = [f.name for f in missing if f.required]
    if required:
        raise ValueError(f"{identifier}: declared required column(s) {required} are not in the "
                         "live table; Iceberg cannot add a required column to existing rows — "
                         "rebuild the table")
    with table.update_schema() as update:
        for f in missing:
            update.add_column(f.name, f.field_type, doc=f.doc)
    return table
