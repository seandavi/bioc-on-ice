"""bioconice-bugsigdb dataset release, built from a LOCAL DuckLake (offline)."""

import json
from datetime import date

import duckdb
import pytest

from bioconice.release import BUGSIGDB, SIGNATURE_TAXON, release_bugsigdb

pytest.importorskip("cdsci.lake")
from cdsci.lake import Settings  # noqa: E402
from cdsci.lake.connect import lake_connect  # noqa: E402
from cdsci.lake.contracts_render import lint_contract  # noqa: E402
from cdsci.lake.publish.builder import LocalDirStore  # noqa: E402
from cdsci.lake.publish.frozen import frozen_ducklake_attach_sql  # noqa: E402
from cdsci.lake.publish.verify import verify_release  # noqa: E402

DAY = date(2026, 10, 2)


@pytest.fixture
def con(tmp_path):
    c = lake_connect(Settings(lake_backend="local", storage_base_uri=f"file://{tmp_path}/lake"))
    c.execute("CREATE SCHEMA lake.bugsigdb")
    c.execute(
        "CREATE TABLE lake.bugsigdb.signature_taxon (bsdb_id VARCHAR NOT NULL, "
        "member_index INTEGER NOT NULL, taxon_rank VARCHAR, taxon_name VARCHAR, "
        "ncbitaxon_id INTEGER, taxon_lineage VARCHAR, taxon_lineage_ids VARCHAR)")
    c.execute(
        "INSERT INTO lake.bugsigdb.signature_taxon VALUES "
        "('bsdb:1/1/1', 1, 's', 'Anaerostipes caccae', 105841, 'k__Bacillati|s__A', '1|105841'), "
        "('bsdb:1/1/1', 2, 'g', 'Blautia', 572511, 'k__Bacillati|g__Blautia', '1|572511'), "
        "('bsdb:2/1/1', 1, 's', 'Unknown thing', NULL, 's__Unknown thing', NULL)")
    return c


def test_contract_lints_clean():
    assert lint_contract(SIGNATURE_TAXON) == []


def test_release_verifies_and_same_day_second_release(con, tmp_path):
    out = tmp_path / "pub"
    m = release_bugsigdb(con, out, today=DAY)
    assert m.release == "2026-10-02"
    assert m.status.value == "published"
    assert m.tables[0].row_count == 3
    assert verify_release(LocalDirStore(out), "bioconice-bugsigdb", "2026-10-02",
                          contract=BUGSIGDB).passed
    latest = json.loads((out / "bioconice-bugsigdb" / "latest.json").read_text())
    assert latest["release"] == "2026-10-02"

    m2 = release_bugsigdb(con, out, today=DAY)
    assert m2.release == "2026-10-02.2"
    latest = json.loads((out / "bioconice-bugsigdb" / "latest.json").read_text())
    assert latest["release"] == "2026-10-02.2"

    c = duckdb.connect()
    c.execute(frozen_ducklake_attach_sql(str(out / "bioconice-bugsigdb" / "2026-10-02")))
    rel = c.sql('SELECT * FROM published."annotation.signature_taxon"')
    assert len(rel.fetchall()) == 3
    assert "valid_from" not in rel.columns


def test_default_output_is_temporary(con, capsys):
    m = release_bugsigdb(con, today=DAY)
    assert m.release == "2026-10-02"
    assert "temporary, removed" in capsys.readouterr().out
