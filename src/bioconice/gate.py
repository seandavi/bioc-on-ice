"""Publish gate (#183 step 5): a release bundle against the previous published release.

The mechanics are `cdsci.publish.gate`, shared with cancer-on-ice (cdsci-lake ADR-0027):
per table, manifest row count and columns against the Parquet; against the reference's
manifest.json, no table missing, columns and types unchanged, row counts within bounds;
declared keys not null and unique, declared references covered; README queries as warnings;
a reference that must be strictly older; gate.json on a pass, bound to manifest.json by
sha256 (`bioconice hf-push` pushes only such a bundle). This module is the bioc policy:
the `TableContract` of every published table (`BUNDLE`, the keys and references the gate
checks), the exemption list for tables still without one (#208), the row bounds (0.9, 1.5),
`--allow-schema-change` (named tables may drop the row-history columns, #222) and
`--allow-column-change` (any column change, #183), both owner-approved and recorded.
"""

from pathlib import Path

import cdsci.publish.gate
from cdsci.lake.contracts import (
    ColumnContract,
    DatasetContract,
    TableContract,
    TemporalModel,
)
from cdsci.publish.gate import GATE_FILE, Check  # noqa: F401 (GATE_FILE re-exported for hf)
from pyiceberg.types import ListType

from . import icite, pubtator3
from .publish import PUBMED_ARTICLE, ROW_HISTORY, SIGNATURE_TAXON
from .schemas import _OBO_ONTOLOGIES
from .schemas import TABLES as DECLARED
from .upload import PUBLIC_URL

README = Path(__file__).parents[2] / "README.md"
_ARROW = {"string": "string", "int": "int32", "long": "int64", "boolean": "bool",
          "double": "double"}


def _arrow(t):
    return f"list<{_arrow(t.element_type)}>" if isinstance(t, ListType) else _ARROW[str(t)]


def declared_contract(name, primary_key=None, references=None, license=None, grain=None,
                      unpublished=(), published=None):
    """A TableContract from the table's declaration in schemas.py: its comment, columns,
    docs and licence (`license` where it declares none), keyed by `primary_key` (default:
    its business key). `grain` is for a table with no key. Row-history columns are not
    published (#222), so not in the contract, nor are the `unpublished` ones. `published`
    is a table whose released columns differ from its declaration (icite.PUBLISHED)."""
    d = DECLARED[name]
    key = tuple(primary_key or d.business_key)
    if published:
        comment, docs = icite.published_docs(name, d)
        columns = tuple(ColumnContract(c, t, docs[c], c not in key)
                        for c, t, _ in icite.PUBLISHED[name][1])
    else:
        comment, columns = d.comment, None
    return TableContract(
        name=name, description=comment, grain=grain or f"one row per ({', '.join(key)})",
        primary_key=key, temporal_model=TemporalModel.UPSERT_LATEST_SNAPSHOT,
        owner="bioc-on-ice", license=license or d.properties["bioc.license"],
        columns=columns or tuple(
            ColumnContract(f.name, _arrow(f.field_type), f.doc or "", not f.required)
            for f in d.schema.fields if f.name not in ROW_HISTORY and f.name not in unpublished),
        references=references or {})


ENCODE_LICENSE = "LicenseRef-ENCODE-Data-Use-Policy"
# BEDbase states no site-wide data licence; each record carries a DUO code, and every
# record of the 2026-09-01 snapshot is DUO:0000042, general research use (cdsci-lake
# docs/data-licenses.md). schemas.py declares no bioc.license for it.
BEDBASE_LICENSE = "LicenseRef-DUO-0000042"
OBO_LICENSE = " AND ".join(sorted({licence for _, licence in _OBO_ONTOLOGIES}))
# Ensembl names no licence instrument, only "no restrictions on access to, or use of, the
# data"; cdsci-lake registers it as ensembl-no-restrictions (sources/ensembl/ingest.py).
# schemas.py declares no bioc.license for it.
ENSEMBL_LICENSE = "LicenseRef-ensembl-no-restrictions"


