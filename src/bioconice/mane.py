"""MANE summary -> Iceberg: the RefSeq <-> Ensembl matched-transcript bridge.

MANE (Matched Annotation from NCBI and EMBL-EBI) names, per human protein-coding
gene, one transcript that RefSeq and Ensembl annotate identically — MANE Select
— plus a few MANE Plus Clinical ones where Select alone cannot report every
known pathogenic variant. The summary file is one row per matched pair (19,437
in v1.5), small enough to read as one Arrow table, so this follows hgnc.py
rather than the streaming NCBI dumps.

**Versioned by release number**, read from the file name
(`MANE.GRCh38.v1.5.summary.txt.gz` -> '1.5'): that is the form MANE itself
cites. With no `url`, the newest file name is read off the `current/` index and
fetched from its own `release_X.Y/` directory, so the manifest url names the
release rather than a moving alias. A file whose name carries no version is
refused rather than given an invented one. Raw is replaced per version, so
re-landing is idempotent and versions accumulate.

The column contract is the header, checked whole, as in hgnc.py.

**`ncbi.tsv` is deliberately not used.** Its nullstr='-' would turn every
minus-strand `chr_strand` (9,588 of 19,437 rows) into NULL — the trap
gene2accession's orientation column fell into. This file's missing marker is
the empty cell (HGNC_ID on 52 rows; both protein columns on the 70 NR_ rows),
and '-' is only ever the strand.

**Identifier versions.** MANE matches exact versions (NM_000546.6 =
ENST00000269305.9), and each side is written in the form the catalog already
uses for it, so rows join without string surgery. RefSeq accessions keep their
version, as ncbi_accession.py writes the REFSEQ_RNA / REFSEQ_PROTEIN namespaces.
Ensembl ids are split into the stable id and a version column, as
annotation.gene / annotation.transcript carry them, so ensembl_transcript_id
joins annotation.transcript.transcript_id directly. REFSEQ_RNA is the existing
namespace string; ENSEMBL_TRANSCRIPT is new with this writer (no one wrote
Ensembl transcript ids before), named on the same pattern and unversioned like
the ENSEMBL gene namespace.

It is a further writer to `annotation.identifier_mapping`, under its own scope
(taxon 9606, source='MANE'), so it can neither retire nor be retired by the
NCBI, Ensembl and HGNC writers (ADR-0004).

ponytail: only the summary is landed. The release's GFF/GTF/FASTA files restate
these transcripts' structure and sequence, which GENCODE (#43) and RefSeq GFF3
(#44) will carry; `changed_select_accessions` and
`protein_coding_genes_not_in_mane` are small TSVs worth their own raw tables
when something needs them.
"""

import re
import urllib.request

import duckdb
from pyiceberg.expressions import AlwaysTrue, And, EqualTo

from . import merge

BASE = "https://ftp.ncbi.nlm.nih.gov/refseq/MANE/MANE_human/"
SUMMARY = r"MANE\.GRCh38\.v(\d+(?:\.\d+)*)\.summary\.txt"
TAXON = 9606

# The file's header, in file order. Raw lowercases these and drops the '#'.
COLUMNS = (
    "#NCBI_GeneID", "Ensembl_Gene", "HGNC_ID", "symbol", "name", "RefSeq_nuc", "RefSeq_prot",
    "Ensembl_nuc", "Ensembl_prot", "MANE_status", "GRCh38_chr", "chr_start", "chr_end",
    "chr_strand",
)


def _current():
    """The newest summary's URL, in its own release directory rather than `current/`."""
    with urllib.request.urlopen(f"{BASE}current/") as r:
        found = re.search(SUMMARY, r.read().decode("utf-8", "replace"))
    if not found:
        raise SystemExit(f"mane: no summary file listed under {BASE}current/")
    return f"{BASE}release_{found.group(1)}/{found.group(0)}.gz"


