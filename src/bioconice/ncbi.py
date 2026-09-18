"""NCBI Gene -> Iceberg, in the same two phases as Ensembl.

NCBI is the *unversioned* case, and it is here partly to prove the model
handles it. There is no release number: the dumps are regenerated nightly, so
`Last-Modified` and any published checksum change daily on identical content.
The version recorded in `provenance.release` is therefore the retrieval date,
with `version_method = 'retrieval_date'` saying plainly how we know it — never
a fabricated release number.

Raw is landed **whole** — every organism NCBI knows. An earlier version
filtered `tax_id` on the way in to avoid downloading rows nothing read; that
made `raw` a function of what we happen to derive, so a third species would
have cost a re-fetch. The files do not fit in memory as one Arrow table
(gene_info is 71.5M rows), so landing streams them in record batches.

Derivation is scoped in `transform`: one taxon, or — the default — every taxon
the dump carries, in a single merge per table. Per-taxon runs remain for a
cheap refresh of one species; both scopes are contained in the same complete
upstream state, so alternating them cannot retire each other's rows.

It is also the **second writer** to `annotation.identifier_mapping`, which
Ensembl already writes. That is why its merge scope names the source: with a
taxon-only scope each ingest would retire the other's rows on alternating runs,
silently and forever. Data Vault calls that the flip-flop effect. The same trap
exists one level in: gene2ensembl and gene_info both produce cross-references,
so they are merged in a single call rather than one each.

`_land` and `merge.manifest` are the shared machinery for every NCBI dump: the
sibling gene2* modules differ from this one only in URL, raw table, column
spec and derivation, so they import from here rather than restating it.
"""

import time

import duckdb
import pyarrow as pa
from pyiceberg.exceptions import RESTError
from pyiceberg.expressions import AlwaysTrue, And, EqualTo

from . import merge, schemas

DATA = "https://ftp.ncbi.nlm.nih.gov/gene/DATA/"

# One DuckDB column spec per dump, in file order. `auto_detect` is off against
# these, so a column NCBI inserts or reorders fails loudly here instead of
# quietly shifting every value one to the left. Upstream's '#tax_id' is named
# taxon_id directly: with an explicit spec the names come from here, not the
# header. The gene2* dumps in the sibling modules keep their own specs, because
# each lands on its own schedule under its own manifest row.
COLUMNS = {
    "gene2ensembl": (
        "{'taxon_id':'INTEGER','gene_id':'VARCHAR','ensembl_gene_id':'VARCHAR',"
        "'rna_accession':'VARCHAR','ensembl_rna_id':'VARCHAR',"
        "'protein_accession':'VARCHAR','ensembl_protein_id':'VARCHAR'}"
    ),
    "gene_info": (
        "{'taxon_id':'INTEGER','gene_id':'VARCHAR','symbol':'VARCHAR','locus_tag':'VARCHAR',"
        "'synonyms':'VARCHAR','dbxrefs':'VARCHAR','chromosome':'VARCHAR',"
        "'map_location':'VARCHAR','description':'VARCHAR','type_of_gene':'VARCHAR',"
        "'symbol_authority':'VARCHAR','full_name_authority':'VARCHAR',"
        "'nomenclature_status':'VARCHAR','other_designations':'VARCHAR',"
        "'modification_date':'VARCHAR','feature_type':'VARCHAR'}"
    ),
    "gene_history": (
        "{'taxon_id':'INTEGER','gene_id':'VARCHAR','discontinued_gene_id':'VARCHAR',"
        "'discontinued_symbol':'VARCHAR','discontinue_date':'VARCHAR'}"
    ),
}

BATCH = 1_000_000
# Rows accumulated per Iceberg commit. R2 Data Catalog rate-limits commits
# per table ("429: too many commits to this table or view"), so committing
# every read batch — 82 commits for gene2pubmed — exhausts the budget on
# large dumps. Memory stays bounded: 5M rows of the widest dump peaked well
# under the 2.7 GB the 1M-batch pipeline measured end to end.
ROWS_PER_COMMIT = 5_000_000


