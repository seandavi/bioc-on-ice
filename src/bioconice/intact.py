"""IntAct -> Iceberg: molecular interaction evidence in PSI-MI MITAB 2.7.

IntAct (EMBL-EBI; with MINT, DIP, UniProt and the other IMEx curators) publishes
dated releases, each frozen under `pub/databases/intact/<YYYY-MM-DD>/`; the date
is the version (2026-01-09 is current on 2026-09-18). `psimitab/intact.zip` holds
two MITAB files with one header: intact.txt (1,787,425 rows, 11 GB) and
intact_negative.txt (983 rows, interactions shown NOT to occur). Both land, since
the zip is the file; MITAB's own `negative` column tells them apart. CC BY 4.0:
"IntAct (EMBL-EBI), CC BY 4.0; del Toro et al. NAR 2022".

Raw is landed **whole** and verbatim — all 42 columns as text, every organism and
molecule type — as the *latest* release only, like raw.biogrid__interactions:
dated releases stay frozen upstream. The zip is handled as biogrid.py does.

The derived rows stack into `annotation.interaction` beside BioGRID's, under
source='INTACT' (ADR-0004).

**The interaction AC is stable but is not a row key.** Measured on 2026-01-09: the
1,787,425 rows carry 916,318 distinct `intact:EBI-…` ACs, because a MITAB row is
binary and 933,611 rows are the spoke expansion of an n-ary interaction (a
pull-down with one bait and N preys is N rows under one AC). So the key is the AC
plus the pair as published. Even that repeats on 4,051 rows (1,943 groups), which
differ only in participant detail this table does not carry — features, roles,
stoichiometry — so the derivation is DISTINCT, and that it then yields one row per
key is checked by the merge. It holds because detection method, interaction type,
publication and expansion are functions of the AC (0 exceptions), as is an
interactor's taxon within an AC (0 exceptions). Against 2025-08-08: of 870,728
shared ACs, 6 changed method and 11 publication; 23,970 changed an interactor id
(UniProt accession and isoform re-mapping), which this key records as one row
retired and one opened; 7 ACs were withdrawn and 45,590 added.

**Interactors stay in IntAct's identifier space**: the MITAB primary id, split
into namespace and id — UniProtKB accessions (95%, isoform and PRO-chain suffixes
kept), IntAct's own EBI- ids for molecules with no public accession, ChEBI,
Ensembl, RNAcentral, and a tail of a dozen others. Nothing is mapped to genes.

324 rows have no interactor B: single-participant interactions, mostly
autophosphorylation, each the only row of its AC. B repeats A there, the form
BioGRID and IntAct's own 21,879 A=B rows already use for self-interaction; a key
column cannot be NULL. Raw keeps the '-'.

ponytail: confidence (intact-miscore) is left in raw. It is recomputed from all
evidence for a pair, so it moves on rows whose own evidence did not change; as a
Type 2 attribute it would open new versions every release, the icite__metrics
lesson. Key it by release in its own table when something needs the score.
"""

import re

import duckdb
from pyiceberg.expressions import EqualTo

from . import merge
from .biogrid import _open, check_header, fetch
from .ncbi import _land

FTP = "https://ftp.ebi.ac.uk/pub/databases/intact/"