def land_raw(cat, release, url=None):
    """Phase 1: the summary, verbatim and whole, replaced per MANE version.

    Returns (version, rows). `url` is a specific release's file, or a local copy.
    """
    url = url or _current()
    named = re.search(SUMMARY, url)
    if not named:
        raise SystemExit(f"mane: {url} does not name its release (MANE.GRCh38.vX.Y.summary.txt); "
                         "refusing to invent a version")
    version = named.group(1)

    con = duckdb.connect()
    # The dialect is stated rather than sniffed: no quoting, no comment character
    # (the header itself starts with '#'), empty cell for missing. all_varchar
    # keeps raw unparsed.
    source = (f"read_csv('{url}', header=true, all_varchar=true, delim='\\t', quote='', "
              "escape='', comment='', nullstr='')")
    if (header := tuple(con.sql(f"SELECT * FROM {source} LIMIT 0").columns)) != COLUMNS:
        raise SystemExit(f"mane: {url} header is not the declared one; "
                         f"differs in {sorted(set(header) ^ set(COLUMNS))}")
    select = ", ".join(f'"{c}" AS {c.lstrip("#").lower()}' for c in COLUMNS)
    arrow = con.sql(f"SELECT {select}, '{version}' AS mane_version, '{release}' AS landed_in "
                    f"FROM {source}").to_arrow_table()
    if not arrow.num_rows:
        raise SystemExit(f"mane: {url} yielded no rows")

    n = merge.write(cat, "raw.ncbi__mane_summary", arrow, EqualTo("mane_version", version))
    merge.manifest(cat, release, "mane", url, n, version=version, method="release_number")
    return version, n


def transform(cat, release, version):
    """Phase 2: the matched pairs, and RefSeq -> Ensembl transcript cross-references.

    Scoped to `version`'s rows: raw accumulates every landed version.
    """
    con = duckdb.connect()
    con.register("raw", cat.load_table("raw.ncbi__mane_summary").scan(
        row_filter=EqualTo("mane_version", version)).to_arrow())

    # The gene's name, its Ensembl version and the GRCh38 placement stay in raw:
    # they are facts about the gene and the assembly that other tables own, and
    # carrying them would open a new version row here whenever they moved.
    # 'GeneID:7157' loses its prefix to join annotation.ncbi__gene; 'HGNC:11998'
    # keeps it, the form the HGNC namespace already uses.
    transcript = con.sql(f"""
        SELECT split_part(ensembl_nuc, '.', 1) AS ensembl_transcript_id, {TAXON} AS taxon_id,
               split_part(ensembl_nuc, '.', 2) AS ensembl_transcript_version,
               refseq_nuc AS refseq_rna, mane_status,
               replace(ncbi_geneid, 'GeneID:', '') AS gene_id,
               split_part(ensembl_gene, '.', 1) AS ensembl_gene_id, hgnc_id, symbol,
               refseq_prot AS refseq_protein,
               split_part(ensembl_prot, '.', 1) AS ensembl_protein_id,
               split_part(ensembl_prot, '.', 2) AS ensembl_protein_version
        FROM raw
    """).to_arrow_table()

    # One direction, like every other writer. Both MANE statuses are matched
    # pairs; the status itself is in annotation.mane__transcript.
    mapping = con.sql(f"""
        SELECT DISTINCT 'REFSEQ_RNA' AS source_namespace, refseq_nuc AS source_id,
               'ENSEMBL_TRANSCRIPT' AS target_namespace,
               split_part(ensembl_nuc, '.', 1) AS target_id, {TAXON} AS taxon_id,
               'MANE' AS source, NULL::DOUBLE AS confidence
        FROM raw
    """).to_arrow_table()

    return {
        # MANE is this table's only writer, so its scope is the whole table.
        "annotation.mane__transcript": merge.merge(
            cat, "annotation.mane__transcript", transcript, release, AlwaysTrue()),
        # The scope names THIS writer: NCBI, Ensembl and HGNC write the same taxon.
        "annotation.identifier_mapping": merge.merge(
            cat, "annotation.identifier_mapping", mapping, release,
            And(EqualTo("taxon_id", TAXON), EqualTo("source", "MANE"))),
    }


def ingest(cat, release, url=None):
    version, n = land_raw(cat, release, url)
    return {"raw.ncbi__mane_summary": n, **transform(cat, release, version)}
