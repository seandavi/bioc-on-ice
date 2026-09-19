"""`bioconice migrate-assembly-scope` (issue #94) against a warehouse in the old shape."""

import dataclasses
from pathlib import Path

import pyarrow as pa
import pytest
from pyiceberg.exceptions import NoSuchTableError
from pyiceberg.expressions import AlwaysTrue, EqualTo
from pyiceberg.schema import Schema

from bioconice import catalog, ensembl, merge, migrate, schemas

HUMAN = {"taxon_id": 9606, "assembly": "GRCh38.p14", "accession": "GCA_000001405.29"}
NEW = {"annotation.gene": "genome_id", "annotation.transcript": "genome_id",
       "annotation.exon": "genome_id", "raw.ensembl__gtf": "genome_id",
       "reference.genome": "is_canonical"}


def old_shape(identifier):
    """The declaration as it was before #94: without the column this migration adds."""
    d, drop = schemas.TABLES[identifier], NEW[identifier]
    return dataclasses.replace(
        d, schema=Schema(*[f for f in d.schema.fields if f.name != drop]),
        business_key=tuple(k for k in d.business_key if k != drop),
        partition_by=tuple(k for k in d.partition_by if k != drop))


def seed(cat, identifier, release, rows, scope=AlwaysTrue()):
    fields = [f for f in cat_schema(identifier) if f.name not in merge.VALIDITY]
    merge.merge(cat, identifier, pa.Table.from_pylist(rows, schema=pa.schema(fields)),
                release, scope)


def cat_schema(identifier):
    return schemas.TABLES[identifier].schema.as_arrow()


def gene(gene_id, taxon, version, symbol, gene_type, curation):
    return {"gene_id": gene_id, "taxon_id": taxon, "source": "ENSEMBL", "version": version,
            "symbol": symbol, "gene_type": gene_type, "curation_source": curation}


TP53 = gene("ENSG00000141510", 9606, "18", "TP53", "protein_coding", "ensembl_havana")
LNC = gene("ENSG00000288825", 9606, "1", None, "lncRNA", "havana")
GONE = gene("ENSG00000000001", 9606, "1", "GONE", "lncRNA", "havana")
FISH = gene("ENSTNIG00000000002", 99883, "1", None, "protein_coding", "ensembl")


@pytest.fixture
def cat(tmp_path, monkeypatch):
    """One assembly per taxon, tables in the pre-#94 shape, one closed history row."""
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    cat = catalog()
    with monkeypatch.context() as m:
        for identifier in NEW:
            m.setitem(schemas.TABLES, identifier, old_shape(identifier))
        seed(cat, "reference.genome", "2026.08", [
            {"genome_id": HUMAN["accession"], "taxon_id": 9606, "source": "ENSEMBL",
             "assembly_name": "GRCh38.p14"},
            # Ensembl publishes no accession for this one; production holds ''
            {"genome_id": "", "taxon_id": 99883, "source": "ENSEMBL",
             "assembly_name": "TETRAODON 8.0"}])
        seed(cat, "annotation.gene", "2026.08", [TP53, LNC, GONE, FISH])
        seed(cat, "annotation.gene", "2026.09", [TP53, LNC, FISH])       # GONE is retired
        seed(cat, "annotation.transcript", "2026.08", [
            {"transcript_id": "ENST00000269305", "taxon_id": 9606, "source": "ENSEMBL",
             "gene_id": TP53["gene_id"], "version": "9", "biotype": "protein_coding",
             "canonical": True}])
        seed(cat, "annotation.exon", "2026.08", [
            {"exon_id": "ENSE00002064269", "transcript_id": "ENST00000269305", "taxon_id": 9606,
             "source": "ENSEMBL", "sequence_name": "17", "start": 7687377, "end": 7687550,
             "strand": "-", "rank": 1}])
        seed(cat, "annotation.identifier_mapping", "2026.08", [
            {"source_namespace": "ENSEMBL", "source_id": TP53["gene_id"],
             "target_namespace": ns, "target_id": target, "taxon_id": 9606, "source": source}
            for ns, target, source in (("SYMBOL", "TP53", "Ensembl"), ("ENTREZ", "7157", "NCBI"))])
        merge.write(cat, "raw.ensembl__gtf", pa.Table.from_pylist(
            [{"seqname": "17", "feature": "gene", "taxon_id": 9606, "ensembl_release": "116",
              "landed_in": "2026.08"}], schema=cat_schema("raw.ensembl__gtf")), AlwaysTrue())
    return cat


def rows(cat, identifier, row_filter=AlwaysTrue()):
    return cat.load_table(identifier).scan(row_filter=row_filter).to_arrow().to_pylist()


def intervals(cat, identifier, key):
    return sorted((r[key], r["valid_from"], r["valid_to"]) for r in rows(cat, identifier))


