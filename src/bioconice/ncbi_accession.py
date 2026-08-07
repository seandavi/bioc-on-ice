"""NCBI gene2accession -> Iceberg, in the same two phases as ncbi.py.

Same FTP directory, same unversioned nightly regeneration, so the version is
again the retrieval date. Raw is landed whole and verbatim — every organism
and every accession status — and it is by far the largest landing in the
catalog (~1e9 rows upstream; gene2accession is a superset of gene2refseq), so
landing streams in record batches exactly as ncbi._land does. It is its own
module rather than a fourth entry in ncbi.COLUMNS so that this landing can
run, fail, and re-run on its own schedule without dragging the three small
dumps along.

It is a further writer to `annotation.identifier_mapping`, and it merges in
its own call, so it needs its own scope: (taxon, source='NCBI_ACCESSION') —
deliberately NOT 'NCBI'. ncbi.transform already merges its gene_info +
gene2ensembl cross-references in ONE call scoped to source='NCBI'; a second
merge call into that same scope would retire that call's rows on every run,
the flip-flop effect (ADR-0004). One merge call, one writer, one source
value. Folding this derive into ncbi.py's single call — retiring
'NCBI_ACCESSION' — is a candidate for the planned reuse pass, not something
to do by side effect from a sibling module.

Accession versions are kept exactly as the file gives them (NM_000546.6, not
NM_000546): the versioned form is what the record asserts, and stripping the
version is interpretation a reader can do with split_part.
"""

from datetime import datetime, timezone

import duckdb
import pyarrow as pa
from pyiceberg.expressions import And, EqualTo

from . import merge, schemas
from .ensembl import _write

URL = "https://ftp.ncbi.nlm.nih.gov/gene/DATA/gene2accession.gz"

# File order, upstream's dots turned into underscores ('accession.version' is
# not a valid column name; the upstream names are recorded in the column docs).
# `auto_detect` is off, so a column NCBI inserts or reorders fails loudly here
# instead of quietly shifting every value one to the left.
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

BATCH = 1_000_000


def land_raw(cat, release, url=None):
    """Phase 1: gene2accession, verbatim and whole, streamed in batches.

    Same replace-first-batch-then-append shape as ncbi._land, and the same
    ponytail there applies: a crash between batches leaves a partial landing
    that a re-run repairs.
    """
    identifier = "raw.ncbi__gene2accession"
    table = schemas.create(cat, identifier)
    arrow_schema = table.schema().as_arrow()
    con = duckdb.connect()
    reader = con.sql(f"""
        SELECT *, '{release}' AS landed_in
        FROM read_csv('{url or URL}', sep='\t', header=true,
                      auto_detect=false, columns={COLUMNS}, nullstr='-')
    """).to_arrow_reader(BATCH)

    n = 0
    for batch in reader:
        # Casting to the declared schema is the check: a null in an identifier
        # field fails here rather than landing quietly.
        arrow = pa.Table.from_batches([batch]).cast(arrow_schema)
        if n:
            table.append(arrow)
        else:
            table.overwrite(arrow)
        n += arrow.num_rows
    if not n:
        # Otherwise a bad URL silently leaves the previous landing in place and
        # reports success.
        raise SystemExit(f"{identifier}: {url or URL} yielded no rows")
    _manifest(cat, release, n)
    return {identifier: n}


def _manifest(cat, release, rows):
    """Record what this release was built from — ADR-0007.

    Keyed apart from ncbi.py's 'ncbi_gene' row, since the two land
    independently and would otherwise clobber each other's manifest.
    """
    now = datetime.now(timezone.utc)
    con = duckdb.connect()
    arrow = con.sql(f"""
        SELECT '{release}' AS release, 'ncbi_gene2accession' AS source,
               '{now.date()}' AS source_version,
               'retrieval_date' AS version_method,
               '{now.isoformat(timespec="seconds")}' AS retrieved_at,
               '{URL}' AS url, NULL::VARCHAR AS checksum, {rows}::BIGINT AS row_count
    """).to_arrow_table()
    _write(cat, "provenance.release", arrow,
           And(EqualTo("release", release), EqualTo("source", "ncbi_gene2accession")))


def transform(cat, release, taxon):
    """Phase 2: ENTREZ <-> accession cross-references for one species.

    RefSeq accessions are the underscored forms (NM_/NR_/XM_/XR_ RNA,
    NP_/XP_/YP_ protein) and GenBank/INSDC accessions never contain an
    underscore, so the underscore is the discriminator — it also holds for
    every future RefSeq prefix. GenBank RNA and protein accessions are not
    emitted (the issue asks for the RefSeq ones), and neither are RefSeq
    genomic accessions (NC_/NT_/NW_): a REFSEQ_GENOMIC namespace can be
    derived later from the same raw rows.

    Like the other dumps, the file arrives sorted by tax_id upstream, so the
    taxon filter prunes nearly every Parquet row group on min/max stats.
    """
    raw = cat.load_table("raw.ncbi__gene2accession").scan(
        row_filter=EqualTo("taxon_id", taxon)).to_arrow()
    con = duckdb.connect()
    con.register("acc", raw)

    mapping = con.sql(f"""
        SELECT DISTINCT source_namespace, source_id, target_namespace, target_id,
               {taxon}::INTEGER AS taxon_id, 'NCBI_ACCESSION' AS source,
               NULL::DOUBLE AS confidence
        FROM (
            SELECT 'ENTREZ' AS source_namespace, gene_id AS source_id,
                   'REFSEQ_RNA' AS target_namespace,
                   rna_nucleotide_accession_version AS target_id
            FROM acc WHERE contains(rna_nucleotide_accession_version, '_')
          UNION ALL
            SELECT 'ENTREZ', gene_id, 'REFSEQ_PROTEIN', protein_accession_version
            FROM acc WHERE contains(protein_accession_version, '_')
          UNION ALL
            SELECT 'ENTREZ', gene_id, 'GENBANK_GENOMIC',
                   genomic_nucleotide_accession_version
            FROM acc WHERE NOT contains(genomic_nucleotide_accession_version, '_')
        )
        WHERE source_id IS NOT NULL AND target_id IS NOT NULL
    """).to_arrow_table()

    # The scope names THIS writer, not 'NCBI': ncbi.transform merges its own
    # cross-references in a single call under source='NCBI', and a second merge
    # call into that scope would retire those rows on every run (ADR-0004).
    return {"annotation.identifier_mapping": merge.merge(
        cat, "annotation.identifier_mapping", mapping, release,
        And(EqualTo("taxon_id", taxon), EqualTo("source", "NCBI_ACCESSION")))}


def ingest(cat, release, taxa):
    out = dict(land_raw(cat, release))
    for taxon in taxa:
        for k, v in transform(cat, release, taxon).items():
            out[f"{k} [{taxon}]"] = v
    return out
