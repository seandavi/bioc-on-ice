"""Publish gate (#183 step 5): a release bundle against the previous published release.

`cdsci.lake.publish.verify.verify_release_over_release` does the checking: per table,
columns and types unchanged, row count within bounds, key not null and unique, join
coverage. This module supplies bioc-on-ice's contracts and its exemption list, refuses a
reference that is not older than the bundle, checks the bundle's manifest.json against its
own Parquet, runs the README queries on the bundle (optional), and prints the report.
`--allow-schema-change` lets named tables drop the row-history columns (#222), and
`--allow-column-change` lets them change columns in any way (lake migration, #183); both
owner-approved, and both record the exact change.
Reads the bundle and the reference's manifest.json only. A pass leaves gate.json in the
bundle, bound to its manifest.json by sha256: `bioconice hf-push` pushes only such a bundle.
"""

import hashlib
import json
import re
from datetime import UTC, date, datetime
from pathlib import Path

import duckdb
from cdsci.lake.contracts import (
    ColumnContract,
    DatasetContract,
    TableContract,
    TemporalModel,
)
from cdsci.lake.publish.release import AcceptanceCheck
from cdsci.lake.publish.verify import verify_release_over_release
from pyiceberg.types import ListType

from . import icite
from .publish import PUBMED_ARTICLE, ROW_HISTORY, _lit
from .release import BUGSIGDB
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