@pytest.mark.parametrize("copy_swap", [False, True])
def test_rows_gain_their_assembly_and_keep_their_history(cat, copy_swap):
    before = intervals(cat, "annotation.gene", "gene_id")
    assert ("ENSG00000000001", "2026.08", "2026.09") in before            # the closed row

    migrate.assembly_scope(cat, copy_swap)

    genes = rows(cat, "annotation.gene")
    assert intervals(cat, "annotation.gene", "gene_id") == before
    assert {(g["taxon_id"], g["genome_id"]) for g in genes} == {
        (9606, HUMAN["accession"]), (99883, "TETRAODON 8.0")}
    table = cat.load_table("annotation.gene")
    assert "genome_id" in table.schema().identifier_field_names()
    assert [f.name for f in table.spec().fields] == ["source", "taxon_id", "genome_id"]
    for identifier in ("annotation.transcript", "annotation.exon", "raw.ensembl__gtf"):
        assert [r["genome_id"] for r in rows(cat, identifier)] == [HUMAN["accession"]]
    # the original is kept under __v1 by a rename; a copy-swap keeps __v2 instead
    kept, gone = ("__v2", "__v1") if copy_swap else ("__v1", "__v2")
    assert len(rows(cat, "annotation.gene" + kept)) == len(before)
    with pytest.raises(NoSuchTableError):
        cat.load_table("annotation.gene" + gone)

    genome = {g["taxon_id"]: g for g in rows(cat, "reference.genome")}
    assert genome[99883]["genome_id"] == "TETRAODON 8.0"
    assert all(g["is_canonical"] and g["valid_from"] == "2026.08" for g in genome.values())

    mapping = {m["source"]: m for m in rows(cat, "annotation.identifier_mapping")}
    assert sorted(mapping) == ["ENSEMBL", "NCBI"]
    assert (mapping["ENSEMBL"]["target_id"], mapping["ENSEMBL"]["valid_from"]) == ("TP53", "2026.08")

    # the descriptions changed in place follow the declaration
    table = cat.load_table("annotation.identifier_mapping")
    assert "canonical assembly" in table.properties["comment"]
    assert "ENSEMBL" in table.schema().find_field("source").doc

    # a second run finds nothing to do: no table gets a new snapshot
    names = [*NEW, "annotation.identifier_mapping"]
    state = {n: cat.load_table(n).metadata_location for n in names}
    migrate.assembly_scope(cat, copy_swap)
    assert {n: cat.load_table(n).metadata_location for n in names} == state

    # and the migrated rows are what the new writer writes: nothing to merge
    gtf = Path(__file__).parent / "tiny.gtf"
    ensembl.land_raw(cat, "2026.10", "x", "116", url=str(gtf), info=HUMAN)
    counts = ensembl.transform(cat, "2026.10", HUMAN, "116")
    assert counts["annotation.gene"]["written"] == 0
    assert counts["annotation.identifier_mapping"]["written"] == 0
    assert counts["reference.genome"]["written"] == 0
    assert len(rows(cat, "raw.ensembl__gtf")) == 11                      # replaced, not added to


def test_a_taxon_without_exactly_one_genome_stops_the_run_before_any_write(cat):
    orphan = gene("ENSMUSG00000059552", 10090, "1", "Trp53", "protein_coding", "ensembl_havana")
    table = cat.load_table("annotation.gene")
    table.append(pa.Table.from_pylist([{**orphan, "valid_from": "2026.09", "valid_to": None}],
                                      schema=table.schema().as_arrow()))
    with pytest.raises(SystemExit, match="taxon 10090, source ENSEMBL map to 0"):
        migrate.assembly_scope(cat)
    assert rows(cat, "annotation.gene__v2") == []
    assert "genome_id" not in cat.load_table("annotation.gene").schema().column_names


def test_an_ingest_before_the_migration_writes_nothing(cat):
    before = len(rows(cat, "raw.ensembl__gtf"))
    with pytest.raises(ValueError, match="rebuild the table"):
        ensembl.land_raw(cat, "2026.10", "x", "116", info=HUMAN,
                         url=str(Path(__file__).parent / "tiny.gtf"))
    assert len(rows(cat, "raw.ensembl__gtf")) == before


def test_respelling_refuses_to_double_a_mapping(cat):
    """An ingest with the new code before the migration asserts 'ENSEMBL' rows of its own."""
    table = cat.load_table("annotation.identifier_mapping")
    dup = rows(cat, "annotation.identifier_mapping", EqualTo("source", "Ensembl"))
    table.append(pa.Table.from_pylist([{**r, "source": "ENSEMBL"} for r in dup],
                                      schema=table.schema().as_arrow()))
    with pytest.raises(SystemExit, match="both 'Ensembl' and 'ENSEMBL'"):
        migrate.assembly_scope(cat)
