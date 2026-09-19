"""WikiPathways gene sets -> Iceberg, in the same two phases as the other sources.

WikiPathways publishes its pathways as gene sets once a month: one GMT file per
species under `current/gmt/`, the release date in every file name
(`wikipathways-20260910-gmt-Homo_sapiens.gmt`). CC0. On 2026-09-18 that is 18
species files, 1,957 pathways, 336 KB for human — small enough to read whole.

**GMT is ragged, so it is not read as CSV.** A line is a set name, a
description, then as many gene ids as the set has, tab-separated, with no
header and no quoting. Each file is read as text and split on LF, the only
dialect there is (checked 2026-09-18: ASCII, no CR, no quote character
anywhere). Raw keeps one row per line with the gene ids as the verbatim
tab-joined remainder of the line: a string rather than a list column, because
splitting is interpretation — duplicates (2,338 repeated ids within a line
on 2026-09-18) and order are what the file says, and the precedent is the GTF
attribute blob. The set name packs four fields
(`name%WikiPathways_<date>%<WP id>%<species>`); it lands unparsed and transform
takes it apart, checking the date and species against the file name's.

Every species file in the directory is landed; none is selected. The version is
the date the file names share, recorded as `release_number`, and raw is
replaced per version, so a re-landing is idempotent and versions accumulate.
`current/` keeps the last twelve months as `/<YYYYMMDD>/gmt/`; landing one of
those through `url` takes its date the same way.

Gene ids are Entrez GeneIDs — the GMT export's only id system, and every one of
the ids on 2026-09-18 is all digits. Transform fails on one that is not, rather
than label something else as Entrez.

ponytail: pathway ontology tags, authors, last-modified dates and the non-gene
nodes (metabolites, interactions) are not in the GMT; they live in the GPML
export. Land that as its own raw table when something needs more than gene sets.
"""

import re
import urllib.request
from pathlib import Path

import duckdb
from pyiceberg.expressions import AlwaysTrue, EqualTo

from . import merge

URL = "https://data.wikipathways.org/current/gmt/"
FILE = re.compile(r"wikipathways-(\d{8})-gmt-([A-Za-z_]+)\.gmt")

# ponytail: species name -> NCBI taxon id is a hand-kept dict, because the GMT
# names species and carries no taxon id. All 18 species published on 2026-09-18,
# each checked against NCBI Taxonomy that day. A species not here fails the
# transform loudly; add it then. Replace with a join to a landed NCBI Taxonomy
# names table once one exists. These are SPECIES-level ids, as WikiPathways
# names them: NCBI Gene files yeast under strain S288C (559292), so join genes
# on gene_id, which is unique across taxa, not on (gene_id, taxon_id).
TAXA = {
    "Anopheles gambiae": 7165, "Arabidopsis thaliana": 3702, "Bos taurus": 9913,
    "Caenorhabditis elegans": 6239, "Canis familiaris": 9615, "Danio rerio": 7955,
    "Drosophila melanogaster": 7227, "Equus caballus": 9796, "Gallus gallus": 9031,
    "Homo sapiens": 9606, "Mus musculus": 10090, "Pan troglodytes": 9598,
    "Populus trichocarpa": 3694, "Rattus norvegicus": 10116,
    "Saccharomyces cerevisiae": 4932, "Solanum lycopersicum": 4081, "Sus scrofa": 9823,
    "Zea mays": 4577,
}


def _files(url):
    """Every species GMT under `url`: an http directory listing, or a local directory."""
    if url.startswith("http"):
        with urllib.request.urlopen(url, timeout=60) as r:
            names = {m.group(0) for m in FILE.finditer(r.read().decode("utf-8", "replace"))}
    else:
        names = {p.name for p in Path(url).glob("*.gmt")}
    return [url.rstrip("/") + "/" + n for n in sorted(names)]


