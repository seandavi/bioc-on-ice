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

FIRST_SEEN = "The biocOnIce release in which this record first appeared."
RETIRED_IN = (
    "The biocOnIce release in which this record disappeared upstream. "
    "NULL means the record is current — it does not mean unknown. "
    "Queries wanting current data must filter on retired_in IS NULL."
)
TAXON = "NCBI taxonomy id of the organism, e.g. 9606 for human. Part of the merge key."
COORD = "1-based-inclusive"


@dataclass(frozen=True)
class TableDef:
    schema: Schema
    comment: str
    properties: dict = field(default_factory=dict)


NAMESPACES = {
    "raw": "Source files landed verbatim, before any interpretation. Read these to "
           "re-derive or audit; query the annotation and reference namespaces instead.",
    "reference": "Genome assemblies and the sequences that make them up.",
    "annotation": "Gene, transcript and exon structure, and identifier cross-references.",
}

TABLES = {
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
            NestedField(12, "first_seen", StringType(), required=True, doc=FIRST_SEEN),
        ),
        comment="Ensembl GTF landed verbatim, one row per feature line. Deliberately has no "
                "merge key and no retired_in: a GTF line has no natural identity, and an "
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
            NestedField(3, "provider", StringType(), doc="Who published the assembly, e.g. Ensembl."),
            NestedField(4, "assembly_name", StringType(),
                        doc="Provider's assembly name, e.g. GRCh38.p14."),
            NestedField(5, "first_seen", StringType(), required=True, doc=FIRST_SEEN),
            NestedField(6, "retired_in", StringType(), doc=RETIRED_IN),
            identifier_field_ids=[1, 2],
        ),
        comment="Genome assemblies. One row per assembly per organism.",
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
            NestedField(7, "first_seen", StringType(), required=True, doc=FIRST_SEEN),
            NestedField(8, "retired_in", StringType(), doc=RETIRED_IN),
            identifier_field_ids=[1, 2],
        ),
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
            NestedField(7, "first_seen", StringType(), required=True, doc=FIRST_SEEN),
            NestedField(8, "retired_in", StringType(), doc=RETIRED_IN),
            identifier_field_ids=[1, 2],
        ),
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
            NestedField(12, "first_seen", StringType(), required=True, doc=FIRST_SEEN),
            NestedField(13, "retired_in", StringType(), doc=RETIRED_IN),
            identifier_field_ids=[1, 2, 3],
        ),
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
            NestedField(6, "source", StringType(), doc="Who asserts this mapping, e.g. Ensembl."),
            NestedField(7, "confidence", DoubleType(),
                        doc="Asserter's confidence where one is published; NULL where none is."),
            NestedField(8, "first_seen", StringType(), required=True, doc=FIRST_SEEN),
            NestedField(9, "retired_in", StringType(), doc=RETIRED_IN),
            identifier_field_ids=[1, 2, 3, 4, 5],
        ),
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
        identifier, schema=d.schema, properties={"comment": d.comment, **d.properties})
