"""NCBI gene_orthologs and gene_group -> Iceberg: orthology across taxa.

Same FTP directory and the same unversioned nightly regeneration as ncbi.py,
so the version is the retrieval date. Two files in one five-column format, both
landed **whole** and verbatim; each gets its own manifest row (source `ncbi_gene`,
artifact the file name), so url and row_count are per file. No column of either can
hold a real '-' (taxon ids, GeneIDs, a relationship label; checked 2026-09-18,
no bare '-' cell in either file), so `ncbi.tsv`'s nullstr is harmless here.

Only gene_orthologs is derived. gene_group no longer carries orthologs — NCBI's
README: "Ortholog records appear in the gene_orthologs file, and are excluded
from the gene_group file" — so its eight other relationships (readthrough
parent/child/sibling, region member/parent, related functional gene/pseudogene)
stay in raw until something asks for a gene-relationship table.

**A pair spans two taxa, so which one owns the row?** The file is not
symmetric: NCBI names a primary gene per ortholog set (21 reference taxa on
2026-09-18, human for the vertebrates) and lists the other N-1 members against
it, once. `annotation.ortholog` stores each pair in BOTH directions and the row
belongs to `taxon_id`, the taxon of its first gene. That makes the per-taxon
scope mean what it says: a `--taxa 10090` run reads every raw row naming mouse
on either side, recomputes exactly the rows with taxon_id = 10090, and merges
under (taxon_id = 10090, source = 'NCBI') — the complete state of that scope,
so nothing it did not recompute can be retired. The mirrored rows (9606 ->
10090) belong to human's scope and wait for a run that covers human. Stored
one-directional, a mouse run would own almost nothing (mouse is rarely the
primary) and "orthologs of this gene" would need an OR over two columns.
Which side NCBI made the primary is left in raw.

Pairs between two non-primary members of a set (mouse <-> rat, both listed
against human) are not in the file and are not inferred here: that closure is
interpretation, and a join through the shared primary gives it to whoever
wants it.

The table is stacked by `source` like the gene tables, writer 'NCBI', so Ensembl
Compara can be a second writer in its own id space without either retiring the
other's rows (ADR-0004).
"""

import duckdb
from pyiceberg.expressions import And, EqualTo, Or

from . import merge
from .ncbi import DATA, _derive, _land, _where, tsv

# One spec for both files: the README states they share a column format. Same
# contract as ncbi.COLUMNS: auto_detect off, names from the spec, file order.
COLUMNS = ("{'taxon_id':'INTEGER','gene_id':'VARCHAR','relationship':'VARCHAR',"
           "'other_taxon_id':'INTEGER','other_gene_id':'VARCHAR'}")
FILES = ("gene_orthologs", "gene_group")


def land_raw(cat, release, urls=None):
    """Phase 1: both files verbatim and whole, each under its own manifest row."""
    urls = urls or {}
    counts = {}
    for name in FILES:
        url = urls.get(name, f"{DATA}{name}.gz")
        facts = merge.reading(release, "ncbi_gene", name, url)
        n = _land(cat, release, f"raw.ncbi__{name}", tsv(url, COLUMNS))
        merge.manifest(cat, release, "ncbi_gene", name, f"{DATA}{name}.gz", n, **facts)
        counts[f"raw.ncbi__{name}"] = n
    return counts


def transform(cat, release, taxon=None):
    """Phase 2: ortholog pairs in both directions, for one species or (default) all.

    A single-taxon run scans for the taxon on either side. Only the first
    column is sorted upstream, so that scan reads the whole table; it is five
    narrow columns and 19M rows. Measured 2026-09-18 on a local warehouse:
    18.9M raw rows -> 37.8M pairs over 1,753 taxa in one all-taxa merge, 11 s
    for land + derive, 19 GB peak.
    """
    raw_scope = Or(_where(taxon), EqualTo("other_taxon_id", taxon)) if taxon else _where(taxon)
    con = duckdb.connect()
    con.register("raw", cat.load_table("raw.ncbi__gene_orthologs").scan(
        row_filter=raw_scope).to_arrow())

    # The relationship filter is belt and braces: the README promises the
    # column is always 'Ortholog' in this file. DISTINCT because a pair given
    # in both directions upstream would otherwise mirror into a duplicate key.
    pairs = con.sql(f"""
        SELECT DISTINCT gene_id, taxon_id, ortholog_gene_id, ortholog_taxon_id, 'NCBI' AS source
        FROM (
            SELECT gene_id, taxon_id, other_gene_id AS ortholog_gene_id,
                   other_taxon_id AS ortholog_taxon_id
            FROM raw WHERE relationship = 'Ortholog'
          UNION ALL
            SELECT other_gene_id, other_taxon_id, gene_id, taxon_id
            FROM raw WHERE relationship = 'Ortholog'
        )
        {f"WHERE taxon_id = {int(taxon)}" if taxon else ""}
    """).to_arrow_table()

    # The scope names this writer, so a second one (Ensembl Compara) can stack
    # into the same table under its own source.
    return {"annotation.ortholog": merge.merge(
        cat, "annotation.ortholog", pairs, release,
        And(_where(taxon), EqualTo("source", "NCBI")))}


def ingest(cat, release, taxa=None):
    return {**land_raw(cat, release), **_derive(transform, cat, release, taxa)}
