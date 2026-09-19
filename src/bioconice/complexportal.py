"""Complex Portal -> Iceberg: curated and predicted macromolecular complexes.

Complex Portal (EMBL-EBI, CC0) describes stable complexes: a named assembly with
N participants and their stoichiometry, a GO annotation, and the evidence for it.
That is a different shape from a binary interaction, so it gets its own two
tables rather than rows in a pairwise one.

It is released with IntAct, frozen under `pub/databases/intact/complex/<date>/`;
the date is the version (2026-01-09 is current on 2026-09-18). `complextab/` holds
one TSV per species plus `9606_predicted.tsv` (hu.MAP machine-learning
predictions), all with one 19-column header: 29 files, 20,579 complexes, of
which 15,284 are predicted. Every file lands — raw is whole, and the file name
is kept because it is the only thing that says 'predicted'. The files are small
(14 MB together), so DuckDB reads them straight from the FTP's HTTPS face and
nothing here streams. Raw accumulates per version, like raw.hgnc__complete_set.

The complex AC (CPX-663) is the key: unique across all 29 files, curated and
predicted alike. Participants come from 'Identifiers (and stoichiometry) of
molecules in complex', the complex as curated; the 'Expanded participant list',
which flattens sub-complexes into their members, stays in raw.

Participants stay in Complex Portal's identifier space, the namespace read off
the id's form: UniProtKB (98%), ChEBI, a CPX- sub-complex, RNAcentral, IntAct.
A **molecule set** — `[P02400,P05319](1)`, interchangeable paralogs filling one
position — becomes one row per member with the set kept beside it, so a join on
UniProt accession finds the complex; 115 of 101,431 participants are sets, and
(complex, member) stays unique through the explosion.

ponytail: GO annotations, cross-references, ligands and disease stay on the
complex row as published '|'-separated 'ID(label)' strings. Explode them into
annotation.complexportal__go etc. when a join needs them.
"""

import glob
import re
import urllib.request

import duckdb
from pyiceberg.expressions import AlwaysTrue, EqualTo

from . import merge

FTP = "https://ftp.ebi.ac.uk/pub/databases/intact/complex/"

# The files' header -> raw column name, in file order.
COLUMNS = {
    "#Complex ac": "complex_ac",
    "Recommended name": "recommended_name",
    "Aliases for complex": "aliases",
    "Taxonomy identifier": "taxonomy_identifier",
    "Identifiers (and stoichiometry) of molecules in complex": "participants",
    "Evidence Code": "evidence_code",
    "Experimental evidence": "experimental_evidence",
    "Go Annotations": "go_annotations",
    "Cross references": "cross_references",
    "Description": "description",
    "Complex properties": "complex_properties",
    "Complex assembly": "complex_assembly",
    "Ligand": "ligand",
    "Disease": "disease",
    "Agonist": "agonist",
    "Antagonist": "antagonist",
    "Comment": "comment",
    "Source": "source",
    "Expanded participant list": "expanded_participants",
}


def _index(url):
    """The hrefs of an FTP-over-HTTPS directory listing."""
    with urllib.request.urlopen(url, timeout=60) as r:
        return re.findall(r'href="([^"?/][^"]*)"', r.read().decode("utf-8", "replace"))


def latest():
    """The newest dated release directory, e.g. '2026-01-09'."""
    found = [h.rstrip("/") for h in _index(FTP) if re.fullmatch(r"\d{4}-\d{2}-\d{2}/", h)]
    if not found:
        raise SystemExit(f"complexportal: no dated release directory listed at {FTP}")
    return max(found)


def land_raw(cat, release, version=None, url=None):
    """Phase 1: every complextab file of one release, verbatim and whole.

    Returns (version, rows). `url` is a `<YYYY-MM-DD>/complextab/` directory —
    another release on the FTP, or a local copy laid out the same way; the dated
    directory states the version, which is why `current/` is not accepted.
    """
    if url:
        dated = re.search(r"/(\d{4}-\d{2}-\d{2})/complextab/?$", url)
        if not dated:
            raise SystemExit(f"complexportal: {url} is not a <YYYY-MM-DD>/complextab/ directory; "
                             "the dated directory is where the version comes from")
        version, url = dated.group(1), url.rstrip("/") + "/"
    else:
        version = version or latest()
        url = f"{FTP}{version}/complextab/"
    files = ([url + h for h in _index(url) if h.endswith(".tsv")] if url.startswith("http")
             else sorted(glob.glob(url + "*.tsv")))
    if not files:
        raise SystemExit(f"complexportal: no .tsv files at {url}")

    # 29 small files from one Apache: uncapped, DuckDB opens a connection per core
    # and EBI refuses some ('Could not connect to server', seen 2026-09-18). The
    # same cap-and-retry bedbase.py uses for its many-small-requests read.
    con = duckdb.connect(config={"threads": "4", "http_retries": "6",
                                 "http_retry_wait_ms": "2000", "http_retry_backoff": "2"})
    # The dialect is stated, not sniffed. Cells use '"' as content
    # (psi-mi:"MI:0469"(IntAct)), so quoting is off; '-' is the missing marker and
    # reads as NULL. Columns are read by header name, so a renamed or dropped one
    # fails in the binder; an added one fails the check below.
    read = (f"read_csv({files}, delim='\\t', header=true, all_varchar=true, quote='', escape='', "
            f"nullstr='-', filename=true)")
    header = [r[0] for r in con.sql(f"DESCRIBE SELECT * EXCLUDE (filename) FROM {read}").fetchall()]
    if header != list(COLUMNS):
        raise SystemExit(f"complexportal: {url} header is not the declared one; "
                         f"differs in {sorted(set(header) ^ set(COLUMNS))}")
    select = ", ".join(f'"{h}" AS {c}' for h, c in COLUMNS.items())
    arrow = con.sql(f"""
        SELECT {select}, parse_filename(filename) AS file,
               '{version}' AS complexportal_version, '{release}' AS landed_in
        FROM {read}
    """).to_arrow_table()

    n = merge.write(cat, "raw.complexportal__complex", arrow,
                    EqualTo("complexportal_version", version))
    merge.manifest(cat, release, "complexportal", "complex", url, n, version=version, method="release_number")
    return version, n


