"""Cellosaurus -> Iceberg, in the same two phases as the other sources.

Cellosaurus is the cell-line registry: one entry per line (168,970 in release
56.0 of 25-June-2026), each with a stable CVCL_ accession, and the glue between
CELLxGENE, ENCODE biosamples, BEDbase metadata, GEO and DepMap, which all name
cell lines differently. CC BY 4.0 (licence quoted on issue #134); it is not an
OBO Foundry ontology, so obo.py does not reach it.

**`cellosaurus.txt` is the raw, not the .obo or the .xml.** Upstream publishes
three serialisations of one release. The flat file is the one the release notes
and the format documentation describe, and the only one that needs no
interpretation to land: the .obo folds the entry into OBO's vocabulary
(category and sex both become `subset`; species, cross-references and patents
all become `xref`; the CC lines are run together into one `comment`), which is
already a reading of the record, and the .xml is the same release at 642 MB
against 117 MB. Each line is `CC   value` — a two-letter line code, three spaces,
the value — entries end with `//`, and the file opens with a header (banner,
version, licence, the line-code table).

**Raw is one row per line of the file, header included**, in file order:
`line_number`, the entry's `accession`, the line `code`, the `value`. That is
verbatim and whole — `code || '   ' || value` ordered by line_number is the
file again, which the test asserts — without being a pre-interpreted wide
table: comments (CC), STR profiles (ST), references (RX) and web links (WW),
which nothing derives yet, are all there for whoever needs them. A body line
that is neither `XX   value` nor `//` fails the landing, since it could not be
given back verbatim.

**Versioned by the file's own header** (` Version: 56.0`), recorded as
`release_number`. The URL is a rolling one that upstream replaces per release,
so raw is replaced per version and versions accumulate, like raw.obo__*.

Three derived tables, all Type 2, Cellosaurus their only writer:

  annotation.cellosaurus__cell_line  one row per accession
  annotation.cellosaurus__xref       one row per DR line (accession, database, identifier)
  annotation.cellosaurus__disease    one row per DI line (accession, database, disease id)

**Cross-references get their own table rather than annotation.identifier_mapping.**
That table is keyed on a single `taxon_id` and every writer's scope includes
one: it maps *gene* identifiers within a species. A cell line is not a gene, a
hybrid line has two species, and 153,632 of the 473,381 DR lines point at
Wikidata — none of which is a gene-id authority a mapping consumer expects to
find under `target_namespace`. Diseases are a separate table again because a DI
line is an assertion about the line ("derived from a patient with"), not
another name for it, and it carries a label where a DR line has none.

**A cell line can have more than one species** (1,439 entries in release 56.0,
hybridomas and hybrid lines all but two), so `taxon_ids` is a sorted list, not a scalar.

ponytail: `cellosaurus_refs.txt` (what the RX ids resolve to),
`cellosaurus_xrefs.txt` (each DR database's name, category and URL template)
and `cellosaurus_deleted_ACs.txt` are not landed. Nothing derived needs them
yet; secondary (merged) accessions are in the main file and are derived. Land
each as its own raw table when something does.

ponytail: OI (originate from same individual) stays in raw. It is symmetric, so
one new sibling would open a new version of every line in the family; derive it
as its own (accession, accession) table if something needs it.
"""

import duckdb
from pyiceberg.expressions import AlwaysTrue, EqualTo

from . import merge

URL = "https://ftp.expasy.org/databases/cellosaurus/cellosaurus.txt"


