"""schemas.create evolves a live table when its declaration gains a column (issue #118)."""

import dataclasses

import pyarrow as pa
import pytest
from pyiceberg.expressions import EqualTo
from pyiceberg.schema import Schema
from pyiceberg.types import NestedField, StringType

from bioconice import catalog, merge, schemas

ID = "provenance.release"


@pytest.fixture
def cat(tmp_path, monkeypatch):
    monkeypatch.setenv("BIOCONICE_WAREHOUSE", str(tmp_path))
    monkeypatch.delenv("BIOCONICE_URI", raising=False)
    return catalog()


def declare(monkeypatch, *extra):
    d = schemas.TABLES[ID]
    grown = dataclasses.replace(d, schema=Schema(*d.schema.fields, *extra))
    monkeypatch.setitem(schemas.TABLES, ID, grown)


def test_a_new_optional_column_is_added_and_old_rows_read_null(cat, monkeypatch):
    merge.manifest(cat, "2026.09", "old", "http://x", 1)

    declare(monkeypatch, NestedField(99, "artifact", StringType(), doc="Added later."))
    old = cat.load_table(ID).scan().to_arrow().to_pylist()[0]
    new = pa.Table.from_pylist([{**old, "source": "new", "artifact": "homo_sapiens"}])
    merge.write(cat, ID, new, EqualTo("source", "new"))   # create evolves, then the cast holds

    rows = {r["source"]: r for r in cat.load_table(ID).scan().to_arrow().to_pylist()}
    assert rows["old"]["artifact"] is None and rows["new"]["artifact"] == "homo_sapiens"
    assert cat.load_table(ID).schema().find_field("artifact").doc == "Added later."
    # and it is idempotent: a second create changes nothing
    before = cat.load_table(ID).metadata.current_schema_id
    schemas.create(cat, ID)
    assert cat.load_table(ID).metadata.current_schema_id == before


def test_a_new_required_column_is_refused(cat, monkeypatch):
    schemas.create(cat, ID)
    declare(monkeypatch, NestedField(99, "genome_id", StringType(), required=True, doc="Key."))
    with pytest.raises(ValueError, match="rebuild"):
        schemas.create(cat, ID)