# The bundle's own tables that have a contract outside a dataset release of their own.
BUNDLE = DatasetContract(
    id="bioconice", title="biocOnIce release bundle",
    description="Tables of the bioconice publish bundle", publisher="bioc-on-ice",
    tables={c.name: c for c in (
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
CONTRACTS = [BUGSIGDB, BUNDLE]
# latest/ is the newest completed release: upload writes its manifest.json last.
DEFAULT_REFERENCE = f"{PUBLIC_URL}/latest"
# Proof of a pass, in the bundle. Re-running the gate later is no substitute: once uploaded,
# latest/ is the bundle itself, and the gate refuses a reference that is not older.
GATE_FILE = "gate.json"

# Tables with no TableContract yet (#208). They get only the column and row-count checks:
# no key or join checks. Writing a contract for one means deleting its line here.
UNCONTRACTED_TABLES = frozenset({
    "annotation.identifier_mapping",
    "annotation.pubtator3__mention",
    "annotation.rnacentral__rna",
    "provenance.release",
    "raw.metadata",
    "raw.pubtator3__chemical",
    "raw.pubtator3__disease",
    "raw.pubtator3__gene",
    "raw.pubtator3__mutation",
    "raw.pubtator3__species",
    "raw.rnacentral__id_mapping",
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


def manifest_sha256(release: Path) -> str:
    return hashlib.sha256((release / "manifest.json").read_bytes()).hexdigest()


def _columns(manifest):
    return {f"{t['schema']}.{t['table']}": [(c["name"], c["type"]) for c in t["columns"]]
            for t in manifest["tables"]}


def allow_dropped_history(checks, allowed, ref_manifest, manifest):
    """(checks, {table: dropped columns}): a failed columns_unchanged of a table in `allowed`
    passes when its only change from the reference is dropped ROW_HISTORY columns (#222)."""
    before, after = _columns(ref_manifest), _columns(manifest)
    changes = {t: [] for t in sorted(allowed)}
    out = []
    for c in checks:
        table = c.name.removesuffix(".columns_unchanged")
        kept = [col for col in before.get(table, []) if col[0] not in ROW_HISTORY]
        if not c.passed and table in changes and kept == after.get(table):
            changes[table] = [n for n, _ in before[table] if n in ROW_HISTORY]
            c = AcceptanceCheck(c.name, True, c.required,
                                f"schema change allowed: dropped {', '.join(changes[table])}")
        out.append(c)
    return out, changes


def column_diff(before, after):
    """The exact change from column list `before` to `after`, of (name, type) pairs: a
    rename is a removal and an addition."""
    old, new = dict(before), dict(after)
    return {"added": {n: t for n, t in after if n not in old},
            "removed": {n: t for n, t in before if n not in new},
            "retyped": {n: {"from": t, "to": new[n]} for n, t in before
                        if n in new and new[n] != t},
            "reordered": [n for n, _ in before if n in new] != [n for n, _ in after if n in old]}


def _describe(diff):
    parts = [f"{k} " + ", ".join(f"{n} {t}" for n, t in diff[k].items())
             for k in ("added", "removed") if diff[k]]
    if diff["retyped"]:
        parts.append("retyped " + ", ".join(f"{n} {t['from']} -> {t['to']}"
                                            for n, t in diff["retyped"].items()))
    return "; ".join(parts + ["reordered"] * diff["reordered"]) or "none"


def allow_changed_columns(checks, allowed, ref_manifest, manifest):
    """(checks, {table: column_diff}): a failed columns_unchanged of a table in `allowed`
    passes whatever its change from the reference (owner approval, #183 lake migration)."""
    before, after = _columns(ref_manifest), _columns(manifest)
    changes = {t: column_diff(before.get(t, []), after.get(t, [])) for t in sorted(allowed)}
    out = []
    for c in checks:
        table = c.name.removesuffix(".columns_unchanged")
        if not c.passed and table in changes and c.name != table:
            c = AcceptanceCheck(c.name, True, c.required,
                                f"column change allowed: {_describe(changes[table])}")
        out.append(c)
    return out, changes


def gate(release: Path, reference: str | None = DEFAULT_REFERENCE,
         allow_schema_change=(), allow_column_change=()) -> bool:
    """Print the gate report for the bundle `release` against `reference` (a release base
    URL or directory); True when every required check passes, and then write gate.json.
    Tables in `allow_schema_change` may drop row-history columns (owner approval, #222);
    those in `allow_column_change` may change columns in any way (owner approval, #183).
    Every other check still runs on both."""
    (release / GATE_FILE).unlink(missing_ok=True)
    manifest = json.loads((release / "manifest.json").read_text())
    ref_release = None
    ref_manifest = {"tables": []}
    con = release_con(release, manifest["tables"])
    if reference is not None:
        try:
            ref_manifest = json.loads(con.execute(
                "SELECT content FROM read_text(?)",
                [f"{reference.rstrip('/')}/manifest.json"]).fetchone()[0])
            ref_release = ref_manifest["release"]
        except (duckdb.Error, ValueError, KeyError) as e:
            print(f"FAIL: reference {reference}: no readable manifest.json ({type(e).__name__})")
            return False
        print(f"release {manifest['release']} against {reference} (release {ref_release})")
        try:
            older = date.fromisoformat(ref_release) < date.fromisoformat(manifest["release"])
        except (TypeError, ValueError):
            print(f"FAIL: release ids must be YYYY-MM-DD dates: reference {ref_release!r}, "
                  f"release {manifest['release']!r}")
            return False
        # latest/ after an upload is the bundle itself.
        if not older:
            print(f"FAIL: reference release {ref_release} is not older than {manifest['release']}; "
                  f"pass --reference {PUBLIC_URL}/v<date> with an earlier release date")
            return False
    report = verify_release_over_release(
        "bioconice", release, reference, CONTRACTS, UNCONTRACTED_TABLES)
    checks, schema_changes = allow_dropped_history(report.checks, allow_schema_change,
                                                   ref_manifest, manifest)
    checks, column_changes = allow_changed_columns(
        checks, allow_column_change, ref_manifest, manifest)
    checks += bundle_checks(con, manifest["tables"], readme_queries())
    if schema_changes:
        print("schema change allowed (owner approval, #222): " + "; ".join(
            f"{t} drops {', '.join(cols) or 'nothing'}" for t, cols in schema_changes.items()))
    if column_changes:
        print("column change allowed (owner approval, #183): " + "; ".join(
            f"{t}: {_describe(d)}" for t, d in column_changes.items()))
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
    if not failed:
        (release / GATE_FILE).write_text(json.dumps({
            "release": manifest["release"], "manifest_sha256": manifest_sha256(release),
            "reference": reference, "reference_release": ref_release,
            "schema_changes": schema_changes, "column_changes": column_changes,
            "passed_at": datetime.now(UTC).isoformat(timespec="seconds")}, indent=2) + "\n")
    return not failed