# A participant id's authority, read off its form. UniProtKB's own accession
# pattern, with the isoform (-2) and processed-chain (-PRO_0000…) suffixes
# Complex Portal uses. Three ids in 2026-01-09 match nothing (GenBank nucleotide
# accessions such as M14387) and get a NULL namespace rather than a guessed one.
UNIPROT = r"^([OPQ][0-9][A-Z0-9]{3}[0-9]|[A-NR-Z][0-9]([A-Z][A-Z0-9]{2}[0-9]){1,2})(-[0-9]+|-PRO_[0-9]+)?$"
NAMESPACE = f"""CASE WHEN id LIKE 'CHEBI:%' THEN 'CHEBI' WHEN id LIKE 'CPX-%' THEN 'COMPLEXPORTAL'
                     WHEN id LIKE 'URS%' THEN 'RNACENTRAL' WHEN id LIKE 'EBI-%' THEN 'INTACT'
                     WHEN regexp_matches(id, '{UNIPROT}') THEN 'UNIPROT' END"""


def transform(cat, release, version):
    """Phase 2: the complex record, and its participants one per row."""
    con = duckdb.connect()
    con.register("raw", cat.load_table("raw.complexportal__complex").scan(
        row_filter=EqualTo("complexportal_version", version)).to_arrow())

    # 'ECO:0000353(physical interaction evidence …)' and 'psi-mi:"MI:0469"(IntAct)'
    # are split into id and label; the '|'-lists stay as published (module docstring).
    complex_ = con.sql(r"""
        SELECT complex_ac, taxonomy_identifier::INTEGER AS taxon_id, recommended_name AS name,
               aliases, file LIKE '%\_predicted.tsv' ESCAPE '\' AS predicted,
               regexp_extract(evidence_code, '^(ECO:[0-9]+)', 1) AS evidence_code,
               regexp_extract(evidence_code, '^ECO:[0-9]+\((.*)\)$', 1) AS evidence,
               experimental_evidence, complex_assembly AS assembly, go_annotations,
               cross_references, description, complex_properties AS properties,
               ligand, disease, agonist, antagonist, comment,
               regexp_extract(source, '\((.*)\)$', 1) AS curated_by
        FROM raw
    """).to_arrow_table()

    # 'P04637(0)|[P02400,P05319](1)|CHEBI:29105(2)': '|' separates participants,
    # '(n)' is the stoichiometry with (0) upstream's 'unknown', '[a,b]' a molecule
    # set. A token that is not 'id(n)' yields a NULL id and fails the cast rather
    # than vanishing. No taxon here: the complex's is the pathogen's for a
    # host-pathogen complex, so it is not every participant's.
    participant = con.sql(rf"""
        SELECT complex_ac, id AS participant_id, {NAMESPACE} AS participant_namespace,
               NULLIF(stoichiometry, 0) AS stoichiometry,
               CASE WHEN token LIKE '[%' THEN token END AS molecule_set
        FROM (SELECT complex_ac, token, stoichiometry,
                     unnest(coalesce(str_split(trim(token, '[]'), ','), [NULL])) AS id
              FROM (SELECT complex_ac,
                           NULLIF(regexp_extract(p, '^(.+)\([0-9]+\)$', 1), '') AS token,
                           TRY_CAST(regexp_extract(p, '\(([0-9]+)\)$', 1) AS INTEGER) AS stoichiometry
                    FROM (SELECT complex_ac, unnest(str_split(participants, '|')) AS p FROM raw)))
    """).to_arrow_table()

    # Complex Portal is these tables' only writer, so its scope is the whole table.
    return {
        "annotation.complexportal__complex": merge.merge(
            cat, "annotation.complexportal__complex", complex_, release, AlwaysTrue()),
        "annotation.complexportal__participant": merge.merge(
            cat, "annotation.complexportal__participant", participant, release, AlwaysTrue()),
    }


def ingest(cat, release, version=None, url=None):
    version, n = land_raw(cat, release, version, url)
    return {f"raw.complexportal__complex [{version}]": n, **transform(cat, release, version)}