def _commit(table, arrow, first):
    """One overwrite-or-append, riding out the catalog's commit rate limit.

    pyiceberg's own retry gives up within seconds; the 429 window is longer
    than that, so wait it out here rather than dying mid-landing.
    """
    for attempt in range(10):
        try:
            (table.overwrite if first else table.append)(arrow)
            return
        except RESTError as err:
            if not schemas.is_rate_limit(err):
                raise
            time.sleep(65)
    raise RuntimeError(f"commit still rate-limited after {attempt + 1} waits")


def tsv(url, columns):
    """The DuckDB read for an NCBI dump: tab-separated, '-' for null, declared columns."""
    return (f"read_csv('{url}', sep='\\t', header=true, auto_detect=false, "
            f"columns={columns}, nullstr='-')")


def _land(cat, release, identifier, source, config=None):
    """Stream one source verbatim into its raw table, replacing what was there.

    `source` is any DuckDB table expression — `tsv(url, columns)` for the NCBI
    dumps, a `read_csv(...)` with other options for iCite — so a landing differs
    only in what it reads and where it lands. Replace-with-the-first-chunk then
    append, rather than one atomic overwrite, because these do not fit in
    memory whole. Reads in BATCH-row record batches but commits only every
    ROWS_PER_COMMIT rows — reading is memory-bound, committing is
    rate-limited, and the two limits want different granularities.

    `config` is passed straight to `duckdb.connect()` — DuckDB startup settings
    (e.g. `threads`, `http_retries`) for a source whose read needs them, such as
    bedbase.py's many-small-page HTTP crawl. Unused by every other caller.

    ponytail: a crash between commits leaves the table partly landed. Re-running
    the ingest repairs it and raw carries no validity interval to corrupt, so the
    exposure is a wrong row count until then. Upgrade path if that is not good
    enough: land into a staging table and swap.
    """
    table = schemas.create(cat, identifier)
    arrow_schema = table.schema().as_arrow()
    con = duckdb.connect(config=config or {})
    reader = con.sql(f"SELECT *, '{release}' AS landed_in FROM {source}").to_arrow_reader(BATCH)

    n = 0
    pending = []
    for batch in reader:
        # Casting to the declared schema is the check: a null in an identifier
        # field fails here rather than landing quietly.
        pending.append(pa.Table.from_batches([batch]).cast(arrow_schema))
        if sum(t.num_rows for t in pending) >= ROWS_PER_COMMIT:
            _commit(table, pa.concat_tables(pending), first=not n)
            n += sum(t.num_rows for t in pending)
            pending = []
    if pending:
        _commit(table, pa.concat_tables(pending), first=not n)
        n += sum(t.num_rows for t in pending)
    if not n:
        # Otherwise a bad URL silently leaves the previous landing in place and
        # reports success.
        raise SystemExit(f"{identifier}: {source} yielded no rows")
    return n


def land_raw(cat, release, urls=None):
    """Phase 1: all three NCBI Gene dumps, verbatim and unfiltered."""
    urls = urls or {}
    counts = {f"raw.ncbi__{name}": _land(cat, release, f"raw.ncbi__{name}",
                                         tsv(urls.get(name, f"{DATA}{name}.gz"), COLUMNS[name]))
              for name in COLUMNS}
    # One source, three files: `url` is the directory they came from and
    # `row_count` their total, because the manifest is keyed (release, source).
    # Per-file provenance needs a third key column — issue #8.
    merge.manifest(cat, release, "ncbi_gene", DATA, sum(counts.values()))
    return counts


def _where(taxon):
    """Scope of one derivation: a single taxon, or every taxon the dump carries."""
    return EqualTo("taxon_id", taxon) if taxon else AlwaysTrue()