# MITAB 2.7 header -> raw column name, in file order; checked whole, as in biogrid.py.
COLUMNS = {
    "#ID(s) interactor A": "id_a",
    "ID(s) interactor B": "id_b",
    "Alt. ID(s) interactor A": "alt_ids_a",
    "Alt. ID(s) interactor B": "alt_ids_b",
    "Alias(es) interactor A": "aliases_a",
    "Alias(es) interactor B": "aliases_b",
    "Interaction detection method(s)": "detection_methods",
    "Publication 1st author(s)": "first_authors",
    "Publication Identifier(s)": "publication_ids",
    "Taxid interactor A": "taxid_a",
    "Taxid interactor B": "taxid_b",
    "Interaction type(s)": "interaction_types",
    "Source database(s)": "source_databases",
    "Interaction identifier(s)": "interaction_ids",
    "Confidence value(s)": "confidence_values",
    "Expansion method(s)": "expansion_methods",
    "Biological role(s) interactor A": "biological_roles_a",
    "Biological role(s) interactor B": "biological_roles_b",
    "Experimental role(s) interactor A": "experimental_roles_a",
    "Experimental role(s) interactor B": "experimental_roles_b",
    "Type(s) interactor A": "interactor_types_a",
    "Type(s) interactor B": "interactor_types_b",
    "Xref(s) interactor A": "xrefs_a",
    "Xref(s) interactor B": "xrefs_b",
    "Interaction Xref(s)": "interaction_xrefs",
    "Annotation(s) interactor A": "annotations_a",
    "Annotation(s) interactor B": "annotations_b",
    "Interaction annotation(s)": "interaction_annotations",
    "Host organism(s)": "host_organisms",
    "Interaction parameter(s)": "interaction_parameters",
    "Creation date": "creation_date",
    "Update date": "update_date",
    "Checksum(s) interactor A": "checksums_a",
    "Checksum(s) interactor B": "checksums_b",
    "Interaction Checksum(s)": "interaction_checksums",
    "Negative": "negative",
    "Feature(s) interactor A": "features_a",
    "Feature(s) interactor B": "features_b",
    "Stoichiometry(s) interactor A": "stoichiometry_a",
    "Stoichiometry(s) interactor B": "stoichiometry_b",
    "Identification method participant A": "identification_methods_a",
    "Identification method participant B": "identification_methods_b",
}


def latest():
    """The newest dated release directory on the FTP, e.g. '2026-01-09'."""
    with _open(FTP) as r:
        found = re.findall(r'href="(\d{4}-\d{2}-\d{2})/"', r.read().decode("utf-8", "replace"))
    if not found:
        raise SystemExit(f"intact: no dated release directory listed at {FTP}")
    return max(found)


def land_raw(cat, release, version=None, url=None):
    """Phase 1: both MITAB files of intact.zip, verbatim and whole, replacing the previous release.

    Returns (version, rows). `url` is an intact.zip under a dated release path
    (or a file:// copy laid out the same way): the directory states the version.
    `current/` is not accepted for that reason — it names no release.
    """
    if url:
        dated = re.search(r"/(\d{4}-\d{2}-\d{2})/psimitab/", url)
        if not dated:
            raise SystemExit(f"intact: {url} is not under <YYYY-MM-DD>/psimitab/; "
                             "the dated directory is where the version comes from")
        version = dated.group(1)
    else:
        version = version or latest()
        url = f"{FTP}{version}/psimitab/intact.zip"
    facts = merge.reading(release, "intact", "mitab", url)
    zpath, members = fetch("intact", version, url)
    for m in members:
        check_header(m, COLUMNS)

    # The dialect is stated, not sniffed. MITAB uses '"' inside cells
    # (psi-mi:"MI:0018"(two hybrid)) without it being a CSV quote, so quoting is
    # off; '-' is MITAB's missing marker and reads as NULL. max_line_size because
    # an alias or xref cell can run long, as iCite's citation lists do.
    # ponytail: ncbi._land commits every 5M rows, so all 1.8M wide rows (11 GB of
    # text) are held before the one commit: 17 GB peak, 61 s, measured. Give _land a
    # rows-per-commit argument if IntAct outgrows the ingest host.
    spec = "{" + ", ".join(f"'{c}': 'VARCHAR'" for c in COLUMNS.values()) + "}"
    n = _land(cat, release, "raw.intact__mitab",
              f"(SELECT *, '{version}' AS intact_version FROM read_csv({[str(m) for m in members]}, "
              f"delim='\\t', header=true, auto_detect=false, columns={spec}, quote='', escape='', "
              f"nullstr='-', max_line_size=268435456))")
    merge.manifest(cat, release, "intact", "mitab", url, n, version=version, method="release_number",
                   checksum=merge.sha256(zpath), **facts)
    return version, n


