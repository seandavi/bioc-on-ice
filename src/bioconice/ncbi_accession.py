"""NCBI gene2accession -> Iceberg, in the same two phases as ncbi.py.

Same FTP directory, same unversioned nightly regeneration, so the version is
again the retrieval date. Raw is landed whole and verbatim — every organism
and every accession status — and it is by far the largest landing in the
catalog (~1e9 rows upstream; gene2accession is a superset of gene2refseq), so
landing streams in record batches via ncbi._land. It is its own module rather
than a fourth entry in ncbi.COLUMNS so that this landing can run, fail, and
re-run on its own schedule without dragging the three small dumps along.

It is a further writer to `annotation.identifier_mapping`, and it merges in
its own call, so it needs its own scope: (taxon, source='NCBI_ACCESSION') —
deliberately NOT 'NCBI'. ncbi.transform already merges its gene_info +
gene2ensembl cross-references in ONE call scoped to source='NCBI'; a second
merge call into that same scope would retire that call's rows on every run,
the flip-flop effect (ADR-0004). One merge call, one writer, one source
value. Folding this derive into ncbi.py's call was considered in the reuse
pass and rejected: a separate merge call is exactly what ADR-0004's
writer-scope rule prescribes, and coupling the two ingest paths would force
them to rerun together.

Accession versions are kept exactly as the file gives them (NM_000546.6, not
NM_000546): the versioned form is what the record asserts, and stripping the
version is interpretation a reader can do with split_part.
"""

import duckdb
from pyiceberg.expressions import And, EqualTo

from . import merge
from .ncbi import DATA, _derive, _land, _where, tsv

URL = f"{DATA}gene2accession.gz"

# File order, upstream's dots turned into underscores ('accession.version' is
# not a valid column name; the upstream names are recorded in the column docs).
# Same contract as ncbi.COLUMNS: auto_detect off, names from the spec.
COLUMNS = (
    "{'taxon_id':'INTEGER','gene_id':'VARCHAR','status':'VARCHAR',"
    "'rna_nucleotide_accession_version':'VARCHAR','rna_nucleotide_gi':'VARCHAR',"
    "'protein_accession_version':'VARCHAR','protein_gi':'VARCHAR',"
    "'genomic_nucleotide_accession_version':'VARCHAR','genomic_nucleotide_gi':'VARCHAR',"
    "'start_position_on_the_genomic_accession':'VARCHAR',"
    "'end_position_on_the_genomic_accession':'VARCHAR','orientation':'VARCHAR',"
    "'assembly':'VARCHAR','mature_peptide_accession_version':'VARCHAR',"
    "'mature_peptide_gi':'VARCHAR','symbol':'VARCHAR'}"
)


def land_raw(cat, release, url=None):
    """Phase 1: stream gene2accession verbatim and whole into raw.ncbi__gene2accession."""
    # '-' is NCBI's null marker AND the minus strand, and nullstr cannot tell them
    # apart. NCBI writes '?' for an unknown orientation, so on a row that has
    # positions a NULL can only have been the minus strand (checked on the first
    # 2M rows, 2026-09-18: positioned rows are '+'/'-', every other row is '?').
    source = ("(SELECT * REPLACE (CASE WHEN start_position_on_the_genomic_accession IS NOT NULL "
              f"THEN coalesce(orientation, '-') ELSE orientation END AS orientation) "
              f"FROM {tsv(url or URL, COLUMNS)})")
    n = _land(cat, release, "raw.ncbi__gene2accession", source)
    merge.manifest(cat, release, "ncbi_gene2accession", URL, n)
    return n


def transform(cat, release, taxon=None):
    """Phase 2: ENTREZ <-> accession cross-references, one species or (default) all.

    RefSeq accessions are two capitals and an underscore (NM_/NR_/XM_/XR_ RNA,
    NP_/XP_/YP_/WP_ protein), which also holds for every future RefSeq prefix.
    The underscore alone is not enough on the protein side: PDB chains
    ('1FX0_A.1') carry one too. GenBank/INSDC accessions never contain an
    underscore, which is what selects GENBANK_GENOMIC. GenBank RNA and protein accessions are not
    emitted (the issue asks for the RefSeq ones), and neither are RefSeq
    genomic accessions (NC_/NT_/NW_): a REFSEQ_GENOMIC namespace can be
    derived later from the same raw rows.

    Like the other dumps, the file arrives sorted by tax_id upstream, so a
    single-taxon filter prunes nearly every Parquet row group on min/max stats.
    """
    con = duckdb.connect()
    con.register("acc", cat.load_table("raw.ncbi__gene2accession").scan(
        row_filter=_where(taxon)).to_arrow())

    mapping = con.sql("""
        SELECT DISTINCT source_namespace, source_id, target_namespace, target_id,
               taxon_id, 'NCBI_ACCESSION' AS source, NULL::DOUBLE AS confidence
        FROM (
            SELECT 'ENTREZ' AS source_namespace, gene_id AS source_id,
                   'REFSEQ_RNA' AS target_namespace,
                   rna_nucleotide_accession_version AS target_id, taxon_id
            FROM acc WHERE regexp_matches(rna_nucleotide_accession_version, '^[A-Z]{2}_')
          UNION ALL
            SELECT 'ENTREZ', gene_id, 'REFSEQ_PROTEIN', protein_accession_version, taxon_id
            FROM acc WHERE regexp_matches(protein_accession_version, '^[A-Z]{2}_')
          UNION ALL
            SELECT 'ENTREZ', gene_id, 'GENBANK_GENOMIC',
                   genomic_nucleotide_accession_version, taxon_id
            FROM acc WHERE NOT contains(genomic_nucleotide_accession_version, '_')
        )
        WHERE source_id IS NOT NULL AND target_id IS NOT NULL
    """).to_arrow_table()

    # The scope names THIS writer, not 'NCBI': ncbi.transform merges its own
    # cross-references in a single call under source='NCBI', and a second merge
    # call into that scope would retire those rows on every run (ADR-0004).
    return {"annotation.identifier_mapping": merge.merge(
        cat, "annotation.identifier_mapping", mapping, release,
        And(_where(taxon), EqualTo("source", "NCBI_ACCESSION")))}


def ingest(cat, release, taxa=None):
    return {"raw.ncbi__gene2accession": land_raw(cat, release),
            **_derive(transform, cat, release, taxa)}