def land_raw(cat, release, url=None):
    """Phase 1: the flat file, one row per line, replaced per Cellosaurus version.

    Returns (version, rows). `url` is a local copy, for tests and pinned reruns.
    """
    url = url or URL
    con = duckdb.connect()
    # The file is read as one string and split on newlines, rather than through
    # read_csv: the split keeps blank header lines and gives every line its
    # ordinal deterministically, which a parallel CSV scan does not promise.
    # ponytail: the whole file is held in memory (117 MB -> 3.6 GB peak RSS);
    # stream it if a release ever grows past what one string can hold (4 GB).
    arrow = con.sql(f"""
        WITH f AS (SELECT string_split(content, chr(10)) AS lines FROM read_text('{url}')),
        l AS (SELECT unnest(range(1, len(lines) + 1)) AS line_number, unnest(lines) AS line,
                     len(lines) AS n FROM f),
        -- The header runs up to the first entry, which an ID line opens. The
        -- empty string after the file's final newline is not a line.
        b AS (SELECT line_number, line,
                     line_number >= min(line_number) FILTER (starts_with(line, 'ID   ')) OVER () AS body
              FROM l WHERE NOT (line_number = n AND line = '')),
        -- Entries are '//'-terminated, so a line's entry is the number of
        -- terminators before it. The accession is the entry's AC line; the ID
        -- line comes first, so it cannot simply be carried downwards.
        e AS (SELECT *, CASE WHEN body THEN left(line, 2) END AS code,
                     CASE WHEN NOT body THEN line WHEN line <> '//' THEN substr(line, 6) END AS value,
                     count(*) FILTER (body AND line = '//') OVER (
                         ORDER BY line_number ROWS BETWEEN UNBOUNDED PRECEDING AND 1 PRECEDING) AS entry
              FROM b)
        SELECT line_number,
               CASE WHEN body THEN max(value) FILTER (code = 'AC') OVER (PARTITION BY entry) END AS accession,
               code, value,
               max(regexp_extract(line, '^ Version: (\\S+)', 1)) FILTER (NOT body) OVER () AS cellosaurus_version,
               '{release}' AS landed_in,
               body AND NOT regexp_matches(line, '^([A-Z]{{2}}   .*|//)$') AS malformed
        FROM e ORDER BY line_number
    """).to_arrow_table()
    if not arrow.num_rows:
        raise SystemExit(f"cellosaurus: {url} yielded no lines")
    version = arrow.column("cellosaurus_version")[0].as_py()
    if not version:
        raise SystemExit(f"cellosaurus: {url} header carries no ' Version:' line")
    if bad := arrow.filter(arrow.column("malformed")).column("line_number").to_pylist():
        raise SystemExit(f"cellosaurus: {url} has {len(bad)} body lines that are neither "
                         f"'XX   value' nor '//', first at line {bad[0]}")

    n = merge.write(cat, "raw.cellosaurus__release", arrow.drop_columns(["malformed"]),
                    EqualTo("cellosaurus_version", version))
    merge.manifest(cat, release, "cellosaurus", url, n, version=version, method="release_number")
    return version, n


def transform(cat, release, version):
    """Phase 2: the cell line, its cross-references and its diseases.

    Scoped to `version`'s rows: raw accumulates every landed version, so an
    unscoped read would derive from all of them at once.
    """
    con = duckdb.connect()
    con.register("raw", cat.load_table("raw.cellosaurus__release").scan(
        row_filter=EqualTo("cellosaurus_version", version)).to_arrow())

    # Multi-valued cells are sorted, so upstream reordering a list is not a new
    # version. SY and AS are one '; '-separated line each; OX and HI repeat.
    # 'Sex unspecified' and 'Age unspecified' are upstream's words and are kept:
    # they are not the same statement as a missing SX or AG line (NULL).
    joined = "array_to_string(list_sort({}), '|')"
    cell_line = con.sql(f"""
        SELECT accession,
               max(value) FILTER (code = 'ID') AS name,
               {joined.format("str_split(max(value) FILTER (code = 'SY'), '; ')")} AS synonyms,
               {joined.format("str_split(max(value) FILTER (code = 'AS'), '; ')")} AS secondary_accessions,
               -- the CASE keeps the cast off other codes' values; FILTER alone does not
               list_sort(list(CASE WHEN code = 'OX' THEN regexp_extract(value, 'NCBI_TaxID=(\\d+)', 1)::INT END)
                         FILTER (code = 'OX')) AS taxon_ids,
               max(value) FILTER (code = 'SX') AS sex,
               max(value) FILTER (code = 'AG') AS age,
               max(value) FILTER (code = 'CA') AS category,
               {joined.format("list(split_part(value, ' ! ', 1)) FILTER (code = 'HI')")} AS parent_accessions
        FROM raw WHERE accession IS NOT NULL GROUP BY accession
    """).to_arrow_table()

    # Identifiers are kept as published ('EFO_0001185', 'Q847482'): the databases
    # do not share a CURIE convention to normalise them to. The identifier is
    # everything after the first '; ', in case one ever contains the separator.
    xref = con.sql("""
        SELECT DISTINCT accession, split_part(value, '; ', 1) AS database,
               substr(value, strpos(value, '; ') + 2) AS identifier
        FROM raw WHERE code = 'DR'
    """).to_arrow_table()

    disease = con.sql("""
        SELECT accession, d.database, d.disease_id, d.disease_name
        FROM (SELECT accession, regexp_extract(value, '^([^;]+); ([^;]+); (.*)$',
                                               ['database', 'disease_id', 'disease_name']) AS d
              FROM raw WHERE code = 'DI')
    """).to_arrow_table()

    # Cellosaurus is the only writer of all three, so each scope is the whole table.
    return {
        name: merge.merge(cat, name, arrow, release, AlwaysTrue())
        for name, arrow in (("annotation.cellosaurus__cell_line", cell_line),
                            ("annotation.cellosaurus__xref", xref),
                            ("annotation.cellosaurus__disease", disease))
    }


def ingest(cat, release, url=None):
    version, n = land_raw(cat, release, url)
    return {f"raw.cellosaurus__release [{version}]": n, **transform(cat, release, version)}
