import duckdb
import pytest

# Frozen carry-overs a lake-backed release SELECT adds (#222); compared on their own.
FROZEN = ("landed_in", "valid_from", "valid_to")


@pytest.fixture
def release_matches_lander():
    """Check a source's lake-backed release SELECTs against what its lander wrote (#183).

    `lake` maps each cdsci-lake table to the raw Iceberg table it replaces: the lake
    table is that raw table without `landed_in`, which is what the cdsci-lake source
    lands. Each SELECT in `release` must give the lander's current rows, with the same
    columns and types, the frozen columns set to `frozen`, and valid_to NULL.
    """
    def check(cat, lake, release, frozen):
        con = duckdb.connect()
        con.execute("ATTACH ':memory:' AS lake")
        for name, raw in lake.items():
            con.register("raw", cat.load_table(raw).scan().to_arrow())
            con.execute(f"CREATE SCHEMA IF NOT EXISTS lake.{name.split('.')[1]}")
            con.execute(f"CREATE TABLE {name} AS SELECT * EXCLUDE (landed_in) FROM raw")
        for name, sql in release.items():
            con.register("lander", cat.load_table(name).scan().to_arrow())
            con.execute("CREATE OR REPLACE TABLE want AS SELECT * FROM lander")
            con.execute(f"CREATE OR REPLACE TABLE got AS {sql}")
            assert con.sql("DESCRIBE got").fetchall() == con.sql("DESCRIBE want").fetchall(), name
            cols = [c[0] for c in con.sql("DESCRIBE got").fetchall()]
            frozen_cols = [c for c in FROZEN if c in cols]
            for c in frozen_cols:
                want = None if c == "valid_to" else frozen
                assert con.sql(f"SELECT DISTINCT {c} FROM got").fetchall() == [(want,)], (name, c)
            keep = ", ".join(f'"{c}"' for c in cols if c not in FROZEN)
            current = " WHERE valid_to IS NULL" if "valid_to" in cols else ""
            for a, b in (("got", f"want{current}"), (f"want{current}", "got")):
                assert con.sql(f"SELECT count(*) FROM (SELECT {keep} FROM {a} EXCEPT ALL "
                               f"SELECT {keep} FROM {b})").fetchone()[0] == 0, (name, a)
    return check
