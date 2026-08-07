"""Declared Iceberg schemas — the single source of table structure and meaning.

Tables are never created from an inferred Arrow schema. Two things required by
SPEC.md cannot be expressed that way: identifier fields, which are the merge
key, and per-column `doc`, which is what makes the catalog self-describing.

A column exists here only once something populates it. Columns whose source has
not landed yet (gene descriptions, assembly checksums) are added by schema
evolution when it does, rather than shipped as permanent NULLs that read as
"we have this" when we do not.
"""

from dataclasses import dataclass, field

from pyiceberg.schema import Schema
from pyiceberg.types import (
    BooleanType, DoubleType, IntegerType, LongType, NestedField, StringType,
)

VALID_FROM = (
    "The biocOnIce release from which this version of the record is valid. "
    "A row is one *version*: any change to any attribute closes the previous "
    "row and opens a new one, so the value here is not necessarily when the "
    "record first existed."
)
VALID_TO = (
    "The biocOnIce release at which this version stopped being current, "
    "exclusive. NULL means this is the current version — it does not mean "
    "unknown. Queries wanting current data must filter on valid_to IS NULL; "
    "queries wanting release R want "
    "valid_from <= R AND (valid_to IS NULL OR valid_to > R)."
)
TAXON = "NCBI taxonomy id of the organism, e.g. 9606 for human. Part of the merge key."
COORD = "1-based-inclusive"
ALPHA_DIVERSITY = (
    "Direction this alpha-diversity metric moved in group 1: 'increased', 'decreased' "
    "or 'unchanged'. NOT a diversity value — there is no number here to plot. NULL "
    "where the study did not report the metric, which is the majority."
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
}

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
    "raw.ncbi_gene2ensembl": TableDef(
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
    "raw.ncbi_gene_info": TableDef(
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
    "raw.ncbi_gene_history": TableDef(
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
    "raw.bugsigdb_full_dump": TableDef(
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
    "raw.ensembl_gtf": TableDef(
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
            NestedField(3, "provider", StringType(), required=True,
                        doc="Who published the assembly, e.g. Ensembl. Part of the business key: two providers may describe the same assembly, and neither may retire the other's row."),
            NestedField(4, "assembly_name", StringType(),
                        doc="Provider's assembly name, e.g. GRCh38.p14."),
            NestedField(5, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(6, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("genome_id", "taxon_id", "provider"),
        comment="Genome assemblies. One row per assembly per organism per provider.",
        properties={"bioc.column.genome_id.prefix": "insdc.gca",
                    "bioc.column.taxon_id.prefix": "ncbitaxon"},
    ),
    "annotation.gene": TableDef(
        schema=Schema(
            NestedField(1, "gene_id", StringType(), required=True,
                        doc="Ensembl stable gene id without version, e.g. ENSG00000141510. "
                            "Part of the merge key; an upstream version bump updates this row "
                            "rather than creating a new one."),
            NestedField(2, "taxon_id", IntegerType(), required=True, doc=TAXON),
            NestedField(3, "version", StringType(),
                        doc="Upstream record version at this release, e.g. 21. Changes over time."),
            NestedField(4, "symbol", StringType(),
                        doc="Official gene symbol, e.g. TP53. NULL for genes with no assigned name."),
            NestedField(5, "gene_type", StringType(),
                        doc="Ensembl biotype, e.g. protein_coding, lncRNA."),
            NestedField(6, "source", StringType(),
                        doc="Ensembl annotation source, e.g. ensembl_havana. This is curation "
                            "provenance from the GTF, not biocOnIce provenance."),
            NestedField(7, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(8, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("gene_id", "taxon_id"),
        comment="Genes as Ensembl defines them. One row per gene per organism. Join to "
                "annotation.transcript on gene_id. Descriptions and cytogenetic bands are not "
                "here: they come from NCBI, keyed by Entrez id, in annotation.ncbi_gene.",
        properties={"bioc.column.gene_id.prefix": "ensembl",
                    "bioc.column.taxon_id.prefix": "ncbitaxon"},
    ),
    "annotation.ncbi_gene": TableDef(
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
                        doc="Ensembl stable transcript id without version, e.g. ENST00000269305."),
            NestedField(2, "taxon_id", IntegerType(), required=True, doc=TAXON),
            NestedField(3, "gene_id", StringType(),
                        doc="Ensembl stable gene id of the parent gene. Joins to annotation.gene."),
            NestedField(4, "version", StringType(), doc="Upstream record version at this release."),
            NestedField(5, "biotype", StringType(),
                        doc="Transcript biotype, e.g. protein_coding, retained_intron."),
            NestedField(6, "canonical", BooleanType(),
                        doc="True if Ensembl tags this as the canonical transcript of its gene."),
            NestedField(7, "valid_from", StringType(), required=True, doc=VALID_FROM),
            NestedField(8, "valid_to", StringType(), doc=VALID_TO),
        ),
        business_key=("transcript_id", "taxon_id"),
        comment="Transcripts. One row per transcript; a gene has many.",
        properties={"bioc.column.transcript_id.prefix": "ensembl",
                    "bioc.column.gene_id.prefix": "ensembl",
                    "bioc.column.taxon_id.prefix": "ncbitaxon"},
    ),
    "annotation.exon": TableDef(
        schema=Schema(
            NestedField(1, "exon_id", StringType(), required=True,
                        doc="Ensembl stable exon id without version, e.g. ENSE00002064269. "
                            "NOT unique on its own: one exon is shared by every transcript "
                            "containing it, so the key is (exon_id, transcript_id, taxon_id)."),
            NestedField(2, "transcript_id", StringType(), required=True,
                        doc="Transcript this row places the exon in. Joins to annotation.transcript."),
            NestedField(3, "taxon_id", IntegerType(), required=True, doc=TAXON),
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
        business_key=("exon_id", "transcript_id", "taxon_id"),
        comment="Exons in transcript context, carrying coding bounds. Stands in for TxDb's "
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
    "raw.ncbi_gene2pubmed": TableDef(
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
    "annotation.gene_pubmed": TableDef(
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
}


def create(cat, identifier):
    """Create the table if absent, with its declared schema, comment and properties."""
    ns = identifier.split(".")[0]
    cat.create_namespace_if_not_exists(ns, properties={"comment": NAMESPACES[ns]})
    d = TABLES[identifier]
    return cat.create_table_if_not_exists(
        identifier, schema=d.iceberg_schema(), properties={"comment": d.comment, **d.properties})