def _derive(transform, cat, release, taxa):
    """Run `transform` once per requested taxon, or once for all of them.

    ponytail: the all-taxa merge holds the whole scope in memory. Measured
    against R2 on 2026-09-12, on the 502 GB ingest host: gene_info +
    gene2ensembl (72M genes, 122M mappings) 131 GB peak; gene2go (124M) 110 GB;
    gene2pubmed (78M) 35 GB; gene2accession (330M mappings) 320 GB in 14 min.
    Headroom is thin on the last one: if it stops fitting, scope by
    In("taxon_id", chunk) over the dump's distinct taxa instead of AlwaysTrue.
    """
    out = {}
    for taxon in taxa or [None]:
        for k, v in transform(cat, release, taxon).items():
            out[f"{k} [{taxon}]" if taxon else k] = v
    return out


def transform(cat, release, taxon=None):
    """Phase 2: NCBI's view of a gene, and its cross-references.

    For one species when `taxon` is given, else for all of them. Both dumps
    arrive sorted by tax_id upstream, so a single-taxon filter prunes nearly
    every Parquet row group on min/max stats without the table being partitioned.
    """
    con = duckdb.connect()
    for name in ("gene2ensembl", "gene_info"):
        con.register(name, cat.load_table(f"raw.ncbi__{name}").scan(
            row_filter=_where(taxon)).to_arrow())

    # NEWENTRY is NCBI's placeholder for GeneRIF submissions against a gene that
    # is not in Gene: one row per taxon, and not a gene. Everything else stays,
    # including the biological-region records, which outnumber the genes.
    con.execute("""
        CREATE OR REPLACE TABLE info AS
        SELECT * FROM gene_info WHERE symbol IS DISTINCT FROM 'NEWENTRY'
    """)

    gene = con.sql("""
        SELECT gene_id, taxon_id, symbol, description,
               type_of_gene AS gene_type, chromosome, map_location
        FROM info
    """).to_arrow_table()

    mapping = con.sql("""
        SELECT DISTINCT source_namespace, source_id, target_namespace, target_id,
               taxon_id, 'NCBI' AS source, NULL::DOUBLE AS confidence
        FROM (
            SELECT 'ENSEMBL' AS source_namespace, ensembl_gene_id AS source_id,
                   'ENTREZ' AS target_namespace, gene_id AS target_id, taxon_id
            FROM gene2ensembl
            WHERE ensembl_gene_id IS NOT NULL AND gene_id IS NOT NULL
          UNION ALL
            SELECT 'ENTREZ', gene_id, 'SYMBOL', symbol, taxon_id FROM info WHERE symbol IS NOT NULL
          UNION ALL
            SELECT 'ENTREZ', gene_id, 'ALIAS', trim(alias), taxon_id
            FROM (SELECT gene_id, taxon_id, unnest(str_split(synonyms, '|')) AS alias
                  FROM info WHERE synonyms IS NOT NULL)
            WHERE trim(alias) <> ''
          UNION ALL
            -- dbXrefs are 'Authority:id' pairs. Split at the FIRST colon only:
            -- HGNC's and AllianceGenome's own ids embed one, 'HGNC:HGNC:11998',
            -- and that prefixed form is the canonical HGNC identifier.
            -- MIM is renamed OMIM: it is NCBI's abbreviation for that authority,
            -- and one authority under two namespace names would be the real bug.
            SELECT 'ENTREZ', gene_id,
                   CASE WHEN ns = 'MIM' THEN 'OMIM' ELSE ns END, id, taxon_id
            FROM (SELECT gene_id, taxon_id, upper(split_part(x, ':', 1)) AS ns,
                         substr(x, strpos(x, ':') + 1) AS id
                  FROM (SELECT gene_id, taxon_id, unnest(str_split(dbxrefs, '|')) AS x
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
        "annotation.ncbi__gene": merge.merge(
            cat, "annotation.ncbi__gene", gene, release, _where(taxon)),
        "annotation.identifier_mapping": merge.merge(
            cat, "annotation.identifier_mapping", mapping, release,
            And(_where(taxon), EqualTo("source", "NCBI"))),
    }


def ingest(cat, release, taxa=None):
    return {**land_raw(cat, release), **_derive(transform, cat, release, taxa)}
