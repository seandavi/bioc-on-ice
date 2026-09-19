"""BioGRID -> Iceberg: curated physical and genetic interactions, every organism.

BioGRID publishes a numbered release monthly (5.0.261 on 2026-08-31) and keeps
every one under Release-Archive, so this is the versioned, whole-dump case. The
file landed is `BIOGRID-ALL-x.y.zzz.tab3.zip`: one tab-separated text file, one
row per curated interaction evidence (2,936,093 in 5.0.261; 95 organisms; 2.05M
physical, 0.89M genetic), MIT licensed — the notice travels in the table comments.

**It is a zip, and DuckDB does not read zip.** The project adds no dependency
for that: stdlib `zipfile` extracts the one member to a scratch file, streamed,
and DuckDB reads the text. `fetch` is that step, shared with intact.py.

Raw is landed **whole** — every organism, every column — but as the *latest*
release only, the narrowing raw.icite__metadata makes and for the same reason:
a release is ~3M wide rows, and every release stays citable upstream.

The derived rows go to `annotation.interaction`, which IntAct writes too, each
under its own `source` scope so neither can retire the other's rows (ADR-0004).

**The key is BioGRID's own interaction id**, not the pair. Measured on 5.0.261:
the id is unique on every row, while (unordered pair, experimental system,
publication) — the key the issue floated — collides on 190,628 rows in 93,572
groups (one paper reporting a pair twice under one system, e.g. both bait/prey
directions). Across six releases, 5.0.255 -> 5.0.261, all 2,842,465 shared ids
kept their interactors, system and publication; 5,054 were withdrawn and 93,628
added. So the id is stable and the merge's retire/new legs mean what they say.

**Interactors stay in BioGRID's identifier space**: Entrez gene ids, or the
BioGRID id for the 7,076 A-side and 222 B-side interactors that have no Entrez
gene (the namespace column says which). The UniProt accessions BioGRID lists
alongside are many-to-one per gene and stay in raw; mapping is a join through
annotation.identifier_mapping, not something to bake in here.

ponytail: the tab3 file carries BioGRID's own experimental-system vocabulary
('Affinity Capture-Western'), not PSI-MI ids, so detection_method_id and
interaction_type_id are NULL for BIOGRID rows. BioGRID's MITAB export has the
MI mapping; land that beside this if a cross-source method filter is needed.
"""

import os
import re
import shutil
import tempfile
import urllib.request
import zipfile
from pathlib import Path

import duckdb
from pyiceberg.expressions import EqualTo

from . import merge
from .ncbi import _land

ARCHIVE = "https://downloads.thebiogrid.org/BioGRID/Release-Archive/"
DOWNLOAD = ("https://downloads.thebiogrid.org/Download/BioGRID/Release-Archive/"
            "BIOGRID-{v}/BIOGRID-ALL-{v}.tab3.zip")

# The file's header -> raw column name, in file order. Checked whole before
# anything is read: the column spec below is positional, so an inserted or
# reordered upstream column must fail rather than shift every value.
COLUMNS = {
    "#BioGRID Interaction ID": "biogrid_interaction_id",
    "Entrez Gene Interactor A": "entrez_gene_a",
    "Entrez Gene Interactor B": "entrez_gene_b",
    "BioGRID ID Interactor A": "biogrid_id_a",
    "BioGRID ID Interactor B": "biogrid_id_b",
    "Systematic Name Interactor A": "systematic_name_a",
    "Systematic Name Interactor B": "systematic_name_b",
    "Official Symbol Interactor A": "official_symbol_a",
    "Official Symbol Interactor B": "official_symbol_b",
    "Synonyms Interactor A": "synonyms_a",
    "Synonyms Interactor B": "synonyms_b",
    "Experimental System": "experimental_system",
    "Experimental System Type": "experimental_system_type",
    "Author": "author",
    "Publication Source": "publication_source",
    "Organism ID Interactor A": "organism_id_a",
    "Organism ID Interactor B": "organism_id_b",
    "Throughput": "throughput",
    "Score": "score",
    "Modification": "modification",
    "Qualifications": "qualifications",
    "Tags": "tags",
    "Source Database": "source_database",
    "SWISS-PROT Accessions Interactor A": "swissprot_a",
    "TREMBL Accessions Interactor A": "trembl_a",
    "REFSEQ Accessions Interactor A": "refseq_a",
    "SWISS-PROT Accessions Interactor B": "swissprot_b",
    "TREMBL Accessions Interactor B": "trembl_b",
    "REFSEQ Accessions Interactor B": "refseq_b",
    "Ontology Term IDs": "ontology_term_ids",
    "Ontology Term Names": "ontology_term_names",
    "Ontology Term Categories": "ontology_term_categories",
    "Ontology Term Qualifier IDs": "ontology_term_qualifier_ids",
    "Ontology Term Qualifier Names": "ontology_term_qualifier_names",
    "Ontology Term Types": "ontology_term_types",
    "Organism Name Interactor A": "organism_name_a",
    "Organism Name Interactor B": "organism_name_b",
}


def _open(url):
    """urlopen with a User-Agent: BioGRID sits behind Cloudflare, which 403s urllib's default."""
    return urllib.request.urlopen(
        urllib.request.Request(url, headers={"User-Agent": "bioc-on-ice"}), timeout=600)


def latest():
    """The newest release number in the archive index, e.g. '5.0.261'."""
    with _open(ARCHIVE) as r:
        found = set(re.findall(r"BIOGRID-(\d+\.\d+\.\d+)", r.read().decode("utf-8", "replace")))
    if not found:
        raise SystemExit(f"biogrid: no BIOGRID-x.y.zzz release listed at {ARCHIVE}")
    return max(found, key=lambda v: tuple(map(int, v.split("."))))


