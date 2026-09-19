"""RNAcentral id mappings -> Iceberg, in the same two phases as the other sources.

RNAcentral is the non-coding RNA sequence aggregator: one `URS` id per distinct
sequence, cross-referenced to the records of its member databases (ENA, Rfam,
Ensembl, RefSeq, miRBase, HGNC, GtRNAdb, …). `id_mapping.tsv.gz` is that whole
cross-reference set as one headerless TSV — 264,333,345 rows over 1,117,109
taxa and 56 databases in release 27 — and it is the ncRNA coverage OrgDb lacks.
CC0 from release 20; the caveat is recorded in the raw table's comment.

**Versioned by release number.** `current_release/` is a moving alias, so it is
only asked which release it is; the file is read from `releases/NN.0/`, which
stays put. Raw is landed whole and verbatim, streamed via ncbi._land, and holds
the latest release only (as raw.icite__metadata does): older releases remain
immutable upstream.

It is a further writer to `annotation.identifier_mapping`, under its own scope
(taxon, source='RNACENTRAL'), so it never retires the rows Ensembl, NCBI or HGNC
assert about the same taxon (ADR-0004).

The source id is the taxon-qualified `URS0000626831_9606`, not the bare URS. A
URS names a sequence, which can occur in hundreds of organisms; the
cross-references, the RNA type and RNAcentral's own pages and API all hang off
the (sequence, organism) pair, and every other id in identifier_mapping names
one organism's entity by itself. taxon_id being a key column would make the
bare form unambiguous per row but not per id. HGNC's rna_central_id is the bare
form; joining it is `|| '_9606'`.

Target namespaces are the file's database names, already upper-case, which is
the convention ncbi.transform's dbXrefs established — so HGNC ('HGNC:35391'),
MGI ('MGI:102485'), MIRBASE, RGD, SGD, TAIR and WORMBASE ids land beside NCBI's
in the same namespace and the same form (checked against gene_info,
2026-09-18). FLYBASE and ZFIN ids are transcripts here (FBtr…, ZDB-TSCRIPT-…)
and genes from NCBI; both forms name their own type, so they share the
namespace without colliding. Two are renamed:

  ENSEMBL*  -> ENSEMBL_TRANSCRIPT  RNAcentral's Ensembl ids are transcripts, and
                                   'ENSEMBL' already means genes here; unlike
                                   ENST…, the divisions' ids ('CRE32083') do not
                                   say which they are. It is the namespace
                                   mane.py writes, unversioned ENST… in both,
                                   so the two join. All six fold in: the
                                   divisions are one authority, and every
                                   ENSEMBL_GENCODE row duplicates an ENSEMBL row
                                   (checked, release 27), so nothing is lost.
  REFSEQ    -> REFSEQ_RNA          every id is an NR_ accession; one authority
                                   under two names would be the real bug. They
                                   are unversioned as published (NR_031728)
                                   where ncbi_accession's are versioned.

Ids are kept exactly as published, ENA's 'GU786683.1:1..200:rRNA' composites
included; those are 86% of the rows.

The all-taxa derivation is sharded, as icite's citation graph is. Measured on
release 27 into a local warehouse (2026-09-18): a merge peaks at ~1 GB per
million mappings on first load (32.6M for E. coli: 37 GB) and ~1.75 GB with the
stored side loaded on a rerun, so 264M in one merge is ~260 GB the first time
and past the 502 GB host after that. identifier_mapping has no shard column, so
the shards are contiguous taxon-id ranges that tile the whole id line — a taxon
is wholly inside one scope, so ranges may move between releases, and a taxon
that vanishes upstream is still inside a scope and retires.

ponytail: gene_name is not derived. It labels one cross-reference, not the RNA
— a symbol for HGNC, a versioned gene id for Ensembl, a sequence range for Rfam
— and one RNA carries up to 10,100 distinct ones. The Ensembl rows' gene ids
would make a URS -> ENSEMBL (gene) mapping; derive that from raw when wanted.

ponytail: the file is sorted by URS, not taxon, so no scope's scan can prune:
each reads all of raw and keeps its own rows (4 s and a ~12 GB floor locally,
whatever the taxon). Fine for 13 shards or a few taxa; hundreds of single-taxon
runs against R2 would want raw partitioned or sorted by taxon.
"""

import re
import urllib.request

import duckdb
from pyiceberg.expressions import And, EqualTo, GreaterThanOrEqual, LessThan

from . import merge
from .ncbi import _derive, _land

BASE = "https://ftp.ebi.ac.uk/pub/databases/RNAcentral"
RAW = "raw.rnacentral__id_mapping"

# The file has no header, so the names are ours and the order is the readme's.
# auto_detect is off: a column RNAcentral adds fails loudly rather than shifting.
COLUMNS = ("{'urs':'VARCHAR','database':'VARCHAR','external_id':'VARCHAR',"
           "'taxon_id':'INTEGER','rna_type':'VARCHAR','gene_name':'VARCHAR'}")

# Raw rows per all-taxa merge. Measured on release 27: 13 ranges, the largest
# 37.5M rows because a taxon is never split (Salmonella enterica alone is 32.7M).
ROWS_PER_MERGE = 20_000_000