# Every published table with a TableContract: the gate checks its key and references.
BUNDLE = DatasetContract(
    id="bioconice", title="biocOnIce release bundle",
    description="Tables of the bioconice publish bundle", publisher="bioc-on-ice",
    tables={c.name: c for c in (
        SIGNATURE_TAXON,
        PUBMED_ARTICLE,
        # Read from cdsci-lake since #183 step 7: one version per source, so the raw
        # tables are keyed without their version column.
        declared_contract("raw.hgnc__complete_set", ("hgnc_id",)),
        declared_contract("annotation.hgnc__gene"),
        declared_contract("raw.complexportal__complex", ("complex_ac",)),
        declared_contract("annotation.complexportal__complex"),
        declared_contract("annotation.complexportal__participant", references={
            "complex_ac": "annotation.complexportal__complex.complex_ac"}),
        declared_contract("raw.wikipathways__gmt", ("file_name", "line_number")),
        declared_contract("annotation.wikipathways__pathway"),
        declared_contract("annotation.wikipathways__gene_pathway", references={
            "pathway_id": "annotation.wikipathways__pathway.pathway_id"}),
        # No key: the file's whole-row duplicates are kept, and line_number, the lake's
        # key, is not published. An empty key skips the key checks.
        declared_contract("raw.gwas_catalog__associations", (), grain=(
            "one row per data line of the release file; whole-row duplicates kept "
            "(132 at 2026-09-15)")),
        declared_contract("raw.gwas_catalog__studies", ("study_accession",)),
        declared_contract("clinical.gwas_catalog__study"),
        declared_contract("clinical.gwas_catalog__association", references={
            "study_accession": "clinical.gwas_catalog__study.study_accession"}),
        declared_contract("raw.ncbi__mane_summary", ("refseq_nuc", "ensembl_nuc")),
        declared_contract("annotation.mane__transcript"),
        declared_contract("raw.bugsigdb__full_dump", ("bsdb_id",)),
        declared_contract("raw.cellosaurus__release", ("line_number",)),
        declared_contract("annotation.cellosaurus__cell_line"),
        declared_contract("annotation.cellosaurus__xref", references={
            "accession": "annotation.cellosaurus__cell_line.accession"}),
        declared_contract("annotation.cellosaurus__disease", references={
            "accession": "annotation.cellosaurus__cell_line.accession"}),
        # The lake's merge key; pubmed is the one updatable attribute (cdsci-lake ncbi_gene2go).
        declared_contract("raw.ncbi__gene2go", (
            "taxon_id", "gene_id", "go_id", "evidence", "qualifier", "category")),
        declared_contract("annotation.ncbi__gene_go"),
        # The whole row is the lake's merge key. No reference from pubmed_id to
        # pubmed__article.pmid: ~0.06% of the PMIDs (968 of 1,496,495 for taxon 9606
        # on 2026-11-02) are not in it yet, and the gate allows no orphans.
        declared_contract("raw.ncbi__gene2pubmed", ("taxon_id", "gene_id", "pubmed_id")),
        declared_contract("annotation.ncbi__gene_pubmed"),
        # gene_id is the lake's merge key for gene_info. gene2ensembl's is every identifier
        # column, NULLs included (no RefSeq RNA or protein on a row), and the gate allows no
        # NULL in a key, so it gets no key, as raw.gwas_catalog__associations.
        declared_contract("raw.ncbi__gene_info", ("gene_id",)),
        declared_contract("raw.ncbi__gene_history", ("discontinued_gene_id",)),
        declared_contract("raw.ncbi__gene2ensembl", (), grain=(
            "one row per distinct (gene_id, ensembl_gene_id, rna_accession, ensembl_rna_id, "
            "protein_accession, ensembl_protein_id); the RNA and protein columns may be NULL")),
        declared_contract("annotation.ncbi__gene"),
        # The lake's merge key is every column but the gi numbers and symbol, and all of
        # them except taxon_id and gene_id are NULL on some rows (mature_peptide_accession
        # on every 9606 row), so no key, as gene2ensembl.
        declared_contract("raw.ncbi__gene2accession", (), grain=(
            "one row per distinct (taxon_id, gene_id, status, rna_nucleotide_accession_version, "
            "protein_accession_version, genomic_nucleotide_accession_version, "
            "start_position_on_the_genomic_accession, end_position_on_the_genomic_accession, "
            "orientation, assembly, mature_peptide_accession_version); all but taxon_id and "
            "gene_id may be NULL")),
        # The lake's merge key: a pair can carry two relationships in gene_group
        # ('Readthrough sibling' and 'Potential readthrough sibling'), so not the pair alone.
        declared_contract("raw.ncbi__gene_orthologs", ("gene_id", "relationship", "other_gene_id")),
        declared_contract("raw.ncbi__gene_group", ("gene_id", "relationship", "other_gene_id")),
        declared_contract("annotation.ortholog"),
        declared_contract("raw.eqtlcatalogue__dataset", ("dataset_id",)),
        # cdsci-lake lands the two files only if they name the same datasets.
        declared_contract("raw.eqtlcatalogue__tabix_ftp_paths", ("dataset_id",), references={
            "dataset_id": "raw.eqtlcatalogue__dataset.dataset_id"}),
        declared_contract("resource.eqtlcatalogue__dataset"),
        declared_contract("raw.cellxgene__dataset", ("dataset_version_id",)),
        declared_contract("resource.cellxgene__dataset"),
        # schemas.py declares no bioc.license for ENCODE: its Data Use Policy names no
        # licence instrument, only free use with citation (encodeproject.org/help/citing-encode/).
        declared_contract("raw.encode__experiment", ("accession",), license=ENCODE_LICENSE),
        declared_contract("raw.encode__file", ("title",), license=ENCODE_LICENSE),
        declared_contract("resource.encode__experiment", license=ENCODE_LICENSE),
        declared_contract("resource.encode__file", license=ENCODE_LICENSE),
        declared_contract("raw.bedbase__bed", ("id",), license=BEDBASE_LICENSE),
        declared_contract("raw.bedbase__bedset", ("id",), license=BEDBASE_LICENSE),
        # No orphan on either side in the 2026-09-01 snapshot (578,552 rows).
        declared_contract("raw.bedbase__bedset_membership", ("bedset_id", "bedfile_id"),
                          license=BEDBASE_LICENSE, references={
                              "bedset_id": "raw.bedbase__bedset.id",
                              "bedfile_id": "raw.bedbase__bed.id"}),
        declared_contract("resource.bedbase__bedfile", license=BEDBASE_LICENSE),
        declared_contract("resource.bedbase__bedset", license=BEDBASE_LICENSE),
        # Each row is under its writer's licence: eQTL Catalogue and CELLxGENE are CC BY 4.0.
        declared_contract("resource.resource_relationship",
                          license=f"CC-BY-4.0 AND {ENCODE_LICENSE} AND {BEDBASE_LICENSE}"),
        declared_contract("raw.biogrid__interactions", ("biogrid_interaction_id",)),
        # No key, as raw.gwas_catalog__associations: (file_name, line_number), the lake's
        # key, is not published, and the files hold whole-row duplicates.
        declared_contract("raw.intact__mitab", (), grain=(
            "one row per data line of intact.txt and intact_negative.txt; whole-row "
            "duplicates kept (2,144 rows in 1,052 groups at 2026-01-09)")),
        # Its business key holds on the lake union: 4,778,696 rows at BioGRID 5.0.263 and
        # IntAct 2026-10-17 (2026-11-03), none NULL, none repeated.
        declared_contract("annotation.interaction"),
        # No key, as raw.ncbi__gene2ensembl: a node is keyed by id and an edge by (sub, pred,
        # obj), so every column of the lake's key is NULL on one kind of row or the other.
        *(declared_contract(f"raw.obo__{name}", (), grain=(
            "one row per node (id) and one per edge (sub, pred, obj) of the ontology's "
            "release file")) for name, _ in _OBO_ONTOLOGIES),
        # Each row is under its ontology's licence.
        declared_contract("ontology.term", license=OBO_LICENSE),
        declared_contract("ontology.relationship", license=OBO_LICENSE),
        # One assembly per taxon is published (ensembl.RELEASE), without genome_id on the
        # feature tables, so their keys are the #94 business keys less genome_id.
        declared_contract("raw.ensembl__gtf", (), license=ENSEMBL_LICENSE,
                          unpublished=("genome_id",), grain=(
                              "one row per feature line of the GTF of each taxon's published "
                              "assembly")),
        declared_contract("reference.genome", license=ENSEMBL_LICENSE,
                          unpublished=("is_canonical",)),
        declared_contract("annotation.gene", ("gene_id", "taxon_id", "source"),
                          license=ENSEMBL_LICENSE, unpublished=("genome_id",)),
        declared_contract("annotation.transcript", ("transcript_id", "taxon_id", "source"),
                          license=ENSEMBL_LICENSE, unpublished=("genome_id",)),
        declared_contract("annotation.exon", ("exon_id", "transcript_id", "taxon_id", "source"),
                          license=ENSEMBL_LICENSE, unpublished=("genome_id",)),
        # No key: the lake's key, line_number, is not published, and the files declare none.
        *(declared_contract(f"raw.pubtator3__{kind}", (), grain=(
            f"one row per line of {kind}2pubtator3.gz")) for kind in pubtator3.KINDS),
        declared_contract("annotation.pubtator3__mention"),
        # SPEC's key, the whole tuple. Each row is under its writer's licence: NCBI, NCBI
        # accession and MANE under NCBI's, Ensembl's, HGNC and RNAcentral CC0, UniProt CC BY.
        declared_contract("annotation.identifier_mapping", license=" AND ".join(sorted({
            "LicenseRef-NCBI-Molecular-Data-Usage", ENSEMBL_LICENSE, "CC0-1.0", "CC-BY-4.0"}))),
        # No key, as raw.intact__mitab: line_number, the lake's key, is not published, and no
        # column set the file declares is one (gene_name is NULL on 86% of rows).
        declared_contract("raw.rnacentral__id_mapping", (), grain=(
            "one row per line of the release's id_mapping.tsv.gz: one cross-reference from a "
            "sequence in one organism to a member database's record")),
        declared_contract("annotation.rnacentral__rna"),
        # Published typed from cdsci-lake (icite.PUBLISHED). schemas.py declares no
        # bioc.license for iCite: its Figshare snapshots are CC BY 4.0. citing_pmid is always
        # an iCite record; cited_pmid is not (68,932 edges in 2026-08 cite a paper iCite has
        # no record of), so only citing_pmid is a reference.
        declared_contract("raw.icite__metadata", ("pmid",), license="CC-BY-4.0",
                          published=True),
        declared_contract("annotation.icite__publication", license="CC-BY-4.0",
                          published=True),
        declared_contract("annotation.icite__metrics", ("pmid",), license="CC-BY-4.0",
                          published=True, references={
                              "pmid": "annotation.icite__publication.pmid"}),
        declared_contract("annotation.icite__citation", license="CC-BY-4.0", published=True,
                          references={"citing_pmid": "annotation.icite__publication.pmid"}),
    )})