def land_raw(cat, release, url=None):
    """Phase 1: every species file, one row per GMT line, verbatim and whole.

    Returns (version, rows). `url` is a dated archive directory, or a local one.
    """
    url = url or URL
    files = _files(url)
    versions = {m.group(1) for f in files if (m := FILE.search(f))}
    if len(versions) != 1 or not all(FILE.search(f) for f in files):
        raise SystemExit(f"wikipathways: {url} should hold the GMT files of exactly one "
                         f"release; found {len(files)} files, dates {sorted(versions)}")
    version = versions.pop()

    con = duckdb.connect()
    # No CSV reader: the file is text, a line is a record, and tab is the only
    # separator. The final LF leaves one empty string after the split; that and
    # nothing else is dropped. A set with no genes keeps genes NULL.
    arrow = con.sql(f"""
        WITH f AS (SELECT parse_filename(filename) AS file_name, str_split(content, '\n') AS lines
                   FROM read_text({files!r})),
             l AS (SELECT file_name, unnest(lines) AS line,
                          generate_subscripts(lines, 1) AS line_number FROM f)
        SELECT file_name, line_number::INTEGER AS line_number,
               split_part(line, '\t', 1) AS name,
               split_part(line, '\t', 2) AS description,
               nullif(array_to_string(str_split(line, '\t')[3:], '\t'), '') AS genes,
               regexp_extract(file_name, '-gmt-(.*)\\.gmt$', 1) AS species,
               '{version}' AS wikipathways_version, '{release}' AS landed_in
        FROM l WHERE line <> ''
        ORDER BY file_name, line_number
    """).to_arrow_table()
    if not arrow.num_rows:
        raise SystemExit(f"wikipathways: {url} yielded no rows")

    n = merge.write(cat, "raw.wikipathways__gmt", arrow,
                    EqualTo("wikipathways_version", version))
    merge.manifest(cat, release, "wikipathways", "gmt", url, n, version=version,
                   method="release_number")
    return version, n


def transform(cat, release, version):
    """Phase 2: one row per pathway, and one per (pathway, gene).

    Scoped to `version`'s rows: raw accumulates every landed version. The three
    `error()`s fail the moment a row is built, before anything is written: a set
    name that is not the four packed fields, or that disagrees with its file's
    date or species; a species with no taxon id; a gene id that is not an
    Entrez GeneID.
    """
    con = duckdb.connect()
    con.register("raw", cat.load_table("raw.wikipathways__gmt").scan(
        row_filter=EqualTo("wikipathways_version", version)).to_arrow())

    taxon = " ".join(f"WHEN '{name}' THEN {tid}" for name, tid in TAXA.items())
    # The release date is left in raw on purpose: it moves on every line every
    # month, so carrying it would open a new version row for every pathway.
    con.execute(f"""
        CREATE TABLE p AS
        WITH x AS (
            SELECT *, regexp_extract(name, '^(.*)%WikiPathways_([0-9]{{8}})%(WP[0-9]+)%([^%]+)$',
                                     ['name', 'date', 'pathway_id', 'species']) AS f
            FROM raw
        )
        SELECT CASE WHEN f.date = wikipathways_version AND f.species = replace(species, '_', ' ')
                    THEN f.pathway_id
                    ELSE error('wikipathways: ' || file_name || ' line ' || line_number
                               || ' has an unparseable or mismatched set name: ' || x.name)
               END AS pathway_id,
               CASE f.species {taxon}
                    ELSE error('wikipathways: no taxon id declared for species ' || f.species)
               END AS taxon_id,
               f.name AS name, f.species AS species, description AS url, genes
        FROM x
    """)
    pathway = con.sql("SELECT pathway_id, taxon_id, name, species, url FROM p").to_arrow_table()

    # DISTINCT: a GMT line repeats a gene once per node that draws it, and
    # membership is a set.
    gene = con.sql("""
        SELECT DISTINCT pathway_id, taxon_id,
               CASE WHEN regexp_full_match(g, '[0-9]+') THEN g
                    ELSE error('wikipathways: ' || pathway_id || ' lists a gene id that is '
                               || 'not an Entrez GeneID: ' || g) END AS gene_id
        FROM (SELECT pathway_id, taxon_id, unnest(str_split(genes, '\t')) AS g FROM p)
    """).to_arrow_table()

    # WikiPathways is these tables' only writer and every species lands
    # together, so the scope is the whole table: a species file that vanishes
    # from a release retires its pathways, which is what happened upstream.
    return {
        "annotation.wikipathways__pathway": merge.merge(
            cat, "annotation.wikipathways__pathway", pathway, release, AlwaysTrue()),
        "annotation.wikipathways__gene_pathway": merge.merge(
            cat, "annotation.wikipathways__gene_pathway", gene, release, AlwaysTrue()),
    }


def ingest(cat, release, url=None):
    version, n = land_raw(cat, release, url)
    return {"raw.wikipathways__gmt": n, **transform(cat, release, version)}
