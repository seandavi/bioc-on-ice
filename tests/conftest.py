import duckdb
import pytest

from bioconice.publish import ROW_HISTORY


@pytest.fixture
def release_matches_lander():
    """Check a source's lake-backed release SELECTs against what its lander wrote (#183).

    `lake` maps each cdsci-lake table to the raw Iceberg table it replaces: the lake
    table is that raw table without `landed_in`, which is what the cdsci-lake source
    lands. Each SELECT in `release` must give the lander's current rows, with the same
    columns and types less the row-history columns (#222). Lake tables in `line_numbered`
    are keyed by a leading line_number, as cdsci-lake lands them.
    """
    def check(cat, lake, release, line_numbered=()):
        con = duckdb.connect()
        con.execute("ATTACH ':memory:' AS lake")
        for name, raw in lake.items():
            con.register("raw", cat.load_table(raw).scan().to_arrow())
            con.execute(f"CREATE SCHEMA IF NOT EXISTS lake.{name.split('.')[1]}")
            number = "row_number() OVER ()::INTEGER AS line_number, " if name in line_numbered else ""
            con.execute(f"CREATE TABLE {name} AS SELECT {number}* EXCLUDE (landed_in) FROM raw")
        for name, sql in release.items():
            con.register("lander", cat.load_table(name).scan().to_arrow())
            cols = [c[0] for c in con.sql("DESCRIBE lander").fetchall()]
            keep = ", ".join(f'"{c}"' for c in cols if c not in ROW_HISTORY)
            current = " WHERE valid_to IS NULL" if "valid_to" in cols else ""
            con.execute(f"CREATE OR REPLACE TABLE want AS SELECT {keep} FROM lander{current}")
            con.execute(f"CREATE OR REPLACE TABLE got AS {sql}")
            assert con.sql("DESCRIBE got").fetchall() == con.sql("DESCRIBE want").fetchall(), name
            for a, b in (("got", "want"), ("want", "got")):
                assert con.sql(f"SELECT count(*) FROM (SELECT * FROM {a} EXCEPT ALL "
                               f"SELECT * FROM {b})").fetchone()[0] == 0, (name, a)
    return check
