"""Release bundle (issue #189, map #187): every published table as flat Parquet, plus
`bioconice.duckdb`, `views.sql`, `restore.sql` and `manifest.json`, in one local directory.

The mechanics (COPY to Parquet, views.sql, restore.sql, the DuckDB file, manifest.json)
are cdsci.publish, cdsci-lake ADR-0027. What is bioc's lives here: `TABLES` is
(schema, table, SELECT) against whatever DuckDB has attached as `bioc` (the live Iceberg
catalog) or `lake` (cdsci-lake, ADR-0012), plus the table and column docs. A table
moves to cdsci-lake by changing its SELECT. Nothing is uploaded here.
"""

from pathlib import Path

import cdsci.publish
from cdsci.lake.contracts import ColumnContract, TableContract, TemporalModel

from . import (bedbase, biogrid, bugsigdb, cellosaurus, cellxgene, complexportal, encode, ensembl,
               eqtlcatalogue, gwas_catalog, hgnc, icite, intact, mane, ncbi, ncbi_accession, ncbi_go,
               ncbi_orthologs, ncbi_pubmed, obo, pubtator3, rnacentral, wikipathways)
from .release import SIGNATURE_TAXON, SIGNATURE_TAXON_SELECT
from .schemas import TABLES as DECLARED

DEFAULT_BASE_URL = "https://data.bioconice.cancerdatasci.org/latest"
DB_FILE = "bioconice.duckdb"
# README "Query it": anonymous, read-only.
ATTACH_LIVE = (
    "INSTALL iceberg; LOAD iceberg; ATTACH 'bioconice' AS bioc (TYPE ICEBERG, "
    "ENDPOINT 'https://icegate-bioconice.seandavi.workers.dev', AUTHORIZATION_TYPE 'none')"
)


# Iceberg row history, not published: a release is a versioned dataset, not versioned rows
# (ADR-0013, #222).
ROW_HISTORY = ("valid_from", "valid_to", "landed_in")


def _current(name, d):
    """Current rows only (ADR-0013): `valid_to IS NULL` where the table carries it, without
    the row-history columns."""
    names = {f.name for f in d.schema.fields}
    history = [c for c in ROW_HISTORY if c in names]
    exclude = f" EXCLUDE ({', '.join(history)})" if history else ""
    return (f"SELECT *{exclude} FROM bioc.{name}"
            + (" WHERE valid_to IS NULL" if "valid_to" in names else ""))


# Declared tables already read from cdsci-lake (#183): each source module's RELEASE.
FROM_LAKE = {**hgnc.RELEASE, **complexportal.RELEASE, **wikipathways.RELEASE,
             **gwas_catalog.RELEASE, **mane.RELEASE, **bugsigdb.RELEASE,
             **cellosaurus.RELEASE, **ncbi_go.RELEASE, **ncbi_pubmed.RELEASE,
             **ncbi.RELEASE, **ncbi_accession.RELEASE, **ncbi_orthologs.RELEASE,
             **eqtlcatalogue.RELEASE, **cellxgene.RELEASE, **encode.RELEASE,
             **bedbase.RELEASE, **biogrid.RELEASE, **intact.RELEASE, **obo.RELEASE,
             **ensembl.RELEASE, **icite.RELEASE, **pubtator3.RELEASE, **rnacentral.RELEASE,
             # Cross-source (ADR-0015 b): each writer's slice, from its own lake tables.
             "resource.resource_relationship": " UNION ALL ".join(
                 f"SELECT * FROM ({m.RELATIONSHIP})" for m in (eqtlcatalogue, cellxgene, encode,
                                                             bedbase)),
             "annotation.interaction": " UNION ALL ".join(
                 f"SELECT * FROM ({m.INTERACTION})" for m in (biogrid, intact)),
             # BioGRID and IntAct write none. UniProt has no module here: its rows are the
             # lake model transform/models/uniprot/identifier_mapping.sql, which v2026-11-03
             # holds for taxon 9606 only.
             "annotation.identifier_mapping": " UNION ALL ".join(
                 [f"SELECT * FROM ({m.MAPPING})" for m in (ncbi, ncbi_accession, ensembl, hgnc,
                                                         mane, rnacentral)]
                 + ["SELECT * FROM lake.uniprot.identifier_mapping WHERE taxon_id = 9606"])}

# Every table of the live catalog except annotation.identifier_mapping__pre_repair.
TABLES = [(*name.split("."), FROM_LAKE.get(name) or _current(name, d))
          for name, d in DECLARED.items()] + [
    # Not declared in schemas.py: signature_taxon is the bioconice-bugsigdb release
    # contract's table (release.py), read from the lake; raw.metadata has no writer here.
    ("annotation", "signature_taxon", SIGNATURE_TAXON_SELECT),
    # iCite metadata straight from cdsci-lake (#183 wave A): the Iceberg copy was its old
    # reverse-ETL export, same 17 columns and types. The columns the lake added for
    # raw.icite__metadata (#183 H1) stay out, so this table's columns do not change.
    ("raw", "metadata", icite.METADATA_17),
    # From cdsci-lake (#194); becomes lake.pubmed.article with seandavi/cdsci-lake#136.
    ("annotation", "pubmed__article", "SELECT * FROM lake.omicidx.pubmed_article"),
]
_EMPTY = "Empty string, not null, when the PubMed record has no value."


