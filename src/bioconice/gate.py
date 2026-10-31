"""Publish gate (#183 step 5): a release bundle against the previous published release.

`cdsci.lake.publish.verify.verify_release_over_release` does the checking: per table,
columns and types unchanged, row count within bounds, key not null and unique, join
coverage. This module supplies bioc-on-ice's contracts and its exemption list, refuses a
reference that is not older than the bundle, checks the bundle's manifest.json against its
own Parquet, runs the README queries on the bundle (optional), and prints the report.
Reads the bundle and the reference's manifest.json only.
"""

import json
import re
from pathlib import Path

import duckdb
from cdsci.lake.contracts import DatasetContract
from cdsci.lake.publish.release import AcceptanceCheck
from cdsci.lake.publish.verify import verify_release_over_release

from .publish import PUBMED_ARTICLE, _lit
from .release import BUGSIGDB
from .upload import PUBLIC_URL

README = Path(__file__).parents[2] / "README.md"
# The bundle's own tables that have a contract outside a dataset release of their own.
BUNDLE = DatasetContract(
    id="bioconice", title="biocOnIce release bundle",
    description="Tables of the bioconice publish bundle", publisher="bioc-on-ice",
    tables={PUBMED_ARTICLE.name: PUBMED_ARTICLE})
CONTRACTS = [BUGSIGDB, BUNDLE]
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


def readme_queries(path=README):
    """(line, sql) for each ```sql block in the README that is a query (SELECT / WITH)."""
    text = path.read_text()
    return [(text[:m.start()].count("\n") + 2, m[1].strip().rstrip(";"))
            for m in re.finditer(r"```sql\n(.*?)```", text, re.DOTALL)
            if re.match(r"\s*(SELECT|WITH)\b", m[1], re.IGNORECASE)]


def release_con(release: Path, tables):
    """The bundle's local Parquet as `bioc.<schema>.<table>` views, the names the README uses."""
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS bioc")
    for s in sorted({t["schema"] for t in tables}):
        con.execute(f"CREATE SCHEMA bioc.{s}")
    for t in tables:
        con.execute(f"CREATE VIEW bioc.{t['schema']}.{t['table']} AS "
                    f"SELECT * FROM read_parquet({_lit(str(release / t['path']))})")
    # Schema-qualified names in a README query (`provenance.release`) resolve in bioc.
    con.execute("USE bioc")
    return con


def bundle_checks(con, tables, queries):
    """Required: each table's manifest row_count, columns and types match its Parquet.
    Optional: each README query runs on the bundle."""
    checks = []
    for t in tables:
        name = f"{t['schema']}.{t['table']}"
        n = con.execute(f"SELECT count(*) FROM bioc.{name}").fetchone()[0]
        columns = [tuple(c[:2]) for c in con.execute(f"DESCRIBE bioc.{name}").fetchall()]
        declared = [(c["name"], c["type"]) for c in t["columns"]]
        checks.append(AcceptanceCheck(f"{name}.manifest_row_count", n == t["row_count"], True,
                                      f"parquet {n}, manifest {t['row_count']}"))
        checks.append(AcceptanceCheck(f"{name}.manifest_columns", columns == declared, True,
                                      "" if columns == declared else "manifest and Parquet differ"))
    for line, sql in queries:
        try:
            n, detail = len(con.execute(sql).fetchall()), ""
        except duckdb.Error as e:  # type name only: the message can carry a local path
            n, detail = None, type(e).__name__
        checks.append(AcceptanceCheck(f"readme_query_line_{line}", n is not None, False,
                                      detail or f"{n} rows"))
    return checks


def gate(release: Path, reference: str | None = DEFAULT_REFERENCE) -> bool:
    """Print the gate report for the bundle `release` against `reference` (a release base
    URL or directory); True when every required check passes."""
    manifest = json.loads((release / "manifest.json").read_text())
    con = release_con(release, manifest["tables"])
    if reference is not None:
        try:
            ref_release = json.loads(con.execute(
                "SELECT content FROM read_text(?)",
                [f"{reference.rstrip('/')}/manifest.json"]).fetchone()[0])["release"]
        except (duckdb.Error, ValueError, KeyError) as e:
            print(f"FAIL: reference {reference}: no readable manifest.json ({type(e).__name__})")
            return False
        print(f"release {manifest['release']} against {reference} (release {ref_release})")
        # ISO dates: string order is date order. latest/ after an upload is the bundle itself.
        if ref_release >= manifest["release"]:
            print(f"FAIL: reference release {ref_release} is not older than {manifest['release']}; "
                  f"pass --reference {PUBLIC_URL}/v<date> with an earlier release date")
            return False
    report = verify_release_over_release(
        "bioconice", release, reference, CONTRACTS, UNCONTRACTED_TABLES)
    checks = [*report.checks, *bundle_checks(con, manifest["tables"], readme_queries())]
    by_table: dict[str, list] = {}
    for c in checks:
        # Table checks are named `<schema>.<table>.<check>`; release-wide ones have no dot.
        parts = c.name.split(".", 2)
        table, check = (".".join(parts[:2]), parts[2]) if len(parts) == 3 else ("(release)", c.name)
        by_table.setdefault(table, []).append((check, c))
    for table, rows in by_table.items():
        print(table)
        for check, c in rows:
            status = "ok" if c.passed else "FAIL" if c.required else "warn"
            print(f"  {status:4}  {check}" + (f"  ({c.detail})" if c.detail else ""))
    failed = sum(c.required and not c.passed for c in checks)
    print(f"FAIL: {failed} required check(s) failed" if failed else "PASS")
    return not failed
