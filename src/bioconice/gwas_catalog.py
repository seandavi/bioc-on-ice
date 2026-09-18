"""NHGRI-EBI GWAS Catalog -> Iceberg, in the same two phases as the other sources.

Two files from one dated release directory: the ontology-annotated full
associations download (a zip holding one TSV; 1,192,604 rows at 2026-09-15) and
the v1.0.3.1 studies download (230,057 rows). Of the three studies files a
release carries, that one is the superset, and `gwas-catalog-studies.tsv` has no
STUDY ACCESSION at all, so it could not be joined. Both fit in memory as one
Arrow table, so this needs none of the streaming machinery the NCBI dumps do.

**Versioned by release directory.** Releases live under
`releases/YYYY/MM/DD/`; `releases/latest/` is a copy of the newest and says
nowhere which one it is, so the default is resolved by walking the index to the
newest dated directory and landing from there. The date is the Catalog's own
release label and is recorded as `release_number`. Raw is replaced per release,
so a re-landing is idempotent and releases accumulate, like raw.hgnc__*.

The column contract is each file's header, checked whole, as in hgnc.py: the
file names carry a format version (v1.0.3.1) because the Catalog does change it.

**An association has no identifier in the download**, and no tidy one can be
made: (study, SNP) repeats 9,881 times at 2026-09-15 — one study reports a
variant per sex, per model, per conditional analysis — and multi-SNP rows
(1,956 ';' haplotypes, 3,288 ' x ' interactions, 127 ',' lists) rule out an
rsID key anyway. What does identify a row is what the curator extracted from
the paper: study, SNPS, risk allele, p-value and its annotation, effect, CI,
risk allele frequency and reported genes. Those nine are unique once the
file's 132 whole-row duplicates collapse, several are NULL on real rows, and
Iceberg identifier fields cannot be, so `association_key` is their md5. The
Catalog's own mapping pipeline output (position, mapped genes, consequence,
mapped traits) is re-derived upstream; those are the attributes, and a remap is
a new version of the same association. Measured between the 2026-09-04 and
2026-09-15 releases: 573 associations new, 1 retired, 25,819 re-versioned — every
one of them in the gene or trait mapping columns.

Multi-valued cells become lists only where that is lossless, and nothing is
exploded, because no list is part of a merge key: mapped trait URIs are
normalised by obo's own `_curie` into a sorted list (join: unnest, then
ontology.term on ontology='efo' — all 1,302,028 mentions at 2026-09-15 resolve
against EFO v3.94.0); SNPS and SNP_GENE_IDS likewise. MAPPED_GENE and the
positions stay verbatim strings, since their ' - ', ', ', '; ' and ' x ' carry
structure a flat list would lose.

ponytail: the ancestry, unpublished-studies and efo-trait-mappings files are not
landed; each is its own raw table when something needs it.
"""

import os
import re
import tempfile
import urllib.request
import zipfile
from datetime import datetime, timezone

import duckdb
from pyiceberg.expressions import AlwaysTrue, EqualTo

from . import merge
from .hgnc import _header
from .obo import _curie

RELEASES = "https://ftp.ebi.ac.uk/pub/databases/gwas/releases/"

