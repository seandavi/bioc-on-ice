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
        comment="NCBI gene2ensembl landed verbatim, filtered to the taxa we ingest. NCBI's "
                "'-' placeholder is read as NULL. Regenerated nightly upstream, so it has no "
                "release: the retrieval date is the version, per NLM's own citation form.",
        properties={"bioc.column.taxon_id.prefix": "ncbitaxon",
                    "bioc.column.gene_id.prefix": "ncbigene",
                    "bioc.column.ensembl_gene_id.prefix": "ensembl"},
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
        comment="Genes. One row per gene per organism. Join to annotation.transcript on gene_id.",
        properties={"bioc.column.gene_id.prefix": "ensembl",
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
}


def create(cat, identifier):
    """Create the table if absent, with its declared schema, comment and properties."""
    ns = identifier.split(".")[0]
    cat.create_namespace_if_not_exists(ns, properties={"comment": NAMESPACES[ns]})
    d = TABLES[identifier]
    return cat.create_table_if_not_exists(
        identifier, schema=d.iceberg_schema(), properties={"comment": d.comment, **d.properties})