def resolve(number=None):
    """(RNAcentral release number, its numbered URL); the current release by default."""
    if not number:
        with urllib.request.urlopen(f"{BASE}/current_release/release_notes.txt", timeout=60) as r:
            banner = re.search(r"RNAcentral Release (\d+)", r.read(1000).decode("utf-8", "replace"))
        if not banner:
            raise SystemExit("rnacentral: no 'RNAcentral Release NN' banner in "
                             "current_release/release_notes.txt")
        number = banner.group(1)
    return str(number), f"{BASE}/releases/{number}.0/id_mapping/id_mapping.tsv.gz"


def land_raw(cat, release, number=None, url=None):
    """Phase 1: stream id_mapping verbatim and whole into raw. Returns (release number, rows).

    `url` is a local copy or a mirror; it cannot say which release it is, so
    `number` must.
    """
    if url and not number:
        raise SystemExit("rnacentral: a --url needs --rnacentral-release, the file does not "
                         "carry its own release number")
    if not url:
        number, url = resolve(number)
    # An empty cell is the only missing marker (gene_name, 86% of rows). Quoting
    # is off: the file quotes nothing, and a stray '"' in a gene name must not
    # swallow the lines after it.
    source = (f"(SELECT *, '{number}' AS rnacentral_release FROM read_csv('{url}', sep='\\t', "
              f"header=false, auto_detect=false, columns={COLUMNS}, nullstr='', quote='', escape=''))")
    n = _land(cat, release, RAW, source)
    merge.manifest(cat, release, "rnacentral", "id_mapping", url, n, version=number, method="release_number")
    return str(number), n


# The six Ensembl databases seen in release 27: ENSEMBL, ENSEMBL_GENCODE,
# ENSEMBL_FUNGI, ENSEMBL_METAZOA, ENSEMBL_PLANTS, ENSEMBL_PROTISTS.
NAMESPACE = ("CASE WHEN starts_with(database, 'ENSEMBL') THEN 'ENSEMBL_TRANSCRIPT' "
             "WHEN database = 'REFSEQ' THEN 'REFSEQ_RNA' ELSE upper(database) END")


def _scopes(cat, taxon):
    """The merge scopes of one derivation: a single taxon, or ranges covering every taxon."""
    if taxon:
        return {"": EqualTo("taxon_id", taxon)}  # ncbi._derive labels a single taxon itself
    con = duckdb.connect()
    con.register("raw", cat.load_table(RAW).scan(selected_fields=("taxon_id",)).to_arrow())
    starts = [r[0] for r in con.sql(f"""
        SELECT min(taxon_id) FROM (
            SELECT taxon_id, sum(n) OVER (ORDER BY taxon_id) // {ROWS_PER_MERGE} AS shard
            FROM (SELECT taxon_id, count(*) AS n FROM raw GROUP BY 1))
        GROUP BY shard ORDER BY 1
    """).fetchall()]
    # The outer bounds are the int32 line, not the smallest and largest taxon
    # seen: a stored taxon that upstream has since dropped must still fall in a
    # scope, or it would never retire.
    bounds = [0, *starts[1:], 2**31 - 1]
    return {f" [taxa {lo}-{hi - 1}]": And(GreaterThanOrEqual("taxon_id", lo), LessThan("taxon_id", hi))
            for lo, hi in zip(bounds, bounds[1:])}


def transform(cat, release, taxon=None):
    """Phase 2: URS_taxid <-> member-database cross-references, and the RNA's type.

    For one species when `taxon` is given, else for all of them in taxon-range
    shards. Both are contained in the same complete upstream state, so
    alternating them cannot retire each other's rows.
    """
    out = {}
    for label, scope in _scopes(cat, taxon).items():
        con = duckdb.connect()
        con.register("raw", cat.load_table(RAW).scan(
            row_filter=scope,
            selected_fields=("urs", "database", "external_id", "taxon_id", "rna_type")).to_arrow())

        # DISTINCT because folding ENSEMBL_GENCODE into ENSEMBL_TRANSCRIPT makes
        # duplicates; the file itself has none.
        mapping = con.sql(f"""
            SELECT DISTINCT 'RNACENTRAL' AS source_namespace,
                   urs || '_' || taxon_id AS source_id,
                   {NAMESPACE} AS target_namespace, external_id AS target_id,
                   taxon_id, 'RNACENTRAL' AS source, NULL::DOUBLE AS confidence
            FROM raw WHERE external_id IS NOT NULL
        """).to_arrow_table()
        # One type per (urs, taxon) in release 27. If a release ever gives two,
        # the merge refuses the duplicate key rather than picking one.
        rna = con.sql("""
            SELECT DISTINCT urs || '_' || taxon_id AS urs_taxid, taxon_id, rna_type FROM raw
        """).to_arrow_table()
        con.close()

        out[f"annotation.rnacentral__rna{label}"] = merge.merge(
            cat, "annotation.rnacentral__rna", rna, release, scope)
        # The scope names THIS writer: Ensembl, NCBI and HGNC write the same taxa.
        out[f"annotation.identifier_mapping{label}"] = merge.merge(
            cat, "annotation.identifier_mapping", mapping, release,
            And(scope, EqualTo("source", "RNACENTRAL")))
    return out


def ingest(cat, release, taxa=None, number=None, url=None):
    number, n = land_raw(cat, release, number, url)
    return {f"{RAW} [release {number}]": n, **_derive(transform, cat, release, taxa)}