# Upstream header -> our column name, in file order. The two files spell the
# PubMed column differently ('PUBMEDID', 'PUBMED ID'); raw spells it one way.
ASSOCIATIONS = {
    "DATE ADDED TO CATALOG": "date_added_to_catalog",
    "PUBMEDID": "pubmed_id",
    "FIRST AUTHOR": "first_author",
    "DATE": "date",
    "JOURNAL": "journal",
    "LINK": "link",
    "STUDY": "study",
    "DISEASE/TRAIT": "disease_trait",
    "INITIAL SAMPLE SIZE": "initial_sample_size",
    "REPLICATION SAMPLE SIZE": "replication_sample_size",
    "REGION": "region",
    "CHR_ID": "chr_id",
    "CHR_POS": "chr_pos",
    "REPORTED GENE(S)": "reported_genes",
    "MAPPED_GENE": "mapped_gene",
    "UPSTREAM_GENE_ID": "upstream_gene_id",
    "DOWNSTREAM_GENE_ID": "downstream_gene_id",
    "SNP_GENE_IDS": "snp_gene_ids",
    "UPSTREAM_GENE_DISTANCE": "upstream_gene_distance",
    "DOWNSTREAM_GENE_DISTANCE": "downstream_gene_distance",
    "STRONGEST SNP-RISK ALLELE": "strongest_snp_risk_allele",
    "SNPS": "snps",
    "MERGED": "merged",
    "SNP_ID_CURRENT": "snp_id_current",
    "CONTEXT": "context",
    "INTERGENIC": "intergenic",
    "RISK ALLELE FREQUENCY": "risk_allele_frequency",
    "P-VALUE": "p_value",
    "PVALUE_MLOG": "pvalue_mlog",
    "P-VALUE (TEXT)": "p_value_text",
    "OR or BETA": "or_beta",
    "95% CI (TEXT)": "ci_95_text",
    "PLATFORM [SNPS PASSING QC]": "platform",
    "CNV": "cnv",
    "MAPPED_TRAIT": "mapped_trait",
    "MAPPED_TRAIT_URI": "mapped_trait_uri",
    "STUDY ACCESSION": "study_accession",
    "GENOTYPING TECHNOLOGY": "genotyping_technology",
}
STUDIES = {
    "DATE ADDED TO CATALOG": "date_added_to_catalog",
    "PUBMED ID": "pubmed_id",
    "FIRST AUTHOR": "first_author",
    "DATE": "date",
    "JOURNAL": "journal",
    "LINK": "link",
    "STUDY": "study",
    "DISEASE/TRAIT": "disease_trait",
    "INITIAL SAMPLE SIZE": "initial_sample_size",
    "REPLICATION SAMPLE SIZE": "replication_sample_size",
    "PLATFORM [SNPS PASSING QC]": "platform",
    "ASSOCIATION COUNT": "association_count",
    "MAPPED_TRAIT": "mapped_trait",
    "MAPPED_TRAIT_URI": "mapped_trait_uri",
    "STUDY ACCESSION": "study_accession",
    "GENOTYPING TECHNOLOGY": "genotyping_technology",
    "SUBMISSION DATE": "submission_date",
    "STATISTICAL MODEL": "statistical_model",
    "BACKGROUND TRAIT": "background_trait",
    "MAPPED BACKGROUND TRAIT": "mapped_background_trait",
    "MAPPED BACKGROUND TRAIT URI": "mapped_background_trait_uri",
    "COHORT": "cohort",
    "FULL SUMMARY STATISTICS": "full_summary_statistics",
    "SUMMARY STATS LOCATION": "summary_stats_location",
    "GXE": "gxe",
}
# raw table -> (file within a release directory, its columns)
FILES = {
    "raw.gwas_catalog__associations": (
        "gwas-catalog-associations_ontology-annotated-full.zip", ASSOCIATIONS),
    "raw.gwas_catalog__studies": ("gwas-catalog-download-studies-v1.0.3.1.txt", STUDIES),
}

# What the curator extracted from the paper — the association's identity.
IDENTITY = ("study_accession", "snps", "strongest_snp_risk_allele", "p_value", "p_value_text",
            "or_beta", "ci_95_text", "risk_allele_frequency", "reported_genes")


def latest(base=RELEASES):
    """The newest dated release directory under `base`: YYYY/, then MM/, then DD/.

    Names are zero-padded, so the string max is the newest. `base` is the
    Catalog's index over HTTP, or a local directory laid out the same way.
    """
    for _ in range(3):
        if base.startswith("http"):
            with urllib.request.urlopen(base) as r:
                names = re.findall(r'href="(\d+)/"', r.read().decode())
        else:
            names = [n for n in os.listdir(base) if n.isdigit()]
        if not names:
            raise SystemExit(f"gwas_catalog: no dated directories under {base}")
        base = f"{base.rstrip('/')}/{max(names)}/"
    return base


def _fetch(url, scratch):
    """A local path to `url`'s TSV: downloaded if remote, unzipped if a zip.

    DuckDB does not read zip archives, so the associations go through disk.
    """
    path = url
    if url.startswith("http"):
        path = os.path.join(scratch, os.path.basename(url))
        urllib.request.urlretrieve(url, path)
    if not path.endswith(".zip"):
        return path
    with zipfile.ZipFile(path) as z:
        if len(names := z.namelist()) != 1:
            raise SystemExit(f"gwas_catalog: {url} holds {names}, expected one TSV")
        return z.extract(names[0], scratch)


def land_raw(cat, release, url=None):
    """Phase 1: both files, verbatim and whole, replaced per Catalog release.

    Returns (version, {raw table: rows}). `url` is a release directory — a dated
    one under RELEASES, or a local copy — holding both files under their
    upstream names.
    """
    url = (url or latest()).rstrip("/") + "/"
    # A dated directory names its own version; any other location has none.
    dated = re.search(r"(\d{4})/(\d{2})/(\d{2})/$", url)
    version = "-".join(dated.groups()) if dated else str(datetime.now(timezone.utc).date())

    con = duckdb.connect()
    counts = {}
    with tempfile.TemporaryDirectory(dir=os.environ.get("BIOCONICE_SCRATCH")) as scratch:
        for identifier, (name, columns) in FILES.items():
            tsv = _fetch(url + name, scratch)
            if (header := _header(tsv)) != tuple(columns):
                raise SystemExit(f"gwas_catalog: {url}{name} header is not the declared one; "
                                 f"differs in {sorted(set(header) ^ set(columns))}")
            select = ", ".join(f'"{src}" AS {dst}' for src, dst in columns.items())
            # The dialect is stated rather than sniffed, and it has NO quoting: a '"'
            # is an ordinary character here (3,182 study titles quote a phrase, and
            # one risk allele carries a stray unbalanced one), so any quote setting
            # would swallow tabs and newlines up to the next '"'. No cell holds a tab
            # or a line break: rows read equals lines minus the header. An empty cell
            # is the only missing marker and reads as NULL; 'NA' and 'NR' are the
            # curators' "not applicable" / "not reported" and are data, so they stay.
            arrow = con.sql(f"""
                SELECT {select}, '{version}' AS gwas_catalog_release, '{release}' AS landed_in
                FROM read_csv('{tsv}', header=true, all_varchar=true, delim='\\t', quote='',
                              escape='', comment='', nullstr='')
            """).to_arrow_table()
            if not arrow.num_rows:
                raise SystemExit(f"gwas_catalog: {url}{name} yielded no rows")
            counts[identifier] = merge.write(cat, identifier, arrow,
                                             EqualTo("gwas_catalog_release", version))

    # One source, two files: `url` is the directory they came from and
    # `row_count` their total, as for ncbi_gene (per-file provenance is issue #8).
    merge.manifest(cat, release, "gwas_catalog", url, sum(counts.values()), version=version,
                   method="release_number" if dated else "retrieval_date")
    return version, counts


