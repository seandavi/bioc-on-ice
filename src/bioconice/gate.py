"""Publish gate (#183 step 5): a release bundle against the previous published release.

`cdsci.lake.publish.verify.verify_release_over_release` does the checking: per table,
columns and types unchanged, row count within bounds, key not null and unique, join
coverage. This module supplies bioc-on-ice's contracts and its exemption list, and prints
the report. Reads the bundle and the reference's manifest.json only.
"""

from pathlib import Path

from cdsci.lake.publish.verify import verify_release_over_release

from .release import BUGSIGDB
from .upload import PUBLIC_URL

CONTRACTS = [BUGSIGDB]
# latest/ is the newest completed release: upload writes its manifest.json last.
DEFAULT_REFERENCE = f"{PUBLIC_URL}/latest"

# Tables with no TableContract yet (#208). They get only the column and row-count checks:
# no key or join checks. Writing a contract for one means deleting its line here.
UNCONTRACTED_TABLES = frozenset({
    "annotation.cellosaurus__cell_line",
    "annotation.cellosaurus__disease",
    "annotation.cellosaurus__xref",
    "annotation.complexportal__complex",
    "annotation.complexportal__participant",
    "annotation.exon",
    "annotation.gene",
    "annotation.hgnc__gene",
    "annotation.icite__citation",
    "annotation.icite__metrics",
    "annotation.icite__publication",
    "annotation.identifier_mapping",
    "annotation.interaction",
    "annotation.mane__transcript",
    "annotation.ncbi__gene",
    "annotation.ncbi__gene_go",
    "annotation.ncbi__gene_pubmed",
    "annotation.ortholog",
    "annotation.pubmed__article",
    "annotation.pubtator3__mention",
    "annotation.rnacentral__rna",
    "annotation.transcript",
    "annotation.wikipathways__gene_pathway",
    "annotation.wikipathways__pathway",
    "clinical.gwas_catalog__association",
    "clinical.gwas_catalog__study",
    "ontology.relationship",
    "ontology.term",
    "provenance.release",
    "raw.bedbase__bed",
    "raw.bedbase__bedset",
    "raw.bedbase__bedset_membership",
    "raw.biogrid__interactions",
    "raw.bugsigdb__full_dump",
    "raw.cellosaurus__release",
    "raw.cellxgene__dataset",
    "raw.complexportal__complex",
    "raw.encode__experiment",
    "raw.encode__file",
    "raw.ensembl__gtf",
    "raw.eqtlcatalogue__dataset",
    "raw.eqtlcatalogue__tabix_ftp_paths",
    "raw.gwas_catalog__associations",
    "raw.gwas_catalog__studies",
    "raw.hgnc__complete_set",
    "raw.icite__metadata",
    "raw.intact__mitab",
    "raw.metadata",
    "raw.ncbi__gene2accession",
    "raw.ncbi__gene2ensembl",
    "raw.ncbi__gene2go",
    "raw.ncbi__gene2pubmed",
    "raw.ncbi__gene_group",
    "raw.ncbi__gene_history",
    "raw.ncbi__gene_info",
    "raw.ncbi__gene_orthologs",
    "raw.ncbi__mane_summary",
    "raw.obo__cl",
    "raw.obo__doid",
    "raw.obo__efo",
    "raw.obo__go",
    "raw.obo__hsapdv",
    "raw.obo__mmusdv",
    "raw.obo__mondo",
    "raw.obo__uberon",
    "raw.pubtator3__chemical",
    "raw.pubtator3__disease",
    "raw.pubtator3__gene",
    "raw.pubtator3__mutation",
    "raw.pubtator3__species",
    "raw.rnacentral__id_mapping",
    "raw.wikipathways__gmt",
    "reference.genome",
    "resource.bedbase__bedfile",
    "resource.bedbase__bedset",
    "resource.cellxgene__dataset",
    "resource.encode__experiment",
    "resource.encode__file",
    "resource.eqtlcatalogue__dataset",
    "resource.resource_relationship",
})


def gate(release: Path, reference: str | None = DEFAULT_REFERENCE) -> bool:
    """Print the gate report for the bundle `release` against `reference` (a release base
    URL or directory); True when every required check passes."""
    report = verify_release_over_release(
        "bioconice", release, reference, CONTRACTS, UNCONTRACTED_TABLES)
    print(f"release {report.release} against {reference}")
    by_table: dict[str, list] = {}
    for c in report.checks:
        # Table checks are named `<schema>.<table>.<check>`; release-wide ones have no dot.
        parts = c.name.split(".", 2)
        table, check = (".".join(parts[:2]), parts[2]) if len(parts) == 3 else ("(release)", c.name)
        by_table.setdefault(table, []).append((check, c))
    for table, checks in by_table.items():
        print(table)
        for check, c in checks:
            status = "ok" if c.passed else "FAIL" if c.required else "warn"
            print(f"  {status:4}  {check}" + (f"  ({c.detail})" if c.detail else ""))
    failed = sum(c.required and not c.passed for c in report.checks)
    print("PASS" if report.passed else f"FAIL: {failed} required check(s) failed")
    return report.passed
