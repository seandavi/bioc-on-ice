# BugSigDB in biocOnIce

[BugSigDB](https://bugsigdb.org) is a curated database of published microbial
signatures, meaning the taxa reported as more or less abundant between two groups in a
microbiome study. biocOnIce publishes it as two tables that anyone can query
anonymously from DuckDB, R or Python without downloading anything first.

| Table | Rows | One row per |
| --- | --- | --- |
| `raw.bugsigdb__full_dump` | 7,425 | signature: study, experiment, condition, body site, groups, methods, direction |
| `annotation.signature_taxon` | 59,486 | taxon member of a signature (7,365 signatures; the other 60 list no members) |

The two tables join on `bsdb_id`, e.g. `'bsdb:83/1/1'` (study/experiment/signature,
with the `bsdb:` prefix included). Row counts are from the `v2026-10-31` release,
built from BugSigDBExports `v1.3.1`. Licence: CC BY 4.0, so cite BugSigDB as its
site asks.

## Connect

In DuckDB (CLI, R or Python), with no account, token or extension to install:

```sql
ATTACH 'https://data.bioconice.cancerdatasci.org/latest/bioconice.duckdb' AS bioc (READ_ONLY);
```

`latest/` moves with each release. For reproducible work, pin a dated release
instead, e.g. `https://data.bioconice.cancerdatasci.org/v2026-10-31/bioconice.duckdb`.
The last 4 weekly releases are kept (30 days until
[#216](https://github.com/seandavi/bioc-on-ice/issues/216) lands); for
long-term pins use the monthly Hugging Face archive once it starts
([#214](https://github.com/seandavi/bioc-on-ice/issues/214)). Both are listed
at <https://bioconice.cancerdatasci.org/releases>.

Each table is also a plain Parquet file that any Parquet reader can open:

```
https://data.bioconice.cancerdatasci.org/latest/annotation/signature_taxon.parquet
https://data.bioconice.cancerdatasci.org/latest/raw/bugsigdb__full_dump.parquet
```

## What's in the tables

**`annotation.signature_taxon`** has one row per signature member: `bsdb_id`,
`member_index` (1-based position in the signature), `taxon_rank` (MetaPhlAn letter:
`k p c o f g s t`), `taxon_name`, `ncbitaxon_id` (integer NCBI Taxonomy id),
`taxon_lineage` and `taxon_lineage_ids` (the full `|`-separated lineage). Members
keep the rank the curators reported, so one signature can mix genera and species.
Nothing is rolled up to a fixed rank.

**`raw.bugsigdb__full_dump`** is the BugSigDB export as published, one row per
signature, with every column kept as text. The columns you'll use most:

- `condition` / `efo_id`, `body_site` / `uberon_id`, `host_species`
- `abundance_in_group_1`: `'increased'` or `'decreased'` in group 1 relative to group 0
- `group_0_name`, `group_1_name`, the sample sizes, `sequencing_type`, `statistical_test`
- `pmid`, `doi`, `title`, `year`

Because the raw table is unparsed, numbers such as `year` and the sample sizes are
strings. `efo_id` can hold several comma-separated ids when a study has more than one
condition (`'EFO:0005842,EFO:0000400'`). Condition names are capitalised
(`'Colorectal cancer'`), so match on `efo_id` rather than on the name.

## Examples

These were run against `v2026-10-31`.

Taxa most often reported in colorectal cancer signatures, by direction:

```sql
SELECT t.taxon_name, t.taxon_rank,
       count(*) FILTER (WHERE s.abundance_in_group_1 = 'increased') AS increased,
       count(*) FILTER (WHERE s.abundance_in_group_1 = 'decreased') AS decreased
FROM bioc.annotation.signature_taxon t
JOIN bioc.raw.bugsigdb__full_dump s USING (bsdb_id)
WHERE list_contains(string_split(s.efo_id, ','), 'EFO:0005842')   -- colorectal cancer
GROUP BY ALL
ORDER BY increased + decreased DESC
LIMIT 10;
```

| taxon_name | taxon_rank | increased | decreased |
| --- | --- | --- | --- |
| Fusobacterium | g | 26 | 6 |
| Lachnospiraceae | f | 11 | 20 |
| Peptostreptococcus | g | 29 | 1 |
| Parvimonas | g | 26 | 1 |
| … | | | |

Every signature that includes a given taxon, here *Fusobacterium nucleatum*
(NCBI Taxonomy id 851):

```sql
SELECT s.bsdb_id, s.condition, s.body_site, s.abundance_in_group_1, s.pmid
FROM bioc.annotation.signature_taxon t
JOIN bioc.raw.bugsigdb__full_dump s USING (bsdb_id)
WHERE t.ncbitaxon_id = 851;
```

One signature as a list of member names:

```sql
SELECT bsdb_id, list(taxon_name ORDER BY member_index) AS members
FROM bioc.annotation.signature_taxon
WHERE bsdb_id = 'bsdb:83/1/1'
GROUP BY bsdb_id;
```

## From R

```r
con <- DBI::dbConnect(duckdb::duckdb())
DBI::dbExecute(con, "ATTACH 'https://data.bioconice.cancerdatasci.org/latest/bioconice.duckdb' AS bioc (READ_ONLY)")

# All signatures as a named list of NCBI taxon ids, ready for set-based enrichment
x <- DBI::dbGetQuery(con, "SELECT bsdb_id, ncbitaxon_id FROM bioc.annotation.signature_taxon")
sigs <- split(x$ncbitaxon_id, x$bsdb_id)
length(sigs)  # 7365
```

The Bioconductor package [bugsigdbr](https://bioconductor.org/packages/bugsigdbr)
remains the full-featured R interface to BugSigDB. These tables are for SQL access
and for joining BugSigDB to the rest of biocOnIce.

## From Python

```python
import duckdb

con = duckdb.connect()
con.execute("ATTACH 'https://data.bioconice.cancerdatasci.org/latest/bioconice.duckdb' AS bioc (READ_ONLY)")
sigs = con.sql("""
    SELECT bsdb_id, list(ncbitaxon_id ORDER BY member_index) AS taxa
    FROM bioc.annotation.signature_taxon
    GROUP BY bsdb_id
""").pl()   # polars; .df() for pandas, .fetchall() for neither
```

## Good to know

- **No rank rollup.** Members are at the curated rank, so to compare at one rank
  (e.g. genus), walk `taxon_lineage` or `taxon_lineage_ids`.
- **`member_index` can change between BugSigDB versions** if curators reorder a
  signature. To match members across versions, use `ncbitaxon_id` within a
  `bsdb_id` (it isn't unique: two members can resolve to the same taxon).
- **Names follow MetaPhlAn's spelling**, which may lag NCBI's current name. Join on
  `ncbitaxon_id` instead of the name.
- Only signatures whose curation state is *Complete* are in the export.