def _ids(col):
    """A ', '-separated URI cell as a sorted list of CURIEs, in ontology.term's own form."""
    return (f"list_sort(list_distinct(list_transform(str_split({col}, ','), "
            f"u -> {_curie('trim(u)')})))")


def transform(cat, release, version):
    """Phase 2: studies keyed by accession, associations keyed by what was curated.

    Scoped to `version`'s rows: raw accumulates every landed release, so an
    unscoped read would derive from all of them at once.
    """
    con = duckdb.connect()
    for name in ("associations", "studies"):
        con.register(name, cat.load_table(f"raw.gwas_catalog__{name}").scan(
            row_filter=EqualTo("gwas_catalog_release", version)).to_arrow())

    # submission_date, statistical_model and background_trait are left in raw:
    # the file declares them and fills none (0 of 230,057 rows at 2026-09-15).
    study = con.sql(f"""
        SELECT study_accession, pubmed_id, first_author, date AS publication_date, journal,
               link, study AS study_title, date_added_to_catalog, disease_trait,
               initial_sample_size, replication_sample_size, platform, genotyping_technology,
               cohort, association_count::INTEGER AS association_count,
               mapped_trait, {_ids('mapped_trait_uri')} AS mapped_trait_ids,
               mapped_background_trait,
               {_ids('mapped_background_trait_uri')} AS mapped_background_trait_ids,
               full_summary_statistics = 'yes' AS full_summary_statistics,
               summary_stats_location, gxe = 'yes' AS gxe
        FROM studies
    """).to_arrow_table()

    # Every study-level column the associations file repeats per row (publication,
    # trait as reported, sample sizes, platform) is a function of study_accession
    # in it — checked at 2026-09-15 — and lives in the study table instead, so a
    # corrected journal name does not re-version a study's every association.
    # DISTINCT collapses the file's whole-row duplicates (132 at 2026-09-15).
    # p_value stays text: 6,275 of them are below the smallest double (1E-396
    # reads as 0.0); pvalue_mlog is the number.
    # ponytail: if a release ever repeats the nine IDENTITY columns with different
    # mapping columns, merge refuses the duplicate key loudly; widen IDENTITY then.
    key = ", ".join(f"coalesce({c}, '')" for c in IDENTITY)
    association = con.sql(f"""
        SELECT DISTINCT md5(concat_ws(chr(31), {key})) AS association_key,
               study_accession, snps,
               list_sort(list_distinct(regexp_split_to_array(snps, '\\s*(;|,| x )\\s*'))) AS snp_ids,
               strongest_snp_risk_allele, p_value, pvalue_mlog::DOUBLE AS pvalue_mlog,
               p_value_text, or_beta::DOUBLE AS or_beta, ci_95_text, risk_allele_frequency,
               reported_genes, region, chr_id, chr_pos, context, intergenic = '1' AS intergenic,
               mapped_gene, str_split(snp_gene_ids, ', ') AS snp_gene_ids,
               upstream_gene_id, upstream_gene_distance::INTEGER AS upstream_gene_distance,
               downstream_gene_id, downstream_gene_distance::INTEGER AS downstream_gene_distance,
               merged = '1' AS merged, snp_id_current,
               mapped_trait, {_ids('mapped_trait_uri')} AS mapped_trait_ids
        FROM associations
    """).to_arrow_table()

    # The Catalog is each table's only writer, so the scope is the whole table.
    return {
        "clinical.gwas_catalog__study": merge.merge(
            cat, "clinical.gwas_catalog__study", study, release, AlwaysTrue()),
        "clinical.gwas_catalog__association": merge.merge(
            cat, "clinical.gwas_catalog__association", association, release, AlwaysTrue()),
    }


def ingest(cat, release, url=None):
    version, counts = land_raw(cat, release, url)
    return {**counts, **transform(cat, release, version)}