KEYS = {c.name: c.primary_key for c in BUNDLE.tables.values() if c.primary_key}
REFERENCES = {c.name: c.references for c in BUNDLE.tables.values() if c.references}
# latest/ is the newest completed release: upload writes its manifest.json last.
DEFAULT_REFERENCE = f"{PUBLIC_URL}/latest"
USER_AGENT = "bioconice gate (+https://github.com/seandavi/bioc-on-ice)"

# Tables with no TableContract yet (#208). They get only the column and row-count checks:
# no key or join checks. Writing a contract for one means deleting its line here.
UNCONTRACTED_TABLES = frozenset({
    "provenance.release",
    "raw.metadata",
})


def contract_policy(con, manifest, ref):
    """#208: a table with no TableContract fails `contract_declared`, unless it is in
    UNCONTRACTED_TABLES and in the reference (owner-approved exemption: it still gets the
    column and row-count checks), and then only warns. A new table is never exempt."""
    previous = {f"{t['schema']}.{t['table']}" for t in ref["tables"]} if ref else set()
    for t in manifest["tables"]:
        name = f"{t['schema']}.{t['table']}"
        if name not in BUNDLE.tables:
            exempt = name in UNCONTRACTED_TABLES and name in previous
            yield Check(f"{name}.contract_declared", False, not exempt,
                        "no TableContract: key and join checks not run"
                        + ("; exempted by caller" if exempt else ""))


def gate(release: Path, reference: str | None = DEFAULT_REFERENCE,
         allow_schema_change=(), allow_column_change=()) -> bool:
    """Print the gate report for the bundle `release` against `reference` (a release base
    URL or directory); True when every required check passes, and then gate.json is in the
    bundle. Tables in `allow_schema_change` may drop row-history columns (owner approval,
    #222); those in `allow_column_change` may change columns in any way (owner approval,
    #183). Every other check still runs on both."""
    passed, _ = cdsci.publish.gate.gate(
        release, reference, queries=cdsci.publish.gate.readme_queries(README), catalog="bioc",
        keys=KEYS, references=REFERENCES,
        allow_dropped={t: ROW_HISTORY for t in allow_schema_change},
        allow_column_change=allow_column_change, extra_checks=contract_policy,
        user_agent=USER_AGENT)
    return passed
