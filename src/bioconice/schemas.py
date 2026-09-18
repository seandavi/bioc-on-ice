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

from pyiceberg.partitioning import PartitionField, PartitionSpec
from pyiceberg.schema import Schema
from pyiceberg.transforms import IdentityTransform
from pyiceberg.types import (
    BooleanType, DoubleType, IntegerType, LongType, NestedField, StringType,
)

MULTI_VALUED = (
    "Multi-valued: ontology term ids the source publishes for this field, "
    "pipe-joined in one string, same order as the paired _labels column. "
    "Joins to ontology.term once the ontology namespace lands (issue #83); "
    "readable without it in the meantime."
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
}

# (short name, licence) for every OBO ontology this catalog lands, kept here rather than
# imported from obo.py (which needs schemas.TableDef) to avoid a circular import. obo.REGISTRY
# is the same list plus each ontology's release URL; test_obo.py asserts the two stay in sync.
_OBO_ONTOLOGIES = (
    ("cl", "CC-BY-4.0"), ("uberon", "CC-BY-3.0"), ("mondo", "CC-BY-4.0"),
    ("efo", "Apache-2.0"), ("hsapdv", "CC-BY-4.0"), ("mmusdv", "CC-BY-4.0"), ("go", "CC-BY-4.0"),
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
            NestedField(9, "assay_term_ids", StringType(), doc=MULTI_VALUED + " EFO terms."),
            NestedField(10, "assay_labels", StringType(), doc="Pipe-joined labels paired with assay_term_ids, same order."),
            NestedField(11, "tissue_term_ids", StringType(), doc=MULTI_VALUED + " UBERON (or CL) terms."),
            NestedField(12, "tissue_labels", StringType(), doc="Pipe-joined labels paired with tissue_term_ids, same order."),
            NestedField(13, "disease_term_ids", StringType(), doc=MULTI_VALUED + " MONDO terms, or PATO:0000461 for 'normal'."),
            NestedField(14, "disease_labels", StringType(), doc="Pipe-joined labels paired with disease_term_ids, same order."),
            NestedField(15, "cell_type_term_ids", StringType(), doc=MULTI_VALUED + " CL terms, or 'unknown'."),
            NestedField(16, "cell_type_labels", StringType(), doc="Pipe-joined labels paired with cell_type_term_ids, same order."),
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
                        doc="Short ontology name: cl, uberon, mondo, efo, hsapdv, mmusdv, go. Part "
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
}




def create(cat, identifier):
    """Create the table if absent, with its declared schema, comment and properties."""
    ns = identifier.split(".")[0]
    # Remembered on the catalog object itself, not in a module-level set keyed by
    # id(cat): ids are recycled once a catalog is garbage-collected, so a fresh
    # catalog (every test, or a second one in a process) inherited a dead
    # catalog's "already ensured" and then hit NoSuchNamespaceError.
    ensured = cat.__dict__.setdefault("_bioconice_namespaces", set())
    if ns not in ensured:
        cat.create_namespace_if_not_exists(ns, properties={"comment": NAMESPACES[ns]})
        ensured.add(ns)
    d = TABLES[identifier]
    # An empty PartitionSpec() is Iceberg's unpartitioned spec, so this is
    # uniform whether or not the table declares partition_by.
    spec = PartitionSpec(*[
        PartitionField(source_id=d.schema.find_field(n).field_id, field_id=1000 + i,
                       transform=IdentityTransform(), name=n)
        for i, n in enumerate(d.partition_by)])
    return cat.create_table_if_not_exists(
        identifier, schema=d.iceberg_schema(), partition_spec=spec,
        properties={"comment": d.comment, **d.properties})