def fetch(source, version, url):
    """Download a zip and extract every member, once; returns the members' paths.

    Streamed to disk at both steps — the zip is 0.2 GB (BioGRID) to 1.3 GB
    (IntAct) and the text 1.5 to 11 GB — under BIOCONICE_SCRATCH (default: the
    system temp dir), as icite.fetch does. Re-running reuses what is there; the
    download lands under a .part name first so a broken one is never reused.
    """
    scratch = Path(os.environ.get("BIOCONICE_SCRATCH", tempfile.gettempdir())) / source / version
    scratch.mkdir(parents=True, exist_ok=True)
    zpath = scratch / url.rsplit("/", 1)[-1]
    if not zpath.exists():
        part = zpath.with_name(zpath.name + ".part")
        with _open(url) as r, open(part, "wb") as f:
            shutil.copyfileobj(r, f)
        part.rename(zpath)
    with zipfile.ZipFile(zpath) as z:
        for info in z.infolist():
            out = scratch / info.filename
            if not (out.exists() and out.stat().st_size == info.file_size):
                z.extract(info, scratch)
        return [scratch / n for n in sorted(z.namelist())]


def check_header(path, columns):
    """Fail unless the file's first line is exactly the declared header."""
    with open(path, encoding="utf-8") as f:
        header = tuple(f.readline().rstrip("\r\n").split("\t"))
    if header != tuple(columns):
        raise SystemExit(f"{path.name}: header is not the declared one; "
                         f"differs in {sorted(set(header) ^ set(columns))}")


def land_raw(cat, release, version=None, url=None):
    """Phase 1: the ALL tab3 file, verbatim and whole, replacing the previous release.

    Returns (version, rows). `url` is a tab3 zip — an archived release or a
    file:// copy; its name states the version, as hgnc's dated archives do.
    """
    if url:
        named = re.search(r"BIOGRID-ALL-(\d+\.\d+\.\d+)\.tab3", url)
        if not named:
            raise SystemExit(f"biogrid: {url} is not a BIOGRID-ALL-x.y.zzz.tab3.zip; "
                             "the file name is where the version comes from")
        version = named.group(1)
    else:
        version = version or latest()
        url = DOWNLOAD.format(v=version)
    members = fetch("biogrid", version, url)
    if len(members) != 1:
        raise SystemExit(f"biogrid: {url} should hold one text file, found {[m.name for m in members]}")
    txt = members[0]
    check_header(txt, COLUMNS)
    # The dialect is stated, not sniffed. The file has no quoting (no '"' at
    # all in 5.0.261), so quoting is off rather than left to mangle a future
    # gene synonym that has one; '-' is BioGRID's missing marker and reads as
    # NULL, the treatment NCBI's '-' gets.
    spec = "{" + ", ".join(f"'{c}': 'VARCHAR'" for c in COLUMNS.values()) + "}"
    n = _land(cat, release, "raw.biogrid__interactions",
              f"(SELECT *, '{version}' AS biogrid_version FROM read_csv('{txt}', delim='\\t', "
              f"header=true, auto_detect=false, columns={spec}, quote='', escape='', nullstr='-'))")
    merge.manifest(cat, release, "biogrid", "interactions", url, n, version=version, method="release_number")
    return version, n


def transform(cat, release, version):
    """Phase 2: BioGRID's rows of annotation.interaction, in its own identifier space."""
    con = duckdb.connect()
    con.register("raw", cat.load_table("raw.biogrid__interactions").scan(
        row_filter=EqualTo("biogrid_version", version),
        selected_fields=("biogrid_interaction_id", "entrez_gene_a", "entrez_gene_b",
                         "biogrid_id_a", "biogrid_id_b", "organism_id_a", "organism_id_b",
                         "experimental_system", "experimental_system_type",
                         "publication_source")).to_arrow())
    # A/B order is kept as published: for the bait-prey systems A is the bait.
    # Publication Source is 'PUBMED:<id>' or, for 21,583 preprint rows, 'DOI:<doi>'.
    interaction = con.sql("""
        SELECT 'BIOGRID' AS source, biogrid_interaction_id AS interaction_id,
               CASE WHEN entrez_gene_a IS NULL THEN 'BIOGRID' ELSE 'ENTREZ' END AS interactor_a_namespace,
               coalesce(entrez_gene_a, biogrid_id_a) AS interactor_a_id,
               CASE WHEN entrez_gene_b IS NULL THEN 'BIOGRID' ELSE 'ENTREZ' END AS interactor_b_namespace,
               coalesce(entrez_gene_b, biogrid_id_b) AS interactor_b_id,
               organism_id_a::INTEGER AS taxon_id_a, organism_id_b::INTEGER AS taxon_id_b,
               NULL::VARCHAR AS detection_method_id, experimental_system AS detection_method,
               NULL::VARCHAR AS interaction_type_id, experimental_system_type AS interaction_type,
               NULL::VARCHAR AS expansion_method, false AS negative,
               CASE WHEN publication_source LIKE 'PUBMED:%' THEN substr(publication_source, 8) END AS pubmed_id,
               CASE WHEN publication_source LIKE 'DOI:%' THEN lower(substr(publication_source, 5)) END AS doi
        FROM raw
    """).to_arrow_table()
    # The scope names THIS writer: IntAct stacks into the same table.
    return {"annotation.interaction": merge.merge(
        cat, "annotation.interaction", interaction, release, EqualTo("source", "BIOGRID"))}


def ingest(cat, release, version=None, url=None):
    version, n = land_raw(cat, release, version, url)
    return {f"raw.biogrid__interactions [{version}]": n, **transform(cat, release, version)}
