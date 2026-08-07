"""NCBI Gene -> Iceberg, in the same two phases as Ensembl.

NCBI is the *unversioned* case, and it is here partly to prove the model
handles it. There is no release number: the dumps are regenerated nightly, so
`Last-Modified` and any published checksum change daily on identical content.
The version recorded in `provenance.release` is therefore the retrieval date,
with `version_method = 'retrieval_date'` saying plainly how we know it — never
a fabricated release number.

Raw is landed **whole** — every organism NCBI knows, not the two species we
currently derive annotation for. An earlier version filtered `tax_id` on the way
in to avoid downloading rows nothing read; that made `raw` a function of what we
happen to derive, so a third species would have cost a re-fetch. The files do
not fit in memory as one Arrow table (gene_info is 71.5M rows), so landing
streams them in record batches. Per-species scoping lives in `transform`.

It is also the **second writer** to `annotation.identifier_mapping`, which
Ensembl already writes. That is why its merge scope names the source: with a
taxon-only scope each ingest would retire the other's rows on alternating runs,
silently and forever. Data Vault calls that the flip-flop effect. The same trap
exists one level in: gene2ensembl and gene_info both produce cross-references,
so they are merged in a single call rather than one each.
"""

from datetime import datetime, timezone

import duckdb
import pyarrow as pa
from pyiceberg.expressions import And, EqualTo

from . import merge, schemas
from .ensembl import _write

DATA = "https://ftp.ncbi.nlm.nih.gov/gene/DATA/"

# One DuckDB column spec per dump, in file order. `auto_detect` is off against
# these, so a column NCBI inserts or reorders fails loudly here instead of
# quietly shifting every value one to the left.
COLUMNS = {
    "gene2ensembl": (
        "{'tax_id':'INTEGER','gene_id':'VARCHAR','ensembl_gene_id':'VARCHAR',"
        "'rna_accession':'VARCHAR','ensembl_rna_id':'VARCHAR',"
        "'protein_accession':'VARCHAR','ensembl_protein_id':'VARCHAR'}"
    ),
    "gene_info": (
        "{'tax_id':'INTEGER','gene_id':'VARCHAR','symbol':'VARCHAR','locus_tag':'VARCHAR',"
        "'synonyms':'VARCHAR','dbxrefs':'VARCHAR','chromosome':'VARCHAR',"
        "'map_location':'VARCHAR','description':'VARCHAR','type_of_gene':'VARCHAR',"
        "'symbol_authority':'VARCHAR','full_name_authority':'VARCHAR',"
        "'nomenclature_status':'VARCHAR','other_designations':'VARCHAR',"
        "'modification_date':'VARCHAR','feature_type':'VARCHAR'}"
    ),
    "gene_history": (
        "{'tax_id':'INTEGER','gene_id':'VARCHAR','discontinued_gene_id':'VARCHAR',"
        "'discontinued_symbol':'VARCHAR','discontinue_date':'VARCHAR'}"
    ),
}

BATCH = 1_000_000