def _text(name, doc):
    return ColumnContract(name, "string", doc, True, null_meaning=_EMPTY)


PUBMED_ARTICLE = TableContract(
    name="annotation.pubmed__article",
    description=(
        "PubMed citations, one row per PMID: title, abstract, journal, authors, MeSH terms, "
        "references and grants, from the cdsci-lake table lake.omicidx.pubmed_article. "
        "Join on pmid with ncbi__gene_pubmed, icite__publication and pubtator3__mention."
    ),
    grain="one row per pmid",
    primary_key=("pmid",),
    temporal_model=TemporalModel.UPSERT_LATEST_SNAPSHOT,
    owner="bioc-on-ice",
    license="nlm-terms",
    columns=(
        ColumnContract("pmid", "string", "PubMed id, e.g. '35000000'; the key.", False,
                       identifier_namespace="pubmed"),
        _text("title", "Article title (English, or translated to English)."),
        _text("issue", "Volume and issue as 'volume(issue)', e.g. '54(4)'."),
        _text("pages", "Pagination as printed, e.g. '501-511'."),
        _text("abstract", "Abstract text."),
        _text("journal", "Full journal title."),
        ColumnContract(
            "authors",
            "list<struct<lastname:string, forename:string, initials:string, "
            "identifier:string, affiliation:string>>",
            "Authors in byline order; identifier is an ORCID URL where given.", True),
        _text("pubdate", "Publication date as precise as the record gives: 'YYYY', 'YYYY-MM' "
                         "or 'YYYY-MM-DD'."),
        _text("mesh_terms", "MeSH headings, '; '-separated, each 'Dnnnnnn:Heading' followed by "
                            "' / Qnnnnnn:Qualifier' pairs; '*' marks a major topic."),
        _text("publication_types", "Publication types, '; '-separated 'Dnnnnnn:Type'."),
        _text("chemical_list", "Substances, '; '-separated 'Dnnnnnn:Name'."),
        _text("keywords", "Author keywords, '; '-separated."),
        _text("doi", "DOI, without the 'https://doi.org/' prefix."),
        ColumnContract("references", "list<struct<citation:string, pmid:string>>",
                       "Cited works as listed; pmid where PubMed resolved one.", True),
        _text("languages", "Article language code(s), e.g. 'eng'."),
        _text("vernacular_title", "Title in the original language."),
        _text("date_completed", "Date MEDLINE indexing was completed, 'YYYY-MM-DD'."),
        _text("date_revised", "Date the record was last revised, 'YYYY-MM-DD'."),
        _text("pmc", "PubMed Central id."),
        _text("other_id", "Other ids the record carries."),
        _text("medline_ta", "NLM title abbreviation of the journal, e.g. 'Amino Acids'."),
        _text("nlm_unique_id", "NLM catalog id of the journal."),
        _text("issn_linking", "Linking ISSN of the journal."),
        _text("country", "Country of the journal's publisher."),
        ColumnContract("grant_ids",
                       "list<struct<grant_id:string, grant_acronym:string, country:string, "
                       "agency:string>>",
                       "Funding: grant number, acronym, agency and its country.", True),
    ),
)


def connect():
    """DuckDB with both sources of `TABLES`: cdsci-lake as `lake` (read-only) and the live
    Iceberg catalog as `bioc`. The shared lake needs CU_OPENALEX_LAKE_BACKEND=postgres."""
    from cdsci.lake.connect import lake_connect

    con = lake_connect(read_only=True)
    con.execute(ATTACH_LIVE)
    return con


def _docs(name):
    """(table comment, {column: doc}) from the declarations; nothing for an undeclared table."""
    if name in icite.PUBLISHED:
        return icite.published_docs(name, DECLARED[name])
    if name in DECLARED:
        d = DECLARED[name]
        return d.comment, {f.name: f.doc for f in d.schema.fields if f.doc}
    if name == SIGNATURE_TAXON.name:
        return SIGNATURE_TAXON.description, {c.name: c.description for c in SIGNATURE_TAXON.columns}
    if name == PUBMED_ARTICLE.name:
        return PUBMED_ARTICLE.description, {c.name: c.description for c in PUBMED_ARTICLE.columns}
    return None, {}


def publish(con, out: Path, base_url: str = DEFAULT_BASE_URL, only=None) -> dict:
    """Build one release bundle in `out` from `con` (`connect()`): the shared mechanics are
    cdsci.publish (cdsci-lake ADR-0027); this module supplies the table list and docs."""
    return cdsci.publish.publish(
        con, out, tables=TABLES, docs=_docs, base_url=base_url, db_file=DB_FILE,
        code_dir=Path(__file__).parent,
        provenance_sql="SELECT * FROM bioc.provenance.release ORDER BY ALL", only=only)
