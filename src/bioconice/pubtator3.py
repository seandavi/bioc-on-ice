"""PubTator3 -> Iceberg: which concepts NCBI's text mining finds in which papers.

PubTator3 (Lu lab, NCBI; public domain per the directory README) publishes one
complete headerless TSV per entity type, regenerated together about monthly:
PMID, Type, Concept ID, Mentions, Resource. 446,876,903 rows across the five
files landed here in the 2026-08-17 dump (gene 74.5M, disease 168.3M, chemical
147.0M, species 49.0M, mutation 8.1M). Unversioned, like NCBI Gene: the
manifest records the retrieval date.

Five raw tables rather than one stacked table, although the five layouts are
identical: `_land` replaces its whole table, so one table per file is what lets
a file land, fail and be re-landed on its own (91 rate-limited commits in all;
the 34 of disease should not be hostage to the 2 of mutation). The file's own
Type column already says which kind a row is, so nothing is lost by not stacking.

Raw is verbatim, and that rules out `ncbi.tsv`: its nullstr='-' would erase
real data here. '-' is the concept id of a mention PubTator3 recognised but
could not normalise (10,749,370 chemical rows) and six gene mentions are
literally '-'. Quoting is off as well: mentions carry stray double quotes
('Toll"-like receptor'). Only the empty string reads as NULL: mentions are
empty on 60.2M rows — in the mutation and species files (the two checked),
exactly those whose resource list lacks 'PubTator3', i.e. asserted by a
curated resource with no text hit.

One derived table, annotation.pubtator3__mention, keyed by
(pmid, concept_type, concept_id, resource) and merged in 16 shards of pmid the
way icite__citation is. What is and is not in that key, decided from the dump:

  resource   IN the key, exploded from its '|'-list. Upstream prints the list
             in arbitrary order — 'gene2pubmed|gene_interactions' on 131,228
             rows, the reverse on 130,836 — so as an attribute it would open a
             version row whenever the order flipped. One row per asserting
             resource has no order, and makes "curated links only" a filter.
  concept id ';'-joined where a mention resolves to several ids (310,917 gene
             rows, '6900;1491938'; 11,297 species rows): split, so each joins
             to ncbi__gene / taxonomy. NOT split for Mutation, where ';' is
             part of one tmVar id ('tmVar:c|SUB|A|2063|G;HGVS:c.2063A>G;...').
             Rows whose concept id is '-' are not derived: no concept, no key.
  mentions   left in raw on purpose, as hgnc.py leaves date_modified there: the
             surface strings move with every tagger retrain and newly mined
             full text, and carrying them would open a version row each month
             for a paper-concept link that did not change. Join back to raw on
             (pmid, concept_id) to read them.

So the table has no attributes: a row is only ever asserted or withdrawn.
"""

import duckdb
from pyiceberg.expressions import EqualTo

from . import merge
from .ncbi import _land

BASE = "https://ftp.ncbi.nlm.nih.gov/pub/lu/PubTator3/"

# File stem -> the value its Type column must carry on every row.
KINDS = {"gene": "Gene", "disease": "Disease", "chemical": "Chemical",
         "species": "Species", "mutation": "Mutation"}

# In file order; there is no header. auto_detect is off, so a column upstream
# adds or drops fails the read instead of shifting values.
COLUMNS = ("{'pmid':'VARCHAR','type':'VARCHAR','concept_id':'VARCHAR',"
           "'mentions':'VARCHAR','resource':'VARCHAR'}")

SHARDS = 16


def _read(url):
    return (f"read_csv('{url}', sep='\\t', header=false, auto_detect=false, "
            f"quote='', escape='', columns={COLUMNS})")


def land_raw(cat, release, base=None):
    """Phase 1: the five dumps, verbatim and whole, each with its own manifest row."""
    counts = {}
    for kind in KINDS:
        url = f"{base or BASE}{kind}2pubtator3.gz"
        facts = merge.reading(release, "pubtator3", kind, url)
        n = _land(cat, release, f"raw.pubtator3__{kind}", _read(url))
        merge.manifest(cat, release, "pubtator3", kind, url, n, **facts)
        counts[f"raw.pubtator3__{kind}"] = n
    return counts


def transform(cat, release):
    """Phase 2: every (paper, concept, asserting resource), one merge per shard of pmid.

    All five kinds are exploded into DuckDB and checked before the first merge,
    so a bad dump writes nothing. The scope is the shard alone, which is why
    this always derives all five: a subset would retire the other kinds' rows.

    ponytail: all five kinds sit exploded in one in-memory DuckDB table before
    the first merge. Measured 2026-09-18 on a local warehouse, one real file
    beside four one-line stand-ins: species (48,958,091 raw -> 55,466,292
    derived) 46 s, 16.2 GB peak; mutation (8,135,311 -> 8,458,733) 13 s, 5.1 GB.
    All five (446.9M raw) is NOT yet measured; scaled by rows it is ~150 GB at
    worst, inside the 502 GB ingest host. If it stops fitting, build and merge
    one shard at a time, filtering each raw scan on pmid % SHARDS in DuckDB.
    """
    con = duckdb.connect()
    con.execute("CREATE TABLE m (pmid VARCHAR, concept_type VARCHAR, concept_id VARCHAR, "
                "resource VARCHAR, shard INTEGER)")
    for kind, expected in KINDS.items():
        con.register("raw", cat.load_table(f"raw.pubtator3__{kind}").scan(
            selected_fields=("pmid", "type", "concept_id", "resource")).to_arrow())
        bad = con.sql(f"SELECT count(*) FROM raw WHERE type IS DISTINCT FROM '{expected}' "
                      "OR TRY_CAST(pmid AS BIGINT) IS NULL").fetchone()[0]
        if bad:
            raise ValueError(f"pubtator3: {bad:,} rows of raw.pubtator3__{kind} have a Type "
                             f"other than '{expected}' or a non-numeric PMID")
        # DISTINCT because splitting can collide: '6900;1491938' beside a plain '6900'.
        con.execute(f"""
            INSERT INTO m
            SELECT DISTINCT pmid, type, concept_id, resource, (pmid::BIGINT % {SHARDS})::INTEGER
            FROM (SELECT pmid, type, resource,
                         unnest(CASE WHEN type = 'Mutation' THEN [concept_id]
                                     ELSE str_split(concept_id, ';') END) AS concept_id
                  FROM (SELECT pmid, type, concept_id,
                               unnest(str_split(resource, '|')) AS resource FROM raw
                        WHERE concept_id <> '-'))
            WHERE concept_id <> '' AND resource <> ''
        """)
        con.unregister("raw")
    out = {}
    for shard in range(SHARDS):
        inc = con.sql(f"SELECT * FROM m WHERE shard = {shard}").to_arrow_table()
        out[f"annotation.pubtator3__mention [shard {shard}]"] = merge.merge(
            cat, "annotation.pubtator3__mention", inc, release, EqualTo("shard", shard))
    return out


def ingest(cat, release, base=None):
    return {**land_raw(cat, release, base), **transform(cat, release)}