def _land(cat, release, name, url=None):
    """Stream one dump verbatim into its raw table, replacing what was there.

    Replace-with-the-first-batch then append, rather than one atomic overwrite,
    because these do not fit in memory whole.

    ponytail: a crash between batches leaves the table partly landed. Re-running
    the ingest repairs it and raw carries no validity interval to corrupt, so the
    exposure is a wrong row count until then. Upgrade path if that is not good
    enough: land into a staging table and swap.
    """
    identifier = f"raw.ncbi_{name}"
    table = schemas.create(cat, identifier)
    arrow_schema = table.schema().as_arrow()
    con = duckdb.connect()
    reader = con.sql(f"""
        SELECT * RENAME (tax_id AS taxon_id), '{release}' AS landed_in
        FROM read_csv('{url or f"{DATA}{name}.gz"}', sep='\t', header=true,
                      auto_detect=false, columns={COLUMNS[name]}, nullstr='-')
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
        raise SystemExit(f"{identifier}: {url or name} yielded no rows")
    return n


def land_raw(cat, release, urls=None):
    """Phase 1: all three NCBI Gene dumps, verbatim and unfiltered."""
    urls = urls or {}
    counts = {f"raw.ncbi_{name}": _land(cat, release, name, urls.get(name))
              for name in COLUMNS}
    _manifest(cat, release, sum(counts.values()))
    return counts


def _manifest(cat, release, rows):
    """Record what this release was built from — ADR-0007."""
    now = datetime.now(timezone.utc)
    con = duckdb.connect()
    arrow = con.sql(f"""
        SELECT '{release}' AS release, 'ncbi_gene' AS source,
               '{now.date()}' AS source_version,
               'retrieval_date' AS version_method,
               '{now.isoformat(timespec="seconds")}' AS retrieved_at,
               '{DATA}' AS url, NULL::VARCHAR AS checksum, {rows}::BIGINT AS row_count
    """).to_arrow_table()
    # One source, three files: `url` is the directory they came from and
    # `row_count` their total, because the manifest is keyed (release, source).
    # Per-file provenance needs a third key column — issue #8.
    _write(cat, "provenance.release", arrow,
           And(EqualTo("release", release), EqualTo("source", "ncbi_gene")))


def transform(cat, release, taxon):
    """Phase 2: NCBI's view of a gene, and its cross-references, for one species.

    Both dumps arrive sorted by tax_id upstream, so this filter prunes nearly
    every Parquet row group on min/max stats without the table being partitioned.
    """
    con = duckdb.connect()
    for name in ("gene2ensembl", "gene_info"):
        con.register(name, cat.load_table(f"raw.ncbi_{name}").scan(
            row_filter=EqualTo("taxon_id", taxon)).to_arrow())

    # NEWENTRY is NCBI's placeholder for GeneRIF submissions against a gene that
    # is not in Gene: one row per taxon, and not a gene. Everything else stays,
    # including the biological-region records, which outnumber the genes.
    con.execute("""
        CREATE OR REPLACE TABLE info AS
        SELECT * FROM gene_info WHERE symbol IS DISTINCT FROM 'NEWENTRY'
    """)

    gene = con.sql(f"""
        SELECT gene_id, {taxon}::INTEGER AS taxon_id, symbol, description,
               type_of_gene AS gene_type, chromosome, map_location
        FROM info
    """).to_arrow_table()

    mapping = con.sql(f"""
        SELECT DISTINCT source_namespace, source_id, target_namespace, target_id,
               {taxon}::INTEGER AS taxon_id, 'NCBI' AS source, NULL::DOUBLE AS confidence
        FROM (
            SELECT 'ENSEMBL' AS source_namespace, ensembl_gene_id AS source_id,
                   'ENTREZ' AS target_namespace, gene_id AS target_id
            FROM gene2ensembl
            WHERE ensembl_gene_id IS NOT NULL AND gene_id IS NOT NULL
          UNION ALL
            SELECT 'ENTREZ', gene_id, 'SYMBOL', symbol FROM info WHERE symbol IS NOT NULL
          UNION ALL
            SELECT 'ENTREZ', gene_id, 'ALIAS', trim(alias)
            FROM (SELECT gene_id, unnest(str_split(synonyms, '|')) AS alias
                  FROM info WHERE synonyms IS NOT NULL)
            WHERE trim(alias) <> ''
          UNION ALL
            -- dbXrefs are 'Authority:id' pairs. Split at the FIRST colon only:
            -- HGNC's and AllianceGenome's own ids embed one, 'HGNC:HGNC:11998',
            -- and that prefixed form is the canonical HGNC identifier.
            -- MIM is renamed OMIM: it is NCBI's abbreviation for that authority,
            -- and one authority under two namespace names would be the real bug.
            SELECT 'ENTREZ', gene_id,
                   CASE WHEN ns = 'MIM' THEN 'OMIM' ELSE ns END, id
            FROM (SELECT gene_id, upper(split_part(x, ':', 1)) AS ns,
                         substr(x, strpos(x, ':') + 1) AS id
                  FROM (SELECT gene_id, unnest(str_split(dbxrefs, '|')) AS x
                        FROM info WHERE dbxrefs IS NOT NULL)
                  -- no colon means no authority; without this the whole string
                  -- would become both the namespace and the identifier
                  WHERE x LIKE '%:%')
            WHERE ns <> '' AND id <> ''
        )
    """).to_arrow_table()

    # gene_info and gene2ensembl both write cross-references, so they merge in
    # one call: two merges into this same scope would retire each other's rows.
    return {
        "annotation.ncbi_gene": merge.merge(
            cat, "annotation.ncbi_gene", gene, release, EqualTo("taxon_id", taxon)),
        "annotation.identifier_mapping": merge.merge(
            cat, "annotation.identifier_mapping", mapping, release,
            And(EqualTo("taxon_id", taxon), EqualTo("source", "NCBI"))),
    }


def ingest(cat, release, taxa):
    out = dict(land_raw(cat, release))
    for taxon in taxa:
        for k, v in transform(cat, release, taxon).items():
            out[f"{k} [{taxon}]"] = v
    return out
