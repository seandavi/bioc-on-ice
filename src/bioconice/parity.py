"""Parity gate (issue #190, map #187): a release bundle against the live catalog it came from.

Every table's manifest row_count and Parquet count(*) must equal the live count of the
same SELECT `publish` copied from (publish.TABLES), and every README SQL query must return
the same rows from both. Reads the live catalog only.
"""

import json
import re
from collections import Counter
from pathlib import Path

import duckdb

from .publish import TABLES, _lit

README = Path(__file__).parents[2] / "README.md"


def readme_queries(path=README):
    """(line, sql) for each ```sql block in the README that is a query (SELECT / WITH)."""
    text = path.read_text()
    return [(text[:m.start()].count("\n") + 2, m[1].strip().rstrip(";"))
            for m in re.finditer(r"```sql\n(.*?)```", text, re.S)
            if re.match(r"\s*(SELECT|WITH)\b", m[1], re.I)]


def release_con(release: Path, tables):
    """The bundle's local Parquet as `bioc.<schema>.<table>` views, the names the README uses."""
    con = duckdb.connect()
    con.execute("ATTACH ':memory:' AS bioc")
    for s in sorted({t["schema"] for t in tables}):
        con.execute(f"CREATE SCHEMA bioc.{s}")
    for t in tables:
        con.execute(f"CREATE VIEW bioc.{t['schema']}.{t['table']} AS "
                    f"SELECT * FROM read_parquet({_lit(str(release / t['path']))})")
    return con


def _rows(con, sql):
    """The result as a multiset (order-insensitive), or the error text."""
    try:
        return Counter(con.execute(sql).fetchall())
    except duckdb.Error as e:
        return f"error: {e}".splitlines()[0]


def parity(live, release: Path, queries=None) -> bool:
    """Print the parity table for `release` against `live` (`bioc` attached); True when all match."""
    tables = json.loads((release / "manifest.json").read_text())["tables"]
    rel = release_con(release, tables)
    select = {f"{s}.{t}": sql for s, t, sql in TABLES}
    # Unqualified names in a README query (`provenance.release`) resolve in bioc.
    for con in (rel, live):
        con.execute("USE bioc.provenance")
    ok = True

    def row(check, manifest, parquet, live_n, good):
        nonlocal ok
        ok &= good
        print(f"| {check} | {manifest} | {parquet} | {live_n} | {'ok' if good else 'MISMATCH'} |", flush=True)

    print("| check | manifest | parquet | live | |\n| --- | ---: | ---: | ---: | --- |")
    for t in tables:
        name = f"{t['schema']}.{t['table']}"
        n = rel.execute(f"SELECT count(*) FROM bioc.{name}").fetchone()[0]
        if name not in select:
            row(f"`{name}`", f"{t['row_count']:,}", f"{n:,}", "not in publish.TABLES", False)
            continue
        n_live = live.execute(f"SELECT count(*) FROM ({select[name]})").fetchone()[0]
        row(f"`{name}`", f"{t['row_count']:,}", f"{n:,}", f"{n_live:,}", t["row_count"] == n == n_live)
    for line, sql in readme_queries() if queries is None else queries:
        a, b = _rows(rel, sql), _rows(live, sql)
        size = lambda r: f"{r.total():,} rows" if isinstance(r, Counter) else r  # noqa: E731
        row(f"README query, line {line}", "", size(a), size(b), isinstance(a, Counter) and a == b)
    print("PASS" if ok else "FAIL")
    return ok