def _interactor(x):
    """(namespace, id) of a MITAB primary id, 'db:id' or 'db:"ID:with:colons"'.

    The namespace is upstream's database name upper-cased, except the two that
    already have a name in annotation.identifier_mapping.
    """
    return (f"CASE split_part({x}, ':', 1) WHEN 'uniprotkb' THEN 'UNIPROT' "
            f"WHEN 'entrezgene/locuslink' THEN 'ENTREZ' ELSE upper(split_part({x}, ':', 1)) END",
            f"""trim(substr({x}, strpos({x}, ':') + 1), '"')""")


def _taxon(col):
    # 'taxid:9606(human)|taxid:9606(Homo sapiens)': one id, two labels. IntAct's
    # negative pseudo-taxa (-1 in vitro, -2 chemical synthesis) are not organisms.
    t = f"TRY_CAST(regexp_extract({col}, 'taxid:(-?[0-9]+)', 1) AS INTEGER)"
    return f"CASE WHEN {t} > 0 THEN {t} END"


def _mi(col):
    """(PSI-MI id, label) of a CV cell: psi-mi:"MI:0018"(two hybrid). Some labels are quoted."""
    return (f"""NULLIF(regexp_extract({col}, '^psi-mi:"(MI:[0-9]+)"', 1), '')""",
            f"""NULLIF(regexp_extract({col}, '^psi-mi:"MI:[0-9]+"\\("?(.*?)"?\\)(\\||$)', 1), '')""")


def transform(cat, release, version):
    """Phase 2: IntAct's rows of annotation.interaction, in its own identifier space."""
    con = duckdb.connect()
    con.register("raw", cat.load_table("raw.intact__mitab").scan(
        row_filter=EqualTo("intact_version", version),
        selected_fields=("id_a", "id_b", "taxid_a", "taxid_b", "detection_methods",
                         "interaction_types", "expansion_methods", "publication_ids",
                         "interaction_ids", "negative")).to_arrow())
    (a_ns, a_id), (b_ns, b_id) = _interactor("id_a"), _interactor("b")
    (method_id, method), (type_id, type_) = _mi("detection_methods"), _mi("interaction_types")
    # A/B order is kept as published; a missing B repeats A (module docstring),
    # taxon included — but only then: a small molecule's own taxid is empty. DISTINCT, because the columns dropped here
    # (features, roles, stoichiometry) are all that tell some rows apart. A row
    # with no intact: AC yields a NULL key and fails the cast, as it should.
    interaction = con.sql(f"""
        SELECT DISTINCT 'INTACT' AS source,
               NULLIF(regexp_extract(interaction_ids, 'intact:(EBI-[0-9]+)', 1), '') AS interaction_id,
               {a_ns} AS interactor_a_namespace, {a_id} AS interactor_a_id,
               {b_ns} AS interactor_b_namespace, {b_id} AS interactor_b_id,
               {_taxon('taxid_a')} AS taxon_id_a, {_taxon('b_taxid')} AS taxon_id_b,
               {method_id} AS detection_method_id, {method} AS detection_method,
               {type_id} AS interaction_type_id, {type_} AS interaction_type,
               {_mi('expansion_methods')[1]} AS expansion_method, negative = 'true' AS negative,
               NULLIF(regexp_extract(publication_ids, 'pubmed:([0-9]+)', 1), '') AS pubmed_id,
               NULLIF(lower(regexp_extract(publication_ids, 'doi:"?(?:doi:)?(10\\.[^|"]+)', 1)), '') AS doi
        FROM (SELECT *, coalesce(id_b, id_a) AS b,
                     CASE WHEN id_b IS NULL THEN taxid_a ELSE taxid_b END AS b_taxid FROM raw)
    """).to_arrow_table()
    # The scope names THIS writer: BioGRID stacks into the same table.
    return {"annotation.interaction": merge.merge(
        cat, "annotation.interaction", interaction, release, EqualTo("source", "INTACT"))}


def ingest(cat, release, version=None, url=None):
    version, n = land_raw(cat, release, version, url)
    return {f"raw.intact__mitab [{version}]": n, **transform(cat, release, version)}
